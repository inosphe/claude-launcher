"""Killed sessions archived by the daemon on its own (claunch-wz6a5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import auto_archive
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def add_dead(mgr: SessionManager, name: str, *, exited_days=None, **fields) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd="C:/work"),
        exit_code=0,
        exited_at=ago(exited_days) if exited_days is not None else None,
        **fields,
    )
    mgr._sessions[name] = record
    return record


def test_only_killed_records_ended_at_least_the_threshold_ago_are_due(home):
    mgr = manager()
    add_dead(mgr, "old", exited_days=8)
    add_dead(mgr, "edge", exited_days=7)
    add_dead(mgr, "young", exited_days=6.9)
    add_dead(mgr, "paused", exited_days=30, paused_at=ago(30))
    add_dead(mgr, "held", exited_days=30, held_at=ago(30))
    add_dead(mgr, "failed", exited_days=30, failed_at=ago(30))
    add_dead(mgr, "filed", exited_days=30, archived_at=ago(20))

    assert auto_archive.due(mgr.list(), days=7, now=NOW) == ["old", "edge"]


def test_a_record_without_exited_at_falls_back_to_its_last_output(home):
    mgr = manager()
    add_dead(mgr, "legacy", last_output_at=ago(10), created_at=ago(12))
    add_dead(mgr, "recent", last_output_at=ago(1), created_at=ago(12))

    assert auto_archive.due(mgr.list(), days=7, now=NOW) == ["legacy"]


def test_a_sweep_archives_at_most_one_batch_oldest_first_and_drains_the_backlog(home):
    mgr = manager()
    for i in range(5):
        add_dead(mgr, f"k{i}", exited_days=10 + i)  # k4 is the oldest
    add_dead(mgr, "young", exited_days=1)

    first = auto_archive.sweep_once(mgr, days=7, batch=2, now=NOW)
    assert first == {"archived": ["k4", "k3"], "failed": [], "remaining": 3}
    second = auto_archive.sweep_once(mgr, days=7, batch=2, now=NOW)
    assert second["archived"] == ["k2", "k1"] and second["remaining"] == 1
    third = auto_archive.sweep_once(mgr, days=7, batch=2, now=NOW)
    assert third == {"archived": ["k0"], "failed": [], "remaining": 0}
    assert auto_archive.sweep_once(mgr, days=7, batch=2, now=NOW)["archived"] == []

    assert all(mgr.get(f"k{i}").archived_at for i in range(5))
    assert mgr.get("young").archived_at is None


def test_the_archive_event_names_the_clock(home):
    mgr = manager()
    record = add_dead(mgr, "old", exited_days=9)

    auto_archive.sweep_once(mgr, days=7, batch=5, now=NOW)

    events = [e for e in mgr.events.rows(record) if e["kind"] == "archive"]
    assert events and events[-1]["details"] == {"reason": "auto-archive"}


def test_days_or_batch_zero_turns_the_sweep_off(home):
    mgr = manager()
    add_dead(mgr, "old", exited_days=30)

    assert auto_archive.sweep_once(mgr, days=0, batch=5, now=NOW)["archived"] == []
    assert auto_archive.sweep_once(mgr, days=7, batch=0, now=NOW)["archived"] == []
    assert mgr.get("old").archived_at is None


def test_the_tick_reads_the_config_live(home):
    mgr = manager()
    for i in range(3):
        add_dead(mgr, f"k{i}", exited_days=10 + i)
    clock = auto_archive.AutoArchiver(mgr)

    assert store.daemon_config()["auto_archive_killed_days"] == 7
    store.set_daemon_field("auto_archive_batch", 1)
    assert clock.tick(now=NOW)["archived"] == ["k2"]
    store.set_daemon_field("auto_archive_killed_days", 11)
    store.set_daemon_field("auto_archive_batch", 5)
    result = clock.tick(now=NOW)
    assert result["archived"] == ["k1"] and result["remaining"] == 0
    assert clock.last["archived"] == ["k1"]
    assert mgr.get("k0").archived_at is None


def test_a_bad_config_value_falls_back_to_the_default(home):
    days, interval, batch = auto_archive.settings(
        {"auto_archive_killed_days": "x", "auto_archive_interval_s": -5,
         "auto_archive_batch": None}
    )
    assert (days, interval, batch) == (auto_archive.DAYS, 0.0, auto_archive.BATCH)


async def _until(cond, *, tries=500) -> bool:
    for _ in range(tries):
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def test_the_loop_waits_an_interval_then_archives_on_its_own(home):
    mgr = manager()
    add_dead(mgr, "old", exited_days=400)  # well past the cutoff at any real now
    store.set_daemon_field("auto_archive_interval_s", 0.01)
    clock = auto_archive.AutoArchiver(mgr)

    async def run():
        clock.start()
        try:
            return await _until(lambda: mgr.get("old").archived_at is not None)
        finally:
            await clock.stop()

    assert asyncio.run(run())
    assert clock.last["archived"] == ["old"]
    assert clock._task is None


def test_a_failing_tick_does_not_end_the_loop(home, monkeypatch):
    mgr = manager()
    store.set_daemon_field("auto_archive_interval_s", 0.01)
    clock = auto_archive.AutoArchiver(mgr)
    calls = []

    def tick(now=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"archived": [], "failed": [], "remaining": 0}

    monkeypatch.setattr(clock, "tick", tick)

    async def run():
        clock.start()
        try:
            return await _until(lambda: len(calls) >= 2)
        finally:
            await clock.stop()

    assert asyncio.run(run())


def test_interval_zero_idles_and_rereads_the_config(home, monkeypatch):
    mgr = manager()
    add_dead(mgr, "old", exited_days=400)
    store.set_daemon_field("auto_archive_interval_s", 0)
    monkeypatch.setattr(auto_archive, "IDLE_RECHECK", 0.01)
    clock = auto_archive.AutoArchiver(mgr)

    async def run():
        clock.start()
        try:
            await asyncio.sleep(0.1)
            idle = mgr.get("old").archived_at is None
            store.set_daemon_field("auto_archive_interval_s", 0.01)
            turned_on = await _until(lambda: mgr.get("old").archived_at is not None)
            return idle, turned_on
        finally:
            await clock.stop()

    assert asyncio.run(run()) == (True, True)


def test_install_starts_the_clock_with_the_app_and_stops_it_on_shutdown(home):
    mgr = manager()
    store.set_daemon_field("auto_archive_interval_s", 600)

    async def run():
        app = web.Application()
        archiver = auto_archive.install(app, mgr)
        assert app["auto_archive"] is archiver and archiver._task is None
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            task = archiver._task
            running = task is not None and not task.done()
        finally:
            await client.close()
        return running, task, archiver._task

    running, task, after = asyncio.run(run())
    assert running
    assert task.cancelled() and after is None
