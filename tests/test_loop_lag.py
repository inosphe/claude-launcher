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


def test_a_wake_up_at_or_over_the_threshold_is_written_to_the_log(caplog):
    lag = LoopLag(log_threshold=1.0)
    with caplog.at_level("WARNING", logger="claunch.daemon.loop_lag"):
        lag.record(100.0, 0.999)
        assert not caplog.records
        lag.record(101.0, 2.5)
    assert len(caplog.records) == 1
    assert "event loop stalled" in caplog.text and "2.500s" in caplog.text


def test_a_zero_threshold_turns_the_log_off(caplog):
    lag = LoopLag(log_threshold=0)
    with caplog.at_level("WARNING", logger="claunch.daemon.loop_lag"):
        lag.record(100.0, 30.0)
    assert not caplog.records
