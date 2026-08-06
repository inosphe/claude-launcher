"""Terminal screen state for a session, built on the ``pyte`` VT emulator.

``capture-pane`` needs "what a human sees right now", not the raw byte stream —
TUI harnesses like claude redraw the whole alternate screen continuously, so an
ANSI-stripped tail of raw output is useless. Feeding every output chunk through
pyte keeps an authoritative rendered grid (plus scrollback history) that the
capture and idle-detection features read.

pyte does not track DECCKM (application cursor keys, private mode 1), which
``send-keys`` needs to encode arrow keys the way the running program expects —
so this module watches the byte stream for ``CSI ? 1 h/l`` itself.
"""

from __future__ import annotations

import re
from typing import List, Tuple

import pyte

_PRIVATE_MODE_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")

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

        Used to seed newly attached WebSocket viewers with the current state
        without replaying the whole output log.
        """
        parts = [b"\x1b[2J\x1b[H"]
        rows = self.render_screen()
        for i, row in enumerate(rows):
            parts.append(row.encode("utf-8"))
            if i < len(rows) - 1:
                parts.append(b"\r\n")
        x, y = self.cursor()
        parts.append(b"\x1b[%d;%dH" % (y + 1, x + 1))
        return b"".join(parts)
