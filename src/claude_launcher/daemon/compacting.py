"""Detect a session's context compaction from its pty output.

A harness prints its own notice when it starts compacting the session's
context — claude paints, above a progress bar:

    ✽ Compacting conversation… (1m 23s · ↓ 156.2k tokens)
    ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 53%

…and repaints the pair while the compaction runs. :class:`Detector` scans
every pty chunk a session receives and turns a match into a ``compacting``
flag that survives on the session's dashboard row for a bounded window after
the last paint.

The per-harness notice table in :data:`HARNESS_NOTICES` is the extension
point: a harness that announces compaction in its own words becomes visible
on the rail by adding its pattern here and nothing else. The flag is
computed on the byte stream, not on the rendered screen: the screen cannot
tell a live notice apart from conversation text that merely contains the
phrase, while the stream pair (notice + progress bar, painted as one frame)
is painted only by the compaction UI itself.
"""

from __future__ import annotations

import re
import time
from typing import Dict, Optional, Pattern, Tuple

#: How long the flag lingers after the last notice paint when the notice
#: carries no self-declared elapsed time (older claude paints, or a harness
#: whose pattern captures no duration).
DEFAULT_WINDOW_S = 180.0

#: Added to the elapsed time the notice reports in its own parenthetical —
#: the "(1m 23s …)" is how long the compaction had already run when painted,
#: so the label must outlast the last paint by at least the piece that was
#: not yet elapsed, plus the frames a final repaint may skip.
WINDOW_SLACK_S = 60.0

#: How much already-scanned output is carried across chunks. Only enough to
#: bridge one chunk boundary — the notice and its progress bar land as one
#: contiguous paint, so a split needs at most a couple of lines.
TAIL_BYTES = 512

#: The control sequences between painted cells. CSI covers SGR/colour
#: (semicolon and colon parameter forms), cursor movement and erase ops; OSC
#: covers window titles (terminated by BEL or ST). ``\\r`` is dropped so the
#: two painted lines sit on ``\\n`` regardless of the terminal's newline mode.
_SCRUB = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;:?]*[A-Za-z]|\r"
)

#: A live claude compaction: the notice line, optionally a leading spinner
#: glyph and optionally the "(<n>m <n>s · ↓ <count> tokens)" parenthetical,
#: then (one frame later) the progress bar — a run of block cells plus a
#: percentage. Every observed live paint carries the bar; conversation text
#: that merely quotes the notice does not, which is what keeps rendered
#: content from pinning the label on. The frame is one contiguous paint, so
#: anything between the two lines is whitespace only.
_CLAUDE_LIVE = re.compile(
    r"(?:[✽✢✻✾✶✡·])? ?Compacting conversation[ \t]*(?:…|\.\.\.)"
    r"(?:[ \t]*\((?:(?P<minutes>\d+)m )?(?P<seconds>\d+)s[^\n)]*\))?"
    r"\n[ \t]*(?:▰|▱|█|░)[▱▰█░ \t]*\d{1,3}%"
)

#: Harness name -> notice patterns, in the order they should be tried.
#: Add a harness (and its compaction UI's paint shape) here to make its
#: sessions carry the flag; a pattern must capture ``minutes``/``seconds``
#: for the notice's own elapsed time to size the window.
HARNESS_NOTICES: Dict[str, Tuple[Pattern, ...]] = {
    "claude": (_CLAUDE_LIVE,),
}


def _window_from(match: re.Match) -> float:
    """Seconds the flag stays up for one matched notice frame."""
    minutes = match.groupdict().get("minutes")
    seconds = match.groupdict().get("seconds")
    if seconds:
        elapsed = (int(minutes or 0) * 60 + int(seconds)) if minutes else int(seconds)
        return float(elapsed) + WINDOW_SLACK_S
    return DEFAULT_WINDOW_S


class Detector:
    """Per-session scanner: feed pty chunks, read ``compacting``.

    Stateless in the sense that everything it remembers is the last
    ``TAIL_BYTES`` of raw output (to catch a paint split across chunk
    boundaries) and the monotonic deadline of the most recent notice. A
    restart rebuilds it empty, which is right: a compaction whose notice is
    only in the on-disk log is a compaction the dashboard is not live to
    anymore.
    """

    def __init__(self, harness: str) -> None:
        self._patterns = HARNESS_NOTICES.get(harness, ())
        self._tail = b""
        self._until = 0.0  # monotonic; 0.0 = no compaction seen

    def feed(self, chunk: bytes) -> None:
        """Scan one raw pty chunk; extend the flag when a notice lands."""
        if not chunk or not self._patterns:
            return
        scan = self._tail + chunk
        clean = _SCRUB.sub(b"", scan).decode("utf-8", "replace")
        for pattern in self._patterns:
            match = pattern.search(clean)
            if match:
                self._until = time.monotonic() + _window_from(match)
                break
        self._tail = scan[-TAIL_BYTES:]

    @property
    def compacting(self) -> bool:
        """True while a notice has been painted within its window."""
        return time.monotonic() < self._until
