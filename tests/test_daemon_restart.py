"""The web UI's daemon restart: one POST, and the daemon succeeds itself.

Two halves, tested separately because they run in different lifetimes.
``POST /api/daemon/restart`` happens while the daemon serves: it must mark
the intent and trip the ordinary shutdown path — nothing more, so every
teardown guarantee shutdown already has (client notice, session drain,
daemon.json removal) is inherited rather than re-implemented. The spawn
happens after the loop is gone: ``main`` reads the intent off ``_serve``'s
return value and starts the successor only once the singleton lock is
released, so the new daemon finds it free instead of waiting out the grace
window.
"""

from __future__ import annotations

import asyncio
import time

from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


# --------------------------------------------------------------------------- #
# the endpoint: mark intent, trip the ordinary shutdown
# --------------------------------------------------------------------------- #
def test_restart_marks_intent_and_trips_the_shutdown_event(home):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/restart", headers={"Authorization": "Bearer sekrit"}
            )
            assert resp.status == 200
            assert (await resp.json())["restarting"] is True
            assert app["restart_requested"] is True
            # The event trips a beat later (the reply must get out first).
            await asyncio.wait_for(app["shutdown_event"].wait(), timeout=2.0)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_restart_needs_a_credential(home):
    """State-changing like every other endpoint: no token, no restart."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/daemon/restart")
            assert resp.status == 401
            assert app["restart_requested"] is False
            assert not app["shutdown_event"].is_set()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_plain_shutdown_carries_no_restart_intent(home):
    """The two endpoints share the event; only one of them means 'come back'."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/daemon/shutdown", headers={"Authorization": "Bearer sekrit"}
            )
            assert resp.status == 200
            await asyncio.wait_for(app["shutdown_event"].wait(), timeout=2.0)
            assert app["restart_requested"] is False
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the handoff: main() spawns the successor, and only for a restart
# --------------------------------------------------------------------------- #
def test_a_restart_exit_spawns_the_successor(home, monkeypatch):
    spawned = []

    async def fake_serve(host, port, cfg):
        return daemon_main.RESTART_CODE

    monkeypatch.setattr(daemon_main, "_serve", fake_serve)
    monkeypatch.setattr(
        daemon_main.daemon_client, "spawn_daemon", lambda: spawned.append(True)
    )
    assert daemon_main.main([]) == 0
    assert spawned == [True]


def test_a_plain_exit_spawns_nothing(home, monkeypatch):
    spawned = []

    async def fake_serve(host, port, cfg):
        return 0

    monkeypatch.setattr(daemon_main, "_serve", fake_serve)
    monkeypatch.setattr(
        daemon_main.daemon_client, "spawn_daemon", lambda: spawned.append(True)
    )
    assert daemon_main.main([]) == 0
    assert spawned == []
