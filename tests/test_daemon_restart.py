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
import os
import time

from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import paths
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
def _drive_main(monkeypatch, code: int, *, bound_port: int = 0) -> list:
    """Run ``main`` over a stubbed serve loop, recording successor spawns.

    Logging is stubbed out along with it: ``main`` would otherwise attach a
    root-logger FileHandler to this test's temp home and leave it open for
    the rest of the session — a handle into a directory pytest is about to
    delete, and one every later test would keep writing into.
    """
    spawned = []

    async def fake_serve(host, port, cfg, bound=None):
        if bound is not None and bound_port:
            bound["port"] = bound_port
        return code

    monkeypatch.setattr(daemon_main, "_setup_logging", lambda foreground: None)
    monkeypatch.setattr(daemon_main, "_serve", fake_serve)
    monkeypatch.setattr(
        daemon_main.daemon_client, "spawn_daemon", lambda env=None: spawned.append(env)
    )
    assert daemon_main.main([]) == 0
    return spawned


def test_a_restart_exit_spawns_the_successor(home, monkeypatch):
    assert len(_drive_main(monkeypatch, daemon_main.RESTART_CODE)) == 1


def test_a_plain_exit_spawns_nothing(home, monkeypatch):
    assert _drive_main(monkeypatch, 0) == []


# --------------------------------------------------------------------------- #
# the address: a restart the browser can follow
# --------------------------------------------------------------------------- #
def test_a_named_instance_hands_its_port_to_the_successor(home, monkeypatch):
    """An instance binds an ephemeral port, so nothing but this would put the
    successor back on the address the page that asked for the restart is on.

    Asserted on the environment handed to the spawn, never on this process's
    own: the pin belongs to the child, and a daemon that set it on itself on
    the way out would leave it behind for whatever shares that environment.
    """
    monkeypatch.setenv(paths.INSTANCE_ENV, "inst")
    monkeypatch.delenv("CLAUNCH_DAEMON_PORT", raising=False)

    spawned = _drive_main(monkeypatch, daemon_main.RESTART_CODE, bound_port=45671)
    assert spawned[0]["CLAUNCH_DAEMON_PORT"] == "45671"
    assert "CLAUNCH_DAEMON_PORT" not in os.environ


def test_a_pinned_port_is_left_alone(home, monkeypatch):
    """Already pinned: the successor inherits that pin as-is, so there is
    nothing for this to override — it hands over no environment at all."""
    monkeypatch.setenv(paths.INSTANCE_ENV, "inst")
    monkeypatch.setenv("CLAUNCH_DAEMON_PORT", "9999")

    spawned = _drive_main(monkeypatch, daemon_main.RESTART_CODE, bound_port=45671)
    assert spawned == [None]
    assert os.environ["CLAUNCH_DAEMON_PORT"] == "9999"


def test_the_default_daemon_pins_nothing(home, monkeypatch):
    """Its port is fixed in the config; the successor rebinds it by itself,
    and an env pin would outlive a later config edit."""
    monkeypatch.delenv(paths.INSTANCE_ENV, raising=False)
    monkeypatch.delenv("CLAUNCH_DAEMON_PORT", raising=False)

    assert _drive_main(monkeypatch, daemon_main.RESTART_CODE, bound_port=45671) == [None]
    assert "CLAUNCH_DAEMON_PORT" not in os.environ


# --------------------------------------------------------------------------- #
# the lock is free before the loop's teardown joins the executor
# --------------------------------------------------------------------------- #
def test_the_lock_is_released_while_an_executor_thread_is_still_blocked(
    home, monkeypatch
):
    """A worker stuck in the default executor must not keep the lock held.

    The cflow RestartClock runs the project's restart command (here,
    ``tools/restart_live.ps1`` -> ``claunch daemon restart``) in the default
    executor. That subprocess cannot finish until a successor daemon
    answers, and the successor cannot start while this process holds the
    singleton lock — so releasing only after asyncio's executor join (the
    ``asyncio.run`` teardown) is a deadlock of three parties, seen live on
    2026-09-11 and broken only by the 300s THREAD_JOIN_TIMEOUT. ``main``
    must release the lock, and spawn the successor, with that thread still
    blocked.
    """
    import threading

    from claude_launcher.daemon import runtime_state

    unblock = threading.Event()
    spawned: list = []

    async def fake_serve(host, port, cfg, bound=None):
        loop = asyncio.get_running_loop()
        # Fire-and-forget into the default executor, exactly like a clock
        # tick that asyncio.to_thread()'d a subprocess.run() and was then
        # cancelled by shutdown: the future is dropped, the thread lives on.
        loop.run_in_executor(None, unblock.wait)
        await asyncio.sleep(0.05)
        return daemon_main.RESTART_CODE

    monkeypatch.setattr(daemon_main, "_setup_logging", lambda foreground: None)
    monkeypatch.setattr(daemon_main, "_serve", fake_serve)
    monkeypatch.setattr(
        daemon_main.daemon_client, "spawn_daemon", lambda env=None: spawned.append(env)
    )

    worker = threading.Thread(target=lambda: daemon_main.main([]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5.0
        probe = runtime_state.SingletonLock()
        while time.monotonic() < deadline:
            if spawned and probe.acquire():
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "main() kept the singleton lock (or withheld the successor) "
                "while the executor thread was blocked"
            )
        probe.release()
        assert not unblock.is_set()
    finally:
        unblock.set()
        worker.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# the listening socket comes back when asyncio closes it
# --------------------------------------------------------------------------- #
def test_the_listener_is_reopened_after_asyncio_closes_it(monkeypatch):
    """Windows' proactor closes the *listening* socket when one accept fails
    (WinError 64 from a client that gave up mid-handshake). The process and
    its sessions live on, nothing listens -- 2026-09-11 14:27. The watchdog
    must notice and start a new site on the same port."""
    from aiohttp import web, ClientSession

    monkeypatch.setattr(daemon_main, "LISTENER_POLL", 0.05)

    async def run():
        app = web.Application()
        app.router.add_get("/ping", lambda r: web.Response(text="pong"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        box = {"site": site}
        watchdog = asyncio.ensure_future(
            daemon_main._listener_watchdog(box, runner, "127.0.0.1", port)
        )
        try:
            site._server.close()                    # what the proactor does
            await asyncio.sleep(0)
            assert not daemon_main._listener_alive(site)
            for _ in range(100):                    # up to ~5 s
                await asyncio.sleep(0.05)
                if box["site"] is not site and daemon_main._listener_alive(box["site"]):
                    break
            else:
                raise AssertionError("watchdog did not re-open the listener")
            async with ClientSession() as http:
                async with http.get(f"http://127.0.0.1:{port}/ping") as resp:
                    assert await resp.text() == "pong"
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            await runner.cleanup()

    asyncio.run(run())
