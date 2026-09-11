"""Transient notices drawn over a session's terminal, per viewer.

A notice is one line of text the daemon wants a *person looking at the
session* to read — a console-encoding warning from ``claunch attach``, a
wind-down request on kill, a mesh delivery announcement, anything a caller
hands to :meth:`Session.notify` — shown for a few seconds over the top row
of the mirrored screen and then taken away again. It never enters the PTY:
the program in the session does not see it, the transcript does not record
it, and other viewers see it only if it was addressed to them too.

Two renderings, chosen per socket (``ws.py``):

- A control frame ``{"type": "notice", ...}`` to every viewer. The web
  dashboard draws it as an element over its xterm — no bytes involved.
- For a viewer that asked (``?overlay=1``, which ``claunch attach`` does),
  the daemon additionally composes the line into the byte stream it sends
  that viewer: :class:`Overlay` appends a draw after each output chunk while
  the notice is up (the program may have repainted row 1 meanwhile) and
  restores row 1 from the rendered grid when it expires. The draw wraps
  itself in DECSC/DECRC so the program's own cursor is where it left it.

What the overlay must never do is land inside something else: an escape
sequence the program split across two PTY reads, or a UTF-8 character split
the same way. Bytes injected there corrupt the sequence and put its tail on
the screen as text. :class:`SequenceTracker` follows the stream's state so a
draw is deferred to the next chunk that ends on a clean boundary.

Known limits, by design: a program holding a saved cursor (DECSC) across the
injection point gets ours instead; a program in origin mode (DECOM) with a
scroll region that excludes row 1 sees the line drawn at the region's top.
Both are rare in the harnesses this daemon runs and both self-heal on the
program's next repaint.
"""

from __future__ import annotations

import dataclasses
import itertools
import unicodedata
from typing import Optional

#: How long a notice stays up when the sender did not say.
DEFAULT_TTL = 6.0
#: The longest a sender may ask for — a notice is a toast, not a status bar.
MAX_TTL = 60.0

#: Row-1 styling per level: bold on a coloured ground, readable on any theme.
_STYLES = {
    "info": "\x1b[0;1;37;44m",
    "warn": "\x1b[0;1;30;43m",
    "error": "\x1b[0;1;97;41m",
}
LEVELS = tuple(_STYLES)

_ids = itertools.count(1)


@dataclasses.dataclass
class Notice:
    text: str
    ttl: float = DEFAULT_TTL
    level: str = "info"
    id: int = dataclasses.field(default_factory=lambda: next(_ids))

    @classmethod
    def make(cls, text: str, ttl: Optional[float] = None, level: str = "info") -> "Notice":
        """A notice with its fields checked and clamped — the one constructor
        every door (Session.notify, the API, a viewer's control frame) uses,
        so a bad ttl or level cannot reach a draw."""
        text = " ".join(str(text).split())  # one line: fold whitespace/newlines
        if ttl is None:
            ttl = DEFAULT_TTL
        ttl = max(0.5, min(float(ttl), MAX_TTL))
        if level not in _STYLES:
            level = "info"
        return cls(text=text, ttl=ttl, level=level)

    def frame(self) -> dict:
        """The control-frame body every viewer gets."""
        return {
            "type": "notice",
            "id": self.id,
            "text": self.text,
            "ttl": self.ttl,
            "level": self.level,
        }


# --------------------------------------------------------------------------- #
# fitting text to a row
# --------------------------------------------------------------------------- #
def cell_width(ch: str) -> int:
    """Terminal cells one character takes: 2 for East Asian wide/fullwidth
    (Hangul, CJK, most emoji), 0 for combining marks, else 1."""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def fit(text: str, cols: int) -> str:
    """``text`` cut to at most ``cols`` cells and padded to exactly that many,
    so the styled row reads as a bar and hides whatever was under it."""
    out = []
    used = 0
    for ch in text:
        w = cell_width(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * (cols - used)


# --------------------------------------------------------------------------- #
# where the byte stream is
# --------------------------------------------------------------------------- #
_GROUND, _ESC, _ESC_INT, _CSI, _STR, _STR_ESC = range(6)


class SequenceTracker:
    """Follows a terminal byte stream far enough to say whether it currently
    sits between sequences and between characters.

    Fed every chunk the viewer is sent, in order. :attr:`clean` is True when
    the last byte seen ended a complete sequence or a complete character —
    the only place where more bytes may be inserted without corrupting
    something the program wrote. The parse is deliberately shallow: it knows
    the sequence *shapes* (CSI's final byte, the string-terminated OSC/DCS/
    APC/PM/SOS family, two-byte escapes with their intermediates) and nothing
    about what they mean.
    """

    def __init__(self) -> None:
        self._state = _GROUND
        self._utf8_clean = True

    @property
    def clean(self) -> bool:
        return self._state == _GROUND and self._utf8_clean

    def feed(self, data: bytes) -> bool:
        i = 0
        n = len(data)
        state = self._state
        while i < n:
            if state == _GROUND:
                j = data.find(0x1B, i)
                if j < 0:
                    break
                i = j + 1
                state = _ESC
            elif state == _ESC:
                c = data[i]
                i += 1
                if c == 0x5B:  # [
                    state = _CSI
                elif c in (0x5D, 0x50, 0x5F, 0x5E, 0x58):  # ] P _ ^ X
                    state = _STR
                elif 0x20 <= c <= 0x2F:
                    state = _ESC_INT
                elif c == 0x1B:
                    state = _ESC
                else:
                    state = _GROUND
            elif state == _ESC_INT:
                c = data[i]
                i += 1
                if not 0x20 <= c <= 0x2F:
                    state = _GROUND
            elif state == _CSI:
                while i < n and not 0x40 <= data[i] <= 0x7E:
                    i += 1
                if i < n:
                    i += 1
                    state = _GROUND
            elif state == _STR:
                bel = data.find(0x07, i)
                esc = data.find(0x1B, i)
                if bel < 0 and esc < 0:
                    i = n
                elif esc < 0 or (0 <= bel < esc):
                    i = bel + 1
                    state = _GROUND
                else:
                    i = esc + 1
                    state = _STR_ESC
            else:  # _STR_ESC
                c = data[i]
                i += 1
                if c == 0x5C:  # backslash: ST
                    state = _GROUND
                elif c != 0x1B:
                    state = _STR
        self._state = state
        self._utf8_clean = not _ends_mid_character(data)
        return self.clean


def _ends_mid_character(data: bytes) -> bool:
    """Whether ``data`` stops partway through a UTF-8 multi-byte character."""
    for back in range(1, min(3, len(data)) + 1):
        b = data[-back]
        if b < 0x80:
            return False
        if b >= 0xC0:
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return back < need
    return False


# --------------------------------------------------------------------------- #
# composing the bytes
# --------------------------------------------------------------------------- #
_SAVE, _RESTORE = "\x1b7", "\x1b8"
_HOME = "\x1b[1;1H"
_RESET = "\x1b[0m"


class Overlay:
    """One viewer's overlay state: the notice up (if any) and the tracker
    that says whether the stream is at a place it may be drawn."""

    def __init__(self) -> None:
        self.notice: Optional[Notice] = None
        self.tracker = SequenceTracker()
        #: A draw was due but the stream was mid-sequence; do it on the next
        #: clean chunk.
        self.pending = False

    def after_output(self, data: bytes, cols: int) -> bytes:
        """What to append to output chunk ``data`` on its way to the viewer:
        the notice redrawn (the program may have painted over row 1), or
        nothing when there is no notice or the chunk ends mid-sequence."""
        clean = self.tracker.feed(data)
        if self.notice is None:
            return b""
        if not clean:
            self.pending = True
            return b""
        self.pending = False
        return self.draw(cols)

    def draw(self, cols: int) -> bytes:
        """The notice as a styled bar on row 1, cursor left where it was."""
        if self.notice is None:
            return b""
        style = _STYLES.get(self.notice.level, _STYLES["info"])
        return (
            _SAVE + _HOME + style + fit(self.notice.text, cols) + _RESET + _RESTORE
        ).encode("utf-8")

    def show(self, notice: Notice, cols: int) -> bytes:
        """Put ``notice`` up (replacing any current one); the bytes to send
        now, empty if the stream is mid-sequence (the next chunk draws it)."""
        self.notice = notice
        if not self.tracker.clean:
            self.pending = True
            return b""
        self.pending = False
        return self.draw(cols)

    def clear(self, row_restore: bytes) -> bytes:
        """Take the notice down: the caller passes row 1 as the grid has it
        (:meth:`ScreenState.row_sequence`), wrapped here in the same
        cursor save/restore the draw used."""
        self.notice = None
        self.pending = False
        if not self.tracker.clean:
            # Mid-sequence: restoring now would corrupt the program's bytes
            # the same way drawing would. The program's next repaint of row 1
            # (or the viewer's next repaint) wins; leave the row as it is.
            return b""
        return _SAVE.encode() + row_restore + _RESTORE.encode()
