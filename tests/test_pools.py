"""The daemon's two thread pools (claunch-d18v8)."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from claude_launcher.daemon import pools


@pytest.fixture(autouse=True)
def _no_pool_left_behind():
    yield
    pools.shutdown()


def test_sizes_follow_the_core_count_unless_configured(monkeypatch):
    monkeypatch.setattr(pools.os, "cpu_count", lambda: 24)
    assert pools.interactive_size() == 28
    assert pools.background_size() == 6
    monkeypatch.setattr(pools.os, "cpu_count", lambda: 2)
    assert pools.interactive_size() == 6
    assert pools.background_size() == 2
    monkeypatch.setattr(pools.os, "cpu_count", lambda: 128)
    assert pools.interactive_size() == 32
    assert pools.background_size() == 8
    assert pools.interactive_size(5) == 5
    assert pools.background_size(3) == 3


def test_each_pool_runs_on_its_own_named_threads():
    async def main():
        sizes = pools.install(asyncio.get_running_loop(), {})
        inter = await asyncio.to_thread(lambda: threading.current_thread().name)
        back = await pools.background(lambda: threading.current_thread().name)
        return sizes, inter, back

    sizes, inter, back = asyncio.run(main())
    assert sizes == {"interactive": pools.interactive_size(), "background": pools.background_size()}
    assert inter.startswith("interactive")
    assert back.startswith("background")


def test_a_saturated_background_pool_does_not_delay_the_interactive_one():
    release = threading.Event()

    async def main():
        loop = asyncio.get_running_loop()
        pools.install(loop, {"thread_pool_background": 2})
        started = [threading.Event(), threading.Event()]
        busy = [
            asyncio.create_task(pools.background(lambda e=e: (e.set(), release.wait(10))))
            for e in started
        ]
        for e in started:
            assert await loop.run_in_executor(None, e.wait, 5)
        # Both background workers are held; a third background job queues ...
        queued = asyncio.create_task(pools.background(lambda: "late"))
        await asyncio.sleep(0.05)
        assert not queued.done()
        # ... while interactive work answers at once.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "now"), 2) == "now"
        release.set()
        await asyncio.gather(*busy)
        return await queued

    try:
        assert asyncio.run(main()) == "late"
    finally:
        release.set()


def test_background_carries_context_variables():
    var = contextvars.ContextVar("v", default="unset")

    async def main():
        pools.install(asyncio.get_running_loop(), {})
        var.set("kept")
        return await pools.background(var.get)

    assert asyncio.run(main()) == "kept"


def test_background_without_an_installed_pool_still_runs():
    async def main():
        return await pools.background(lambda a, b=0: a + b, 1, b=2)

    assert asyncio.run(main()) == 3


def test_background_exceptions_reach_the_caller():
    async def main():
        pools.install(asyncio.get_running_loop(), {})
        await pools.background(int, "x")

    with pytest.raises(ValueError):
        asyncio.run(main())
