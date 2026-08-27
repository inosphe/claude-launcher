"""Terminal screen state for a session, built on the ``pyte`` VT emulator.

``capture-pane`` needs "what a human sees right now", not the raw byte stream —
TUI harnesses like claude redraw the whole alternate screen continuously, so an
ANSI-stripped tail of raw output is useless. Feeding every output chunk through
pyte keeps an authoritative rendered grid (plus scrollback history) that the
capture and idle-detection features read.

pyte does not track DECCKM (application cursor keys, private mode 1), which
``send-keys`` needs to encode arrow keys the way the running program expects,
nor bracketed paste (private mode 2004), which paste injection needs, nor the
mouse-tracking modes (1000/1002/1003, and the 1006/1015/1005 report encodings),
which decide who owns the wheel — so this module watches the byte stream for
``CSI ? Pm h/l`` itself. It also tracks the
alternate screen (private mode 1049) for the same reason pyte is blind to it:
a repaint that does not say which buffer the program is in leaves a viewer
attached mid-session stuck in xterm's main buffer, where a full-screen TUI
only ever overwrites in place and the wheel has nothing to scroll.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Deque, List, Optional, Set, Tuple

import pyte

_PRIVATE_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")

#: The private mode number behind each mouse *report encoding* we track.
#: Default (no mode) is the original X10 encoding, which cannot express a
#: column past 222 — every terminal in practice asks for SGR (1006), and this
#: project's grids are 233 columns wide, so the encoding matters.
_MOUSE_ENCODING_MODES = {"sgr": 1006, "urxvt": 1015, "utf8": 1005}

# --------------------------------------------------------------------------- #
# SGR reconstruction (pyte grid attributes -> escape sequences)
# --------------------------------------------------------------------------- #
_SGR_NAMED = {
    "black": 0, "red": 1, "green": 2, "brown": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}
_HEX_COLOR_RE = re.compile(r"\A[0-9a-fA-F]{6}\Z")


def _color_params(color, base: int) -> List[str]:
    """SGR parameters for a pyte color name (named / bright / hex) or []."""
    if not color or color == "default":
        return []
    if color in _SGR_NAMED:
        return [str(base + _SGR_NAMED[color])]
    if color.startswith("bright") and color[6:] in _SGR_NAMED:
        return [str(base + 60 + _SGR_NAMED[color[6:]])]
    if _HEX_COLOR_RE.match(color):  # pyte stores 256-color/truecolor as hex
        r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
        return [str(base + 8), "2", str(r), str(g), str(b)]
    return []


def _sgr(char) -> str:
    """The full SGR parameter string ("0" == default) for one pyte cell."""
    params = ["0"]
    if char.bold:
        params.append("1")
    if char.italics:
        params.append("3")
    if char.underscore:
        params.append("4")
    if char.blink:
        params.append("5")
    if char.reverse:
        params.append("7")
    if char.strikethrough:
        params.append("9")
    params += _color_params(char.fg, 30)
    params += _color_params(char.bg, 40)
    return ";".join(params)

#: Bytes kept from the previous chunk so a mode sequence split across two
#: chunks is still recognised.
_TAIL = 16

#: The CSI parameter bytes that pyte does not merely ignore but MIS-READS.
#: Its parser drops ``>`` mid-sequence outright (``SP_OR_GT`` in
#: pyte/streams.py, commented "Secondary DA is not supported atm.") and reads
#: what is left as an ordinary CSI, so ``CSI > 4 ; 2 m`` -- the XTMODKEYS
#: modifyOtherKeys=2 that claude asserts on every start -- arrives at
#: ``select_graphic_rendition(4, 2)`` and turns underline on for every cell
#: drawn afterwards, until something happens to turn it off again. ``<`` and
#: ``=`` are in neither that set nor the ``?`` branch, so they fall through to
#: the parameter default and end the sequence early: ``CSI < u``, the kitty
#: keyboard pop claude pairs with ``CSI > 5 u``, leaves a literal ``u`` on the
#: grid.
#:
#: None of these sequences draw. They are keyboard and capability negotiation
#: addressed to the terminal on the other end, which reads them from the raw
#: stream it is sent either way -- viewers and the on-disk log get the chunk
#: untouched (Session._on_output), and only the emulator feed is filtered. So
#: the grid is right without them and wrong with them.
_CSI_UNPARSEABLE = b"<=>"

#: How far past an ``ESC [`` :meth:`ScreenState._strip_unparseable_csi` looks
#: for the final byte before giving up and handing the bytes to pyte
#: unfiltered. A real CSI is short -- the longest this project's harnesses
#: emit is a two-colour SGR at about 34 bytes -- and past this the stream is
#: something else, where holding bytes back would only delay the grid.
_CSI_SCAN_LIMIT = 128

#: How many scrolled-off lines an attaching viewer is seeded with, so its own
#: terminal can serve the wheel natively. Matched to the browser's xterm
#: ``scrollback`` — seeding more would only be dropped on arrival. Measured on
#: this project's sessions at 233 columns: 1861 attributed rows are 486 KiB of
#: escapes raw and 46 KiB once the WebSocket's permessage-deflate has had it,
#: paid once per attach — against 22.8 KiB per wheel tick for the control-frame
#: scroll it replaces.
HISTORY_SEED_LINES = 5000


class ScreenState:
    """A pyte-backed screen + scrollback with launcher-specific helpers."""

    def __init__(self, cols: int, rows: int, history: int = 5000) -> None:
        self._screen = pyte.HistoryScreen(cols, rows, history=history, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._mode_tail = b""
        #: An unfinished CSI held back from the emulator until the chunk that
        #: completes it arrives -- see :meth:`_strip_unparseable_csi`, which
        #: has to see a sequence whole to know whether to drop it.
        self._csi_carry = b""
        self._mouse_modes: Set[bytes] = set()
        # Bumped by the two calls that can move the grid (feed_render and
        # resize), so a reader can tell "nothing has happened here" apart
        # from "I have not looked lately" without touching a cell. What
        # line_hashes memoises against; see there for why.
        self._revision = 0
        self._hashes: Optional[Tuple[int, Tuple[int, ...]]] = None
        self.app_cursor_keys = False
        self.bracketed_paste = False
        self.alt_screen = False
        #: The program asked to be told about mouse buttons (1000), or that
        #: plus drag (1002) / any motion (1003). Wheel ticks ride the same
        #: reports, so this is the flag that says "the wheel is the
        #: program's, not the viewer's" — see :attr:`wheel_is_the_programs`.
        self.mouse_tracking = False
        #: An extended report encoding: SGR (1006), urxvt (1015) or utf-8
        #: (1005). Tracked for the same reason DECCKM is: a client that
        #: encodes mouse reports itself has to encode them the agreed way.
        self.mouse_encoding = ""

    @property
    def wheel_is_the_programs(self) -> bool:
        """Whether wheel ticks belong to the program rather than the viewer.

        A full-screen TUI that turns mouse tracking on (claude does: it
        asserts ``?1000h ?1002h ?1003h ?1006h`` behind ``?1049h`` and leaves
        them on) is asking for the wheel — it scrolls its own view, from its
        own model, with a depth no terminal could reconstruct. A viewer that
        swallows those ticks to scroll something else leaves the program
        believing nobody ever reached for the wheel.

        The alternate screen alone is not the test. A TUI that does *not*
        take the mouse (a pager on the alt screen) leaves the wheel to the
        terminal, and there the terminal's own answer is the right one.
        """
        return self.mouse_tracking

    @property
    def cols(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    def feed(self, data: bytes) -> None:
        """Track modes and render, in one go — the synchronous path.

        Callers on the event loop want :class:`ScreenFeeder` instead: the
        render half is CPU-bound and unbounded (see its docstring).
        """
        self.track_modes(data)
        self.feed_render(data)

    def track_modes(self, data: bytes) -> None:
        """The cheap half: a regex sweep for the private modes we track.

        Kept separate because it must stay *synchronous with input*. A
        ``send-keys`` arriving right after a program turns DECCKM on has to
        encode arrows the new way, so this cannot lag behind the way the
        rendered grid may.
        """
        self._track_modes(data)

    def feed_render(self, data: bytes) -> None:
        """The expensive half: pyte's VT emulation of ``data``.

        Roughly 530 KiB/s on this project's screens (pyte draws a cell at a
        time, rebuilding a namedtuple per character), so a 64 KiB PTY chunk
        is ~144 ms of solid CPU.

        What reaches pyte is filtered first: see
        :meth:`_strip_unparseable_csi` for the sequences it cannot be given.
        """
        self._stream.feed(self._strip_unparseable_csi(data))
        self._revision += 1

    def _strip_unparseable_csi(self, data: bytes) -> bytes:
        """``data`` minus the CSI sequences pyte mis-reads (:data:`_CSI_UNPARSEABLE`).

        Stateful, because a sequence arrives split as often as not: the PTY
        hands over whatever one read returned, and :class:`ScreenFeeder` cuts
        that again every :data:`SLICE` bytes. A sequence whose final byte has
        not arrived is held in ``_csi_carry`` and rejoined with the next call,
        because the marker byte alone does not say where the sequence ends --
        letting half through would put the rest of it on the grid as text.
        """
        buf = self._csi_carry + data if self._csi_carry else data
        self._csi_carry = b""
        if b"\x1b" not in buf:
            return buf
        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            j = buf.find(b"\x1b", i)
            if j < 0:
                out += buf[i:]
                break
            out += buf[i:j]
            if j + 1 >= n:
                self._csi_carry = buf[j:]
                break
            if buf[j + 1] != 0x5B:  # an ESC starting something else: pyte's
                out += buf[j : j + 1]
                i = j + 1
                continue
            k = j + 2
            while k < n and 0x30 <= buf[k] <= 0x3F:  # parameter bytes
                k += 1
            while k < n and 0x20 <= buf[k] <= 0x2F:  # intermediate bytes
                k += 1
            if k >= n:  # the final byte is in the next chunk, or never comes
                if n - j > _CSI_SCAN_LIMIT:
                    out += buf[j:]
                    break
                self._csi_carry = buf[j:]
                break
            if buf[j + 2] not in _CSI_UNPARSEABLE:
                out += buf[j : k + 1]
            i = k + 1
        return bytes(out)

    def _track_modes(self, data: bytes) -> None:
        window = self._mode_tail + data
        for match in _PRIVATE_MODE_RE.finditer(window):
            params = match.group(1).split(b";")
            if b"1" in params:
                self.app_cursor_keys = match.group(2) == b"h"
            if b"2004" in params:
                self.bracketed_paste = match.group(2) == b"h"
            if b"1049" in params:
                self.alt_screen = match.group(2) == b"h"
            # Any of the three tracking levels means the program is listening
            # for mouse reports; they are set and cleared independently (and
            # usually together, in one sequence), so the flag is the OR of
            # whatever is still on rather than the last one seen.
            for mode in (b"1000", b"1002", b"1003"):
                if mode not in params:
                    continue
                if match.group(2) == b"h":
                    self._mouse_modes.add(mode)
                else:
                    self._mouse_modes.discard(mode)
            self.mouse_tracking = bool(self._mouse_modes)
            for mode, name in ((b"1006", "sgr"), (b"1015", "urxvt"), (b"1005", "utf8")):
                if mode in params:
                    self.mouse_encoding = name if match.group(2) == b"h" else ""
        self._mode_tail = window[-_TAIL:]

    def forget_modes(self) -> None:
        """Drop the private modes, keeping the grid and its scrollback.

        For a screen seeded by replaying an old log: the modes in it were
        asserted by a program that is gone, and the one about to draw has not
        said anything yet. Carrying them over would encode ``send-keys``
        arrows for a DECCKM nobody turned on, and would make the repaint claim
        an alternate screen the new program may not have entered.
        """
        self.app_cursor_keys = False
        self.bracketed_paste = False
        self.alt_screen = False
        self.mouse_tracking = False
        self.mouse_encoding = ""
        self._mouse_modes.clear()
        self._mode_tail = b""

    def scroll_grid_into_history(self) -> None:
        """Push the whole visible grid off the top, leaving a blank screen.

        What a replayed log is worth to a restored session is its
        *scrollback*, not its last frame: the program that drew that frame is
        gone and the new one is about to paint its own. Rolling the grid up
        turns those rows into history — reachable by the wheel — instead of
        leaving them on screen pretending to be live.
        """
        self.feed_render(b"\r\n" * self._screen.lines)

    def resize(self, cols: int, rows: int) -> None:
        self._screen.resize(lines=rows, columns=cols)
        self._revision += 1

    # ------------------------------------------------------------------ #
    # capture
    # ------------------------------------------------------------------ #
    def render_screen(self) -> List[str]:
        """The current visible grid, one right-trimmed string per row.

        Read cell by cell rather than through pyte's ``display``, which calls
        ``wcwidth(char[0])`` on every cell and raises ``IndexError`` on the
        empty ``data`` of a wide glyph's continuation stub — a cell any grid
        holding CJK text carries, and one a DCH shift can push to column 0.
        The stub contributes nothing to the join, which is the same reading
        :meth:`render_history` and :meth:`bottom_line` already do.
        """
        screen = self._screen
        cols = screen.columns
        return [
            "".join(screen.buffer[y][x].data for x in range(cols)).rstrip()
            for y in range(screen.lines)
        ]

    def render_history(self) -> List[str]:
        """Scrolled-off lines (oldest first), right-trimmed.

        Under the alternate screen (where full-screen TUIs live) nothing
        scrolls off, matching tmux, so this may be empty; the raw on-disk log
        is the forensic fallback.
        """
        cols = self._screen.columns
        out: List[str] = []
        for line in self._screen.history.top:
            # A row is a column-keyed mapping, not a list: ``len(line)`` counts
            # the cells that were *written*, which is smaller than the row's
            # width whenever the program drew it sparsely (a cursor jump to a
            # right-aligned element leaves the cells between untouched). The
            # width is ``cols``; reading past the written cells yields the
            # default char without storing it.
            out.append("".join(line[x].data for x in range(cols)).rstrip())
        return out

    @property
    def history_len(self) -> int:
        """Scrolled-off lines available for virtual scroll (oldest first)."""
        return len(self._screen.history.top)

    def history_sequence(self, limit: int = HISTORY_SEED_LINES) -> bytes:
        """The tail of the scrollback, as bytes that fill a terminal's own.

        The seed a freshly attached viewer needs before
        :meth:`repaint_sequence`: written into the main buffer these rows
        scroll off the top the way they originally did, so the browser's
        terminal ends up holding the same scrollback the daemon does — and
        from there the wheel is the browser's own, with a scrollbar, momentum
        and find-in-page behind it, instead of a control frame per tick.

        Leads with ``?1049l`` for the reason the repaint leads with a buffer
        selector: a socket may be replacing one that left the terminal on the
        alternate screen, where these rows would be written into a buffer
        that keeps no scrollback and is about to be cleared anyway.

        Empty when there is no history — which is the honest answer for a
        session running a TUI that repaints instead of scrolling. Callers
        skip this entirely on the alternate screen; see ``ws.py``.
        """
        history = self._screen.history.top
        if not history or limit <= 0:
            return b""
        rows = list(history)[-limit:]
        parts = ["\x1b[?1049l\x1b[2J\x1b[H"]
        for i, row in enumerate(rows):
            if i:
                parts.append("\r\n")
            parts.append(self._row_with_attrs(row))
        parts.append("\x1b[0m\r\n")
        return "".join(parts).encode("utf-8")

    def cursor(self) -> Tuple[int, int]:
        """Cursor position as (x, y), zero-based."""
        c = self._screen.cursor
        return (c.x, c.y)

    def line_hashes(self) -> Tuple[int, ...]:
        """A cheap per-row fingerprint of the visible grid (for idle detection).

        Off :meth:`render_screen` rather than pyte's ``display``, which raises
        on a wide-char stub. Idle detection samples every session on a timer,
        so it is the most frequent caller of the two — and the one whose
        exception would be swallowed into "this session never goes idle".

        Memoised against the grid's revision, because "the most frequent
        caller" understates it: every session is sampled every 0.4s whether
        or not anything was printed, and one sample materialises every cell
        of the grid (a 303x77 screen is 23k pyte namedtuple reads). Sixteen
        idle sessions were spending a quarter of a core re-hashing screens
        nothing had touched. Only feed_render and resize can move the grid,
        and both bump the revision, so an unchanged grid is answered from the
        last tuple -- and the sample the tracker gets is the same tuple it
        would have computed, which is exactly what "no change" has to mean to
        it.
        """
        if self._hashes is not None and self._hashes[0] == self._revision:
            return self._hashes[1]
        out = tuple(hash(line) for line in self.render_screen())
        self._hashes = (self._revision, out)
        return out

    def bottom_line(self) -> str:
        """The bottom row of the visible grid, right-trimmed.

        A TUI's footer / status line lives here. Read straight from the
        emulator buffer — only this one row is materialized, where
        :meth:`render_screen` would rebuild every cell of the whole grid. The
        claude in-turn marker check reads only the footer, so a marker phrase
        that appears anywhere above (in transcript content) cannot be mistaken
        for a footer signal.

        Joining every cell's text needs no wide-char bookkeeping: a wide
        glyph's stub cell — and the stub a DCH shift can push to column 0 —
        carries an empty ``data``, so it contributes nothing to the join.
        (The same way :meth:`render_history` already reads this grid.)
        """
        screen = self._screen
        line = screen.buffer.get(screen.lines - 1)
        if not line:
            return ""
        return "".join(line[x].data for x in range(screen.columns)).rstrip()

    def repaint_sequence(self, offset: int = 0) -> bytes:
        """An ANSI sequence that repaints the current grid on a fresh terminal.

        Used to seed newly attached viewers (WebSocket / ``claunch attach``)
        with the current state without replaying the whole output log. Colors
        and text attributes are reconstructed from the pyte grid — TUIs only
        redraw what changes, so a plain-text seed would leave the viewer
        mostly monochrome until the next full redraw.

        ``offset`` scrolls the same repaint back into history: the window is
        composed from the virtual stream ``history + grid``, so ``offset=0``
        paints the live grid (current behaviour) and ``offset=N`` paints the
        ``rows``-tall window that ends N lines above the grid bottom.
        Clamped to :attr:`history_len`. While a viewer looks at history the
        live cursor's position is meaningless, so it is hidden; ``offset=0``
        restores it.

        The sequence leads with the buffer the program is actually in. A
        full-screen TUI (claude, the wizard) lives in the alternate screen,
        re-asserting ``?1049h`` on every full redraw; a viewer that attaches
        after that first assertion has only this grid to learn the mode from.
        Painted into xterm's main buffer instead, the screen scrolls in place
        and its scrollback stays empty — the wheel has nothing to scroll and
        the session reads as unscrollable until the program's next redraw.
        Leaving the alternate screen (or re-entering it, idempotently) makes
        the seeded grid land where the program is drawing.
        """
        history = self._screen.history.top
        rows = self._screen.lines
        hlen = len(history)
        offset = max(0, min(offset, hlen))

        parts = [
            "\x1b[?1049h" if self.alt_screen else "\x1b[?1049l",
            "\x1b[2J\x1b[H",
        ]
        # And the mouse modes, for the reason the buffer is re-asserted: a
        # viewer that attached after the program turned tracking on has only
        # this repaint to learn it from, and a terminal that does not know
        # swallows the wheel instead of reporting it — which is precisely how
        # a session ends up unscrollable. Re-asserted in the program's own
        # order (tracking levels, then the report encoding).
        for mode in sorted(self._mouse_modes):
            parts.append("\x1b[?%sh" % mode.decode())
        if self.mouse_encoding:
            parts.append("\x1b[?%dh" % _MOUSE_ENCODING_MODES[self.mouse_encoding])
        start = hlen - offset
        for i in range(rows):
            if i:
                parts.append("\r\n")
            vpos = start + i
            if vpos < hlen:
                parts.append(self._row_with_attrs(history[vpos]))
            else:
                parts.append(self._row_with_attrs(self._screen.buffer[vpos - hlen]))
        if offset > 0:
            parts.append("\x1b[0m\x1b[?25l")
        else:
            x, y = self.cursor()
            parts.append("\x1b[0m")
            parts.append("\x1b[%d;%dH" % (y + 1, x + 1))
        return "".join(parts).encode("utf-8")

    def _row_with_attrs(self, row) -> str:
        cols = self._screen.columns
        # ``row`` is pyte's StaticDefaultDict — a mapping keyed by column, so
        # ``len(row)`` is the number of cells ever written, NOT the row's
        # width. A row drawn sparsely (a cursor jump past untouched cells to a
        # right-aligned element) has far fewer written cells than its rightmost
        # column, and clamping to that count silently drops everything to the
        # right of it: the live PTY bytes carry that region fine, but every
        # repaint rebuilt from the grid loses it until the program redraws.
        # The width is ``cols``; the trim below walks back over the blank tail,
        # and reading an unwritten cell yields the default char without
        # storing it, so the mapping does not grow.
        end = cols
        while end and row[end - 1].data in ("", " ") and _sgr(row[end - 1]) == "0":
            end -= 1
        out: List[str] = []
        current = None
        for x in range(end):
            char = row[x]
            if not char.data:
                continue  # continuation cell of a wide character
            sgr = _sgr(char)
            if sgr != current:
                out.append("\x1b[" + sgr + "m")
                current = sgr
            out.append(char.data)
        if current not in (None, "0"):
            out.append("\x1b[0m")
        return "".join(out)


# --------------------------------------------------------------------------- #
# feeding the screen without stalling the event loop
# --------------------------------------------------------------------------- #
#: Bytes rendered between yields. pyte runs at roughly 530 KiB/s here, so this
#: bounds one uninterrupted render to about 7 ms — short enough that an HTTP
#: accept or a mesh delivery waiting behind it is not noticeable, long enough
#: that the per-slice overhead stays in the noise.
SLICE = 4096


class ScreenFeeder:
    """Renders PTY output into a :class:`ScreenState` a slice at a time.

    The daemon used to render inline, on the event loop, in the callback that
    received each PTY chunk. That is fine until a session floods -- and then
    it is not merely slow, it is a stall with no bottom: asyncio drains every
    ready callback before it polls for I/O again, so a reader thread posting
    chunks faster than pyte renders them keeps the ready queue permanently
    non-empty and the loop never gets back to ``accept()``. A daemon in that
    state is alive, listening, burning a core, and answering nothing -- and it
    still holds the singleton lock, so no replacement can take over either.

    So the render is pulled out of the callback and into a task that consumes
    a queue in bounded slices, yielding between them. The work is the same
    work; what changes is that the loop gets a turn every :data:`SLICE` bytes.
    Output ordering is preserved (one consumer, FIFO), and the grid converges
    a little behind the byte stream -- :meth:`drained` is how the few readers
    that need it exactly (capture, attach repaint) wait for it to catch up.
    """

    def __init__(self, screen: ScreenState, *, slice_size: int = SLICE) -> None:
        self.screen = screen
        self._slice = max(1, slice_size)
        self._pending: Deque[bytes] = deque()
        self._pump: Optional[asyncio.Task] = None
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def pending_bytes(self) -> int:
        return sum(len(c) for c in self._pending)

    def submit(self, data: bytes) -> None:
        """Queue ``data`` for rendering; returns immediately.

        The mode tracking rides along here rather than in the pump: it is a
        regex over the chunk, not a screen update, and the keyboard encoding
        that reads it must not lag behind the bytes that set it.
        """
        if not data:
            return
        self.screen.track_modes(data)
        self._pending.append(data)
        self._idle.clear()
        if self._pump is None or self._pump.done():
            self._pump = asyncio.get_event_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while self._pending:
                # The unrendered remainder stays at the head of the queue
                # rather than being held in a local, so pending_bytes always
                # answers "how much has not reached the grid yet" -- mid-chunk
                # included. That is the number worth watching in an outage.
                head = self._pending[0]
                if len(head) <= self._slice:
                    self._pending.popleft()
                    self.screen.feed_render(head)
                else:
                    self._pending[0] = head[self._slice :]
                    self.screen.feed_render(head[: self._slice])
                # The whole point: hand the loop back between slices, so an
                # accept or a delivery queued behind us gets its turn.
                await asyncio.sleep(0)
        finally:
            if not self._pending:
                self._idle.set()

    async def drained(self) -> None:
        """Wait until everything submitted so far has been rendered."""
        await self._idle.wait()

    def drain_now(self) -> None:
        """Render everything pending, synchronously.

        For teardown and for callers with no loop to await on (tests, the
        final capture of an exited session) -- never for the hot path, which
        is the stall this class exists to prevent.
        """
        while self._pending:
            self.screen.feed_render(self._pending.popleft())
        self._idle.set()

    def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        self._pending.clear()
        self._idle.set()
