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
            return
        if len(hashes) != len(self._prev):
            # Resize / row-count change: treat as activity and reset history.
            self._changes.clear()
            self._prev = hashes
            self._last_meaningful = now
            return
        changed = {i for i, h in enumerate(hashes) if h != self._prev[i]}
        animated = self._animated_rows()
        self._changes.append(changed)
        self._prev = hashes
        if changed - animated:
            self._last_meaningful = now

    def last_meaningful_change(self) -> Optional[float]:
        """When (monotonic) non-animated content last changed; None before any sample."""
        return self._last_meaningful

    def idle_for(self, now: float) -> Optional[float]:
        """Seconds since the last meaningful change (None before any sample)."""
        if self._last_meaningful is None:
            return None
        return max(0.0, now - self._last_meaningful)
