"""Idle/busy classification from screen-content samples.

Raw output quiescence is the obvious signal, but claude's TUI animates a
spinner and an elapsed-time counter even while "waiting for the user", so raw
bytes never go quiet. Instead the session samples the rendered screen's
per-row hashes on a fixed cadence and this tracker classifies rows that flap
on most samples as *animated* (spinner/clock rows) — the session is idle when
no **non-animated** row has changed for the caller's threshold.

Pure logic, no asyncio or I/O, so it is directly unit-testable with synthetic
hash sequences.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Set, Tuple

#: How far back :meth:`IdleTracker.moved_rows` sums. A minute is long enough
#: that one repaint does not decide the reading and short enough that a turn
#: which stopped a minute ago no longer counts as work.
MOVED_WINDOW = 60.0

#: A sample that changed at most this many rows may be a spinner or a clock
#: repainting, so its animated rows are left out of :meth:`moved_rows`. A
#: sample that changed more is content: a streaming reply scrolls every row
#: on every sample, which is exactly the flapping that classifies rows as
#: animated, and dropping them would rank the busiest screen as the quietest.
ANIMATION_ROWS = 3


class IdleTracker:
    """Feed periodic ``line_hashes`` samples; ask when content last changed.

    ``window``/``flap_threshold``: a row that changed in at least
    ``flap_threshold`` of the last ``window`` samples counts as animated and is
    ignored when deciding whether "meaningful" content changed.
    """

    def __init__(self, window: int = 5, flap_threshold: int = 3) -> None:
        self._window = window
        self._flap_threshold = flap_threshold
        self._prev: Optional[Tuple[int, ...]] = None
        self._changes: Deque[Set[int]] = deque(maxlen=window)
        self._last_meaningful: Optional[float] = None
        #: Monotonic time each row was last observed to change, so a caller
        #: can ask "did *this* row move recently" — the question a frozen
        #: in-turn marker poses. Animated rows count here even though they
        #: are excluded from "meaningful": a live spinner IS the activity the
        #: marker claims.
        self._last_change_at: dict = {}
        #: ``(monotonic time, rows)`` for each sample in which rows moved
        #: (see :data:`ANIMATION_ROWS`), kept for :data:`MOVED_WINDOW` so a
        #: caller can ask how MUCH the screen moved lately, not only when it
        #: last did.
        self._moved: Deque[Tuple[float, int]] = deque()

    def _animated_rows(self) -> Set[int]:
        counts: dict = {}
        for changed in self._changes:
            for row in changed:
                counts[row] = counts.get(row, 0) + 1
        return {row for row, n in counts.items() if n >= self._flap_threshold}

    def sample(self, hashes: Tuple[int, ...], now: float) -> None:
        """Record one screen sample taken at time ``now`` (monotonic seconds)."""
        if self._prev is None:
            # First observation: everything is new content.
            self._prev = hashes
            self._last_meaningful = now
            for i in range(len(hashes)):
                self._last_change_at[i] = now
            return
        if len(hashes) != len(self._prev):
            # Resize / row-count change: treat as activity and reset history.
            self._changes.clear()
            self._last_change_at = {i: now for i in range(len(hashes))}
            self._prev = hashes
            self._last_meaningful = now
            return
        changed = {i for i, h in enumerate(hashes) if h != self._prev[i]}
        animated = self._animated_rows()
        self._changes.append(changed)
        self._prev = hashes
        for i in changed:
            self._last_change_at[i] = now
        meaningful = changed - animated
        if meaningful:
            self._last_meaningful = now
        moved = len(changed) if len(changed) > ANIMATION_ROWS else len(meaningful)
        if moved:
            self._moved.append((now, moved))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._moved and now - self._moved[0][0] > MOVED_WINDOW:
            self._moved.popleft()

    def moved_rows(self, now: float, window: float = MOVED_WINDOW) -> int:
        """Rows that moved in the last ``window`` seconds.

        Summed per sample, so a row rewritten on three samples counts three
        times: the reading is how much the screen moved, a rate, and a
        streaming reply that keeps rewriting its last lines is exactly the
        work it should rank high. A spinner or clock repaint does not count
        (:data:`ANIMATION_ROWS`), and neither does a resize or the first
        sample — those restamp the whole grid without the session doing
        anything. ``window`` beyond
        :data:`MOVED_WINDOW` reads no further back than that.
        """
        return sum(n for at, n in self._moved if now - at <= window)

    def last_meaningful_change(self) -> Optional[float]:
        """When (monotonic) non-animated content last changed; None before any sample."""
        return self._last_meaningful

    def idle_for(self, now: float) -> Optional[float]:
        """Seconds since the last meaningful change (None before any sample)."""
        if self._last_meaningful is None:
            return None
        return max(0.0, now - self._last_meaningful)

    def last_change_at(self, row: int) -> Optional[float]:
        """Monotonic time ``row`` last changed (None if never sampled).

        A live turn-marker row moves; a frozen one does not. This is the
        evidence the status computation uses to tell a genuine "esc to
        interrupt" from one a crashed TUI left painted on the grid.
        """
        return self._last_change_at.get(row)
