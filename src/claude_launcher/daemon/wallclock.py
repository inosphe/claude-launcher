"""Monotonic stamps as wall-clock epoch seconds that hold still between reads.

The clocks that type into sessions (:mod:`.cflow_clock`,
:mod:`.session_reminder`) keep ``time.monotonic()`` stamps, and the dashboard
used to receive them as ages: ``due_in`` and ``fired_ago``, seconds from the
instant of the request. Those numbers move on every poll whether or not
anything happened, so no two answers were ever the same bytes -- the sessions
and cflow rails measured 0/5 identical at five-second spacing (claunch-4thlk).
An absolute time does not move until the timer itself does, and the page
computes the remaining seconds against its own clock, the way it already
does for ``last_activity_at`` and the term timer rows' ``due``.

The conversion needs the offset between the two clocks. Measured afresh on
each read it jitters by the clocks' resolution (about 16 ms on Windows), and
that alone would flip the rounded value now and then; so the offset is taken
once and kept, and re-taken only when the wall clock has really moved
against the monotonic one (an NTP step, a manual change) by more than
:data:`DRIFT` seconds.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

#: How far the measured offset may wander before the kept one is replaced.
#: Well above the clocks' jitter, well under anything a countdown shows.
DRIFT = 1.0

#: Decimal places of the published value. One tenth of a second is finer
#: than any countdown on the page and coarse enough that float noise from
#: re-deriving a stamp out of an age never crosses a rounding edge in
#: practice.
PLACES = 1

_lock = threading.Lock()
_offset: Optional[float] = None


def offset() -> float:
    """``time.time() - time.monotonic()``, held steady within :data:`DRIFT`."""
    global _offset
    measured = time.time() - time.monotonic()
    with _lock:
        if _offset is None or abs(measured - _offset) > DRIFT:
            _offset = measured
        return _offset


def wall(stamp: Optional[float]) -> Optional[float]:
    """Epoch seconds for a ``time.monotonic()`` stamp, or ``None`` for none."""
    if stamp is None:
        return None
    return round(stamp + offset(), PLACES)
