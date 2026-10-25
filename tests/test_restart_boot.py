"""A restart's two halves: the teardown's sockets, and the boot's restore.

claunch-9akgh measured a keep restart on 2026-10-21 as 19.6s from "shutdown
requested" to "listening", and neither half was visible where a reader looks:

* ``runner.cleanup()`` took 6.1s and 8.5s in two of the restarts. Every app
  shutdown hook runs inside that one step, and ``_close_websockets`` awaited
  each socket's closing handshake in turn — aiohttp's default for that
  handshake is 10s per socket, so a single peer that had already gone held
  the whole teardown.
* the successor restored 33 sessions before binding its port (11.7s), so
  every client spent the restore looking at a daemon that answered nothing,
  and nothing said a boot was in progress.

These pin the fixes: closes are concurrent and bounded, a slow hook is named,
the restore yields to the loop between records, and the boot's phase is
readable from ``/api/health`` and ``/api/daemon``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager


# --------------------------------------------------------------------------- #
# teardown: concurrent, bounded websocket closes
# --------------------------------------------------------------------------- #
class _Socket:
    """A websocket that takes ``delay`` seconds to answer its close."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.close_started = 0

    async def close(self, **kwargs) -> None:
        self.close_started = time.monotonic()
        await asyncio.sleep(self.delay)


def test_websockets_close_concurrently():
    async def run():
        app = web.Application()
        sockets = [_Socket(0.3) for _ in range(4)]
        app["websockets"] = set(sockets)

        began = time.monotonic()
        await api.close_websockets(app)
        elapsed = time.monotonic() - began
        assert all(ws.close_started for ws in sockets)
        # Serial would be 4 x 0.3s = 1.2s; concurrent is one delay.
        assert elapsed < 0.8, elapsed

    asyncio.run(run())


def test_a_peer_that_never_answers_a_close_is_abandoned():
    async def run():
        app = web.Application()
        app["websockets"] = {_Socket(60.0), _Socket(60.0)}

        began = time.monotonic()
        await api.close_websockets(app)
        elapsed = time.monotonic() - began
        # The bound is WS_CLOSE_TIMEOUT, not aiohttp's 10s default per socket.
        assert elapsed < api.WS_CLOSE_TIMEOUT + 1.5, elapsed

    asyncio.run(run())


def test_a_slow_shutdown_hook_is_named():
    async def run():
        app = web.Application()
        seen = []

        async def slow(application):
            await asyncio.sleep(0.05)

        async def record(application):
            seen.append("record")

        app.on_shutdown.append(slow)
        app.on_shutdown.append(record)
        api._time_shutdown_hooks(app)
        # The wrapper keeps aiohttp's call shape and passes both hooks on.
        for hook in app.on_shutdown:
            await hook(app)
        assert seen == ["record"]
        assert [h.__name__ for h in app.on_shutdown] == ["slow", "record"]

    asyncio.run(run())


def test_notify_shutdown_says_whether_a_successor_is_coming():
    async def run():
        class Sock:
            def __init__(self):
                self.frames = []

            async def send_str(self, text):
                self.frames.append(json.loads(text))

        for requested, expected in ((True, True), (False, False)):
            app = web.Application()
            sock = Sock()
            app["websockets"] = {sock}
            app["restart_requested"] = requested
            await api.notify_shutdown(app)
            assert sock.frames == [{"type": "shutdown", "restart": expected}]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# boot: the restore yields and reports progress
# --------------------------------------------------------------------------- #
def test_restore_all_async_yields_between_records(home, monkeypatch):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
    # `was_running` is what the boot's total counts: the records it has to
    # bring back (the rest are kept as exited records in the same pass).
    entries = [{"name": f"s{i}", "was_running": True} for i in range(4)]
    monkeypatch.setattr(mgr._store, "load_all", lambda: entries)
    handled = []
    monkeypatch.setattr(mgr, "_restore_entry", lambda *a: handled.append(a[0]["name"]))
    monkeypatch.setattr(mgr, "persist", lambda: None)

    ticks = []
    progress = []

    async def ticker():
        for _ in range(4):
            ticks.append("tick")
            await asyncio.sleep(0)

    async def run():
        task = asyncio.create_task(ticker())
        failed = await mgr.restore_all_async(progress=lambda done, total: progress.append((done, total)))
        await task
        return failed

    assert asyncio.run(run()) == []
    assert handled == ["s0", "s1", "s2", "s3"]
    # The generator handed the loop back between records: without that the
    # ticker could not have run once, and the port bound above it would
    # answer nothing while the fleet came back.
    assert ticks
    assert progress[0] == (0, 4)
    assert progress[-1] == (4, 4)


def test_progress_counts_only_the_records_a_boot_brings_back(home, monkeypatch):
    """A registry full of exited records must not run the total over.

    Found in review (claunch-9akgh): `done` counted every record while
    `total` counted the ones being brought back. On this machine that is
    1551 records against 48, so the dashboard would have shown "restoring
    ... 1551/48". The only fixture in this file used `was_running` for every
    entry, which hid it.
    """
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
    entries = (
        [{"name": f"dead{i}"} for i in range(9)]
        + [{"name": f"live{i}", "was_running": True} for i in range(3)]
        + [{"name": "delayed0", "delayed_at": "2026-10-21T00:00:00Z"}]
    )
    monkeypatch.setattr(mgr._store, "load_all", lambda: entries)
    monkeypatch.setattr(mgr, "_restore_entry", lambda *a: None)
    monkeypatch.setattr(mgr, "persist", lambda: None)

    progress = []

    async def run():
        await mgr.restore_all_async(
            progress=lambda done, total: progress.append((done, total))
        )

    asyncio.run(run())
    totals = {total for _, total in progress}
    assert totals == {4}, progress  # was_running 3 + delayed 1
    assert progress[0] == (0, 4)
    assert progress[-1] == (4, 4)
    # Never past the total, never backwards.
    assert all(done <= total for done, total in progress)
    assert [done for done, _ in progress] == sorted(d for d, _ in progress)


def test_restore_all_still_runs_synchronously(home, monkeypatch):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
    monkeypatch.setattr(mgr._store, "load_all", lambda: [])
    monkeypatch.setattr(mgr, "persist", lambda: None)
    assert mgr.restore_all() == []


def test_both_restore_entry_points_start_from_a_clean_failure_list(home, monkeypatch):
    """A second pass must not inherit the first pass's failures.

    The boot only ever restores once, so this never showed in the daemon --
    but only ``restore_all`` reset ``_restore_failed``, and the asynchronous
    variant the boot actually runs did not. A process that restores twice
    returned the first pass's names from the second call (review of
    claunch-9akgh, claunch-9akgh.1).
    """
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
    monkeypatch.setattr(mgr._store, "load_all", lambda: [])
    monkeypatch.setattr(mgr, "persist", lambda: None)

    mgr._restore_failed = ["stale-from-an-earlier-sync-pass"]
    assert mgr.restore_all() == []

    mgr._restore_failed = ["stale-from-an-earlier-async-pass"]
    assert asyncio.run(mgr.restore_all_async()) == []


def test_the_port_is_bound_before_the_fleet_is_restored():
    """The boot's central claim, pinned by source order.

    Nothing else in this file fails if ``_serve`` moves the restore back
    above ``site.start()``: the health test fills ``app["boot"]`` by hand
    and the restore tests call the manager directly. Until this test the
    order rested on a hand-measured timeline alone (raised in review of
    claunch-9akgh, claunch-9akgh.1). A static check is what fits -- the
    claim is about the order of two statements in one function, and
    standing up a real daemon to observe it would test aiohttp more than
    this code (same shape as tests/test_web_app_declarations.py).
    """
    import claude_launcher.daemon.__main__ as daemon_main

    source = Path(daemon_main.__file__).read_text(encoding="utf-8")
    bound = source.index("await site.start()")
    restored = source.index("await manager.restore_all_async(")
    assert bound < restored, (
        "the port must answer before the fleet is restored: site.start() is "
        f"at offset {bound}, restore_all_async at {restored}"
    )


def test_the_boot_phase_is_set_before_the_startup_handlers_run():
    """The shims' first pass waits on this phase, so it must be set first.

    ``runner.setup()`` runs the app's ``on_startup`` handlers, and the shims
    register one: their first reconcile pass holds until
    ``app["boot"]["phase"]`` leaves ``"restoring"`` (claunch-esmr3). The
    dict that says so is written in ``_serve``, and ``build_app`` has already
    seeded the same key with ``"ready"`` -- so if the write sits *after*
    ``runner.setup()``, the waiting pass reads ``"ready"``, runs against the
    empty fleet, prunes the shim users of every session the PTY host kept
    and retires the shims those sessions are still talking to. They then
    hang on connect instead of being refused and retry timeouts forever.

    Pinned by source order for the same reason as
    :func:`test_the_port_is_bound_before_the_fleet_is_restored`: the claim
    is about the order of two statements, and standing up a real daemon to
    observe it would test aiohttp more than this code. Reverting the
    move leaves every other test here green (raised by the round's
    reviewer, s1812-rev).
    """
    import claude_launcher.daemon.__main__ as daemon_main

    source = Path(daemon_main.__file__).read_text(encoding="utf-8")
    seeded = source.index('app["boot"] = {')
    handlers = source.index("await runner.setup()")
    assert seeded < handlers, (
        "the boot phase must be set before the startup handlers run: "
        f'app["boot"] = {{}} is at offset {seeded}, runner.setup() at '
        f"{handlers}. After setup() the shims' first reconcile pass reads "
        "build_app's 'ready' default and runs against the empty fleet "
        "(claunch-esmr3)"
    )


def test_boot_status_defaults_to_ready():
    app = web.Application()
    assert api.boot_status(app) == {"phase": "ready", "done": 0, "total": 0}


def test_health_and_daemon_carry_the_boot_phase(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        app = build_app(mgr, "auth", started_at=time.monotonic())
        app["boot"].update(phase="restoring", done=2, total=5, started=time.monotonic())
        try:
            async with TestClient(TestServer(app)) as client:
                # /api/health is the unauthenticated probe the page leads
                # every tick with; a successor answers it while restoring.
                resp = await client.get("/api/health")
                assert resp.status == 200
                body = await resp.json()
                assert body["boot"]["phase"] == "restoring"
                assert (body["boot"]["done"], body["boot"]["total"]) == (2, 5)

                resp = await client.get(
                    "/api/daemon", headers={"Authorization": "Bearer auth"}
                )
                assert resp.status == 200
                assert (await resp.json())["boot"]["phase"] == "restoring"
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
