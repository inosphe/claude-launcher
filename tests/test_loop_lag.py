"""The event loop lag monitor behind the page's latency badge (claunch-y9ax9)."""

from __future__ import annotations

import asyncio
import time

from claude_launcher.daemon.loop_lag import LoopLag


def test_nothing_is_claimed_before_the_first_wake_up():
    assert LoopLag().snapshot() == {"lag_ms": None, "max_ms": None, "window_s": 30.0}


def test_the_worst_delay_is_kept_for_the_window_and_then_dropped():
    lag = LoopLag(window=10.0)
    lag.record(100.0, 0.002)
    lag.record(101.0, 0.450)
    lag.record(102.0, 0.001)
    assert lag.snapshot() == {"lag_ms": 1.0, "max_ms": 450.0, "window_s": 10.0}
    lag.record(111.5, 0.003)  # 101.0 is now more than 10s old
    assert lag.snapshot()["max_ms"] == 3.0


def test_a_blocking_call_on_the_loop_shows_up_as_lag():
    async def go():
        lag = LoopLag(interval=0.01)
        lag.start()
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.2)  # a synchronous stretch holding the loop
            await asyncio.sleep(0.05)
            return lag.snapshot()
        finally:
            await lag.stop()

    snap = asyncio.run(go())
    assert snap["max_ms"] >= 150.0
