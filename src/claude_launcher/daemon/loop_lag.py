"""How late the daemon's event loop runs its callbacks (claunch-y9ax9).

Every HTTP request, terminal socket and control socket shares one event
loop, so a synchronous stretch anywhere in the daemon (a log walk, a process
spawn, a JSON dump) delays all of them at once. The web page's latency badge
measures a round trip; this says how much of that round trip was the loop
being busy rather than the network, which is the number a fix to the daemon
is judged by.

The measure is the standard one: a task asks to wake every ``interval``
seconds and records how far past the deadline it actually woke. Nothing can
run on the loop while another callback holds it, so the overshoot is the
longest stall that ended in that interval.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import time
from typing import Callable, Deque, Optional, Tuple

#: Seconds between wake-ups. Short enough to catch a stall a person notices
#: (100 ms), cheap enough to be nothing (ten timer callbacks a second).
INTERVAL = 0.1
#: Seconds of history the ``max_ms`` answer covers. The web page asks every
#: 5 seconds; stalls seen on the live daemon came about every 10 seconds, so
#: a window shorter than that would read a busy loop as idle between them.
WINDOW = 30.0


class LoopLag:
    def __init__(
        self,
        *,
        interval: float = INTERVAL,
        window: float = WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = interval
        self.window = window
        self._clock = clock
        #: (when the wake-up happened, how late it was in seconds)
        self._samples: Deque[Tuple[float, float]] = collections.deque()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="loop-lag"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            due = self._clock() + self.interval
            await asyncio.sleep(self.interval)
            now = self._clock()
            self.record(now, max(0.0, now - due))

    def record(self, now: float, late: float) -> None:
        self._samples.append((now, late))
        horizon = now - self.window
        while self._samples and self._samples[0][0] < horizon:
            self._samples.popleft()

    def snapshot(self) -> dict:
        """``lag_ms``: the last wake-up's delay. ``max_ms``: the worst in the
        last ``window_s`` seconds. Both None before the first wake-up."""
        if not self._samples:
            return {"lag_ms": None, "max_ms": None, "window_s": self.window}
        return {
            "lag_ms": round(self._samples[-1][1] * 1000, 1),
            "max_ms": round(max(late for _, late in self._samples) * 1000, 1),
            "window_s": self.window,
        }
