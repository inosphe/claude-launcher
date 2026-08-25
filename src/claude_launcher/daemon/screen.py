"""Terminal screen state for a session, built on the ``pyte`` VT emulator.

``capture-pane`` needs "what a human sees right now", not the raw byte stream —
TUI harnesses like claude redraw the whole alternate screen continuously, so an
ANSI-stripped tail of raw output is useless. Feeding every output chunk through
pyte keeps an authoritative rendered grid (plus scrollback history) that the
capture and idle-detection features read.

pyte does not track DECCKM (application cursor keys, private mode 1), which
``send-keys`` needs to encode arrow keys the way the running program expects,
nor bracketed paste (private mode 2004), which paste injection needs — so this
module watches the byte stream for ``CSI ? Pm h/l`` itself. It also tracks the
alternate screen (private mode 1049) for the same reason pyte is blind to it:
a repaint that does not say which buffer the program is in leaves a viewer
attached mid-session stuck in xterm's main buffer, where a full-screen TUI
only ever overwrites in place and the wheel has nothing to scroll.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Deque, List, Optional, Tuple

import pyte

_PRIVATE_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")

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


class ScreenState:
    """A pyte-backed screen + scrollback with launcher-specific helpers."""

    def __init__(self, cols: int, rows: int, history: int = 5000) -> None:
        self._screen = pyte.HistoryScreen(cols, rows, history=history, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)
        self._mode_tail = b""
        self.app_cursor_keys = False
        self.bracketed_paste = False
        self.alt_screen = False

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
        """
        self._stream.feed(data)

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
        self._mode_tail = window[-_TAIL:]

    def resize(self, cols: int, rows: int) -> None:
        self._screen.resize(lines=rows, columns=cols)

    # ------------------------------------------------------------------ #
    # capture
    # ------------------------------------------------------------------ #
    def render_screen(self) -> List[str]:
        """The current visible grid, one right-trimmed string per row."""
        return [line.rstrip() for line in self._screen.display]

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

    def cursor(self) -> Tuple[int, int]:
        """Cursor position as (x, y), zero-based."""
        c = self._screen.cursor
        return (c.x, c.y)

    def line_hashes(self) -> Tuple[int, ...]:
        """A cheap per-row fingerprint of the visible grid (for idle detection)."""
        return tuple(hash(line) for line in self._screen.display)

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
