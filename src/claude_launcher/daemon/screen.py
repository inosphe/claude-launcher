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
import logging
import time
from typing import Callable, Deque, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

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
        #: One hash per visible row, carried between samples so only the rows
        #: pyte marked dirty are recomputed. Dropped (set to None) by anything
        #: that moves the grid without marking rows.
        self._line_cache: Optional[List[int]] = None
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

    @property
    def history_limit(self) -> int:
        return self._screen.history.size

    def set_history_limit(self, lines: int) -> None:
        """Re-bound the scrollback to ``lines``, keeping its newest rows.

        pyte's history is a pair of ``deque(maxlen=...)``; the limit can only
        change by rebuilding them. Shrinking drops the oldest rows (the
        transcript log still has them); growing keeps everything and lets
        the history fill further from here.
        """
        lines = max(0, int(lines))
        h = self._screen.history
        if h.size == lines:
            return
        top = deque(list(h.top)[-lines:] if lines else (), maxlen=lines)
        bottom = deque(list(h.bottom)[:lines] if lines else (), maxlen=lines)
        # ``position`` counts from the bottom: ``size`` means "showing the
        # live screen", less means scrolled up by the difference. Keep that
        # offset (bounded by what ``bottom`` still holds) rather than the raw
        # number -- pyte's ``before_event`` spins ``next_page()`` until
        # ``position == size``, and with an empty ``bottom`` that never
        # advances, so a stale ``position`` below the new size hangs the feed.
        scrolled = min(h.size - h.position, len(bottom))
        self._screen.history = h._replace(
            top=top, bottom=bottom, size=lines, position=lines - scrolled
        )
        self._revision += 1
        # pyte marks no rows for this, and the grid a page turn draws from
        # has changed, so the per-row cache cannot be carried across it.
        self._line_cache = None

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

        A grid that *did* move is re-hashed row by row, and only for the rows
        pyte marked in ``Screen.dirty``. The revision alone does not say how
        much moved, and a session printing one line rebuilt the whole grid
        for it; on the live daemon (42 running sessions) that sampling was
        28.5% of the event loop thread's time. pyte marks every row when the
        whole grid moves -- a scroll, a reset, a resize, an alternate-screen
        switch, a page turn -- so a partial mark means the rest of the grid
        is unchanged. ``dirty`` is consumed here and nowhere else; the paths
        that move the grid without pyte's bookkeeping (``set_history_limit``)
        drop the cache instead.
        """
        if self._hashes is not None and self._hashes[0] == self._revision:
            return self._hashes[1]
        screen = self._screen
        rows = screen.lines
        cache = self._line_cache
        if cache is None or len(cache) != rows:
            cache = [0] * rows
            changed = range(rows)
        else:
            changed = [y for y in screen.dirty if 0 <= y < rows]
        for y in changed:
            cache[y] = self._hash_line(y)
        screen.dirty.clear()
        self._line_cache = cache
        out = tuple(cache)
        self._hashes = (self._revision, out)
        return out

    def _hash_line(self, y: int) -> int:
        """One row's fingerprint, read the way :meth:`render_screen` reads it."""
        line = self._screen.buffer[y]
        cols = self._screen.columns
        return hash("".join(line[x].data for x in range(cols)).rstrip())

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

    def row_sequence(self, index: int, offset: int = 0) -> bytes:
        """The bytes that put one row of the viewer's screen back as the grid
        has it: cursor to that row, the line cleared, the row's text with its
        attributes, attributes reset. The cursor is left on that row -- the
        caller wraps the sequence in a save/restore (``daemon/notice.py``,
        which uses this to take an overlay down without a full repaint).

        ``offset`` selects the same window :meth:`repaint_sequence` paints, so
        a viewer scrolled back into history gets that window's row.
        """
        history = self._screen.history.top
        rows = self._screen.lines
        hlen = len(history)
        offset = max(0, min(offset, hlen))
        index = max(0, min(index, rows - 1))
        vpos = hlen - offset + index
        if vpos < hlen:
            row = self._row_with_attrs(history[vpos])
        else:
            row = self._row_with_attrs(self._screen.buffer[vpos - hlen])
        return ("\x1b[%d;1H\x1b[2K%s\x1b[0m" % (index + 1, row)).encode("utf-8")

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

#: The most unrendered output one session may hold. A session that writes
#: faster than it is rendered (a background session is paced to ~80 KiB/s;
#: the pi harness printing tool results was measured at 60–110 KiB/s) grows
#: its queue without bound otherwise — 1.4 GB in two minutes on 2026-09-11,
#: four such sessions taking the daemon from 340 MB to 1.8 GB. Past this,
#: the *oldest* pending bytes are dropped: the transcript on disk already has
#: them (the log is written before the queue), and pyte re-converges on the
#: program's next repaint. 4 MiB is ~6 s of render — a viewer sees the grid
#: lag a few seconds, never a daemon that stops answering.
PENDING_MAX = 4 * 1024 * 1024

#: What :meth:`ScreenFeeder.drain_now` still renders, from the end of the
#: queue. It runs synchronously on the loop (session exit), so it is bounded
#: like a slice is, only larger: 256 KiB is ~0.4 s. Rendering the whole queue
#: there stalled the loop for minutes when the queue was hundreds of MB.
DRAIN_TAIL = 256 * 1024

#: A session nobody is looking at keeps this much unrendered output, not
#: :data:`PENDING_MAX`. Its grid is read on a timer (status sampling) and on
#: demand (capture, the repaint a new viewer gets), and for those the *last*
#: quarter-megabyte is what matters; rendering a 4 MiB backlog for a screen
#: nobody sees is the CPU the 2026-09-11 outage ran on.
BACKGROUND_PENDING_MAX = 256 * 1024

#: Scrollback lines a background session keeps. A full 5000-line history of
#: coloured 120-column rows is 122 MiB per session (measured); thirty such
#: sessions were the daemon's 4 GB. Raised back to the configured
#: ``scrollback_lines`` the moment a viewer focuses the session (what was
#: trimmed meanwhile is in the transcript log, not in the grid).
BACKGROUND_HISTORY = 500

#: Bytes per second the *sum* of all background sessions may render.
#: Per-session pacing (``background_render_delay``) bounds one session at
#: ~80 KiB/s; thirty flooding sessions still add up to more than pyte can do
#: (~660 KiB/s here). This is the daemon-wide ceiling, well under that.
BACKGROUND_RENDER_BUDGET = 256 * 1024


class RenderBudget:
    """A token bucket shared by every background feeder in the daemon.

    ``take(n)`` returns once ``n`` bytes of budget are available, waiting
    out the deficit at ``rate`` bytes per second. Burst is one second of
    rate, so a quiet daemon renders a fresh burst immediately and a busy one
    settles at the rate. Foreground feeders never take from it.
    """

    def __init__(self, rate: float = BACKGROUND_RENDER_BUDGET) -> None:
        self.rate = max(1.0, float(rate))
        self._tokens = self.rate
        self._stamp = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.rate, self._tokens + (now - self._stamp) * self.rate)
        self._stamp = now

    async def take(self, n: int) -> None:
        # The balance may go negative: concurrent takers each book their
        # debt and sleep it off, so the sum of what they render cannot exceed
        # the rate. (Zeroing the balance instead let every taker re-spend
        # the same refill; four feeders finished 160 KiB in 1.6 s at a
        # 20 KiB/s budget in the first version of this test.)
        self._refill()
        self._tokens -= n
        if self._tokens < 0:
            await asyncio.sleep(-self._tokens / self.rate)


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

    def __init__(
        self,
        screen: ScreenState,
        *,
        slice_size: int = SLICE,
        foreground: Optional[Callable[[], bool]] = None,
        background_delay: float = 0.0,
        max_pending: int = PENDING_MAX,
        on_overflow: Optional[Callable[[int], None]] = None,
        budget: Optional[RenderBudget] = None,
        background_max_pending: int = BACKGROUND_PENDING_MAX,
        background_render: bool = True,
    ) -> None:
        self.screen = screen
        self._slice = max(1, slice_size)
        self._foreground = foreground or (lambda: True)
        self._background_delay = max(0.0, background_delay)
        self._max_pending = max(self._slice, max_pending)
        self._bg_max_pending = max(self._slice, min(background_max_pending, self._max_pending))
        self._budget = budget
        #: False for a harness configured to render only while attached: in
        #: the background the queue is kept (tail only) and nothing is fed to
        #: pyte until a viewer focuses the session.
        self.background_render = background_render
        self._on_overflow = on_overflow
        self._pending: Deque[bytes] = deque()
        self._pending_size = 0
        #: Bytes dropped unrendered so far (see :data:`PENDING_MAX`).
        self.dropped_bytes = 0
        #: Slices pyte raised on and that were dropped (see :meth:`_render`).
        self.render_errors = 0
        self._overflowing = False
        self._pump: Optional[asyncio.Task] = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._pace_changed = asyncio.Event()

    @property
    def pending_bytes(self) -> int:
        return self._pending_size

    def _shed(self, keep: int) -> int:
        """Drop pending bytes from the head until at most ``keep`` remain.

        Whole chunks first, then the head of the survivor. That second cut
        is moved forward to the next ESC so the survivor starts on a sequence
        boundary: a cut inside ``ESC [ 48;2;r;g;b m`` leaves ``;g;b m`` — or
        worse, ``2;r;g;b B``, which pyte dispatches as ``cursor_down`` with
        five arguments and raises (90 pump deaths on 2026-09-11 before this).
        """
        dropped = 0
        while self._pending and self._pending_size > keep:
            head = self._pending[0]
            excess = self._pending_size - keep
            if len(head) <= excess:
                self._pending.popleft()
                self._pending_size -= len(head)
                dropped += len(head)
            else:
                cut = head.find(b"\x1b", excess)
                if cut < 0:
                    cut = len(head)  # no boundary ahead: the whole chunk goes
                self._pending[0] = head[cut:]
                if not self._pending[0]:
                    self._pending.popleft()
                self._pending_size -= cut
                dropped += cut
        return dropped

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
        self._pending_size += len(data)
        self._idle.clear()
        foreground = self._foreground()
        cap = self._max_pending if foreground else self._bg_max_pending
        if self._pending_size > cap:
            dropped = self._shed(cap)
            self.dropped_bytes += dropped
            # Trimming a background tail is the design, not an overflow: the
            # owner is told only when a *watched* session cannot keep up.
            if foreground and not self._overflowing:
                # Once per episode, not per chunk: a flooding session would
                # otherwise raise this on every read.
                self._overflowing = True
                if self._on_overflow is not None:
                    self._on_overflow(dropped)
        if not foreground and not self.background_render:
            return  # tail kept; rendered when a viewer arrives (see wake)
        self._ensure_pump()

    def _ensure_pump(self) -> None:
        if self._pending and (self._pump is None or self._pump.done()):
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
                    self._pending_size -= len(head)
                    rendered = len(head)
                    self._render(head)
                else:
                    self._pending[0] = head[self._slice :]
                    self._pending_size -= self._slice
                    rendered = self._slice
                    self._render(head[: self._slice])
                # The whole point: hand the loop back between slices, so an
                # accept or a delivery queued behind us gets its turn.
                # Clear first, then recheck focus. A focus notification
                # that landed between the previous check and this point
                # must not be erased before we decide to sleep.
                self._pace_changed.clear()
                if self._foreground():
                    await asyncio.sleep(0)
                    continue
                # Background from here. A harness that renders only while
                # attached stops now and keeps its tail for the next viewer.
                if not self.background_render:
                    return
                # The daemon-wide budget first (every background session
                # shares it), then this session's own pacing.
                if self._budget is not None:
                    await self._budget.take(rendered)
                if not self._background_delay:
                    await asyncio.sleep(0)
                else:
                    try:
                        await asyncio.wait_for(
                            self._pace_changed.wait(), self._background_delay
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            if not self._pending:
                self._pending_size = 0
                self._overflowing = False
                self._idle.set()

    async def drained(self) -> None:
        """Wait until everything submitted so far has been rendered."""
        await self._idle.wait()

    def wake(self) -> None:
        """Reconsider background pacing after a session focus change.

        Also restarts the pump for a tail parked by an attached-only
        harness: the viewer that just arrived is who that tail was kept for.
        """
        self._pace_changed.set()
        self._ensure_pump()

    def drain_now(self, *, tail: int = DRAIN_TAIL) -> None:
        """Render what is pending, synchronously — at most the last ``tail`` bytes.

        For teardown and for callers with no loop to await on (tests, the
        final capture of an exited session) -- never for the hot path, which
        is the stall this class exists to prevent. Bounded for the same
        reason (:data:`DRAIN_TAIL`): the last words of a session are what
        is read afterwards, and they are at the end of the queue, not the
        start. Everything is still in the transcript on disk.
        """
        if tail is not None and self._pending_size > tail:
            self.dropped_bytes += self._shed(max(0, tail))
        while self._pending:
            head = self._pending.popleft()
            self._pending_size -= len(head)
            self._render(head)
        self._pending_size = 0
        self._overflowing = False
        self._idle.set()

    def _render(self, data: bytes) -> None:
        """``feed_render`` that cannot kill the pump.

        pyte raises on a sequence it parses but cannot dispatch (a CSI with
        more parameters than the handler takes). One bad slice must cost
        that slice, not the session's screen for the rest of its life —
        which is what an exception escaping the pump task meant: the task
        died, the queue kept filling, and the grid froze.
        """
        try:
            self.screen.feed_render(data)
        except Exception:  # noqa: BLE001 — see above
            self.render_errors += 1
            if self.render_errors == 1:
                log.warning("screen render error (slice of %d bytes dropped)",
                            len(data), exc_info=True)

    def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        self._pending.clear()
        self._pending_size = 0
        self._overflowing = False
        self._idle.set()
