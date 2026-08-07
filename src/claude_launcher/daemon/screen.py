"""Terminal screen state for a session, built on the ``pyte`` VT emulator.

``capture-pane`` needs "what a human sees right now", not the raw byte stream —
TUI harnesses like claude redraw the whole alternate screen continuously, so an
ANSI-stripped tail of raw output is useless. Feeding every output chunk through
pyte keeps an authoritative rendered grid (plus scrollback history) that the
capture and idle-detection features read.

pyte does not track DECCKM (application cursor keys, private mode 1), which
``send-keys`` needs to encode arrow keys the way the running program expects,
nor bracketed paste (private mode 2004), which paste injection needs — so this
module watches the byte stream for ``CSI ? Pm h/l`` itself.
"""

from __future__ import annotations

import re
from typing import List, Tuple

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

    @property
    def cols(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    def feed(self, data: bytes) -> None:
        self._track_modes(data)
        self._stream.feed(data)

    def _track_modes(self, data: bytes) -> None:
        window = self._mode_tail + data
        for match in _PRIVATE_MODE_RE.finditer(window):
            params = match.group(1).split(b";")
            if b"1" in params:
                self.app_cursor_keys = match.group(2) == b"h"
            if b"2004" in params:
                self.bracketed_paste = match.group(2) == b"h"
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
            out.append("".join(line[x].data for x in range(cols)).rstrip())
        return out

    def cursor(self) -> Tuple[int, int]:
        """Cursor position as (x, y), zero-based."""
        c = self._screen.cursor
        return (c.x, c.y)

    def line_hashes(self) -> Tuple[int, ...]:
        """A cheap per-row fingerprint of the visible grid (for idle detection)."""
        return tuple(hash(line) for line in self._screen.display)

    def repaint_sequence(self) -> bytes:
        """An ANSI sequence that repaints the current grid on a fresh terminal.

        Used to seed newly attached viewers (WebSocket / ``claunch attach``)
        with the current state without replaying the whole output log. Colors
        and text attributes are reconstructed from the pyte grid — TUIs only
        redraw what changes, so a plain-text seed would leave the viewer
        mostly monochrome until the next full redraw.
        """
        buffer = self._screen.buffer
        parts = ["\x1b[2J\x1b[H"]
        for y in range(self._screen.lines):
            if y:
                parts.append("\r\n")
            parts.append(self._row_with_attrs(buffer[y]))
        x, y = self.cursor()
        parts.append("\x1b[0m")
        parts.append("\x1b[%d;%dH" % (y + 1, x + 1))
        return "".join(parts).encode("utf-8")

    def _row_with_attrs(self, row) -> str:
        cols = self._screen.columns
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
