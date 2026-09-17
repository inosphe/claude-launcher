"""End-to-end: a real PTY child driven through the SessionManager and the API.

Uses a tiny Python echo program as the harness so the tests run anywhere the
package installs — no claude needed. Exercises the reader-thread pump, pyte
capture, send-keys, wait-for and the aiohttp surface (auth included).
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from claude_launcher import credentials, lineage, profile, store
from claude_launcher.daemon import codex_sessions, db, manager as manager_mod, paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.session import SessionGone

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line == 'quit':\n"
    "        print('BYE')\n"
    "        break\n"
    # Stand-in for a TUI taking (and releasing) the mouse the way claude does:
    # the wheel changes hands on these, and the daemon has to notice.
    "    if line in ('mouseon', 'mouseoff'):\n"
    "        h = 'h' if line == 'mouseon' else 'l'\n"
    "        sys.stdout.write(''.join('\\x1b[?%s%s' % (m, h)\n"
    "                                 for m in (1000, 1002, 1003, 1006)))\n"
    "        sys.stdout.flush()\n"
    "        print('mouse:' + h)\n"
    "        continue\n"
    "    print('echo:' + line)\n"
)


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


async def _screen_text(session) -> str:
    await session.screen_synced()
    return "\n".join(session.capture())


async def _wait_screen(session, needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in await _screen_text(session):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{needle!r} never appeared on screen; got:\n{await _screen_text(session)}"
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def test_session_lifecycle(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(name="t1", harness="py", cwd=str(tmp_path)))
        assert session.pid
        await _wait_screen(session, "READY")

        await session.send_keys(["hello world", "Enter"])
        await _wait_screen(session, "echo:hello world")

        # idle detection: no new content -> becomes idle with the 0.5s threshold
        final = await session.wait_for("idle", timeout=10.0, threshold=0.5)
        assert final in ("idle", "exited")

        await session.send_keys(["quit", "Enter"])
        final = await session.wait_for("exited", timeout=10.0, threshold=0.5)
        assert final == "exited"
        assert session.exited

        # raw log captured output on disk
        log = paths.session_log("t1")
        assert log.is_file()
        assert b"READY" in log.read_bytes()

        # exited sessions stay listed: kill is idempotent now and never
        # drops a record — forgetting is remove()'s job alone
        assert mgr.get("t1").exited
        mgr.kill("t1")
        assert mgr.get("t1").exited
        mgr.remove("t1")
        with pytest.raises(Exception):
            mgr.get("t1")

    asyncio.run(run())


def test_sessions_json_persistence(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="keep", harness="py", cwd=str(tmp_path)))
        entries = db.open_default().load_all()
        assert entries[0]["def"]["name"] == "keep"
        assert entries[0]["was_running"] is True
        await mgr.shutdown_all()

    asyncio.run(run())


async def _settled(condition, *, timeout: float = 5.0) -> None:
    """Wait for an off-loop claim to land (see Manager._claim_codex_launch)."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


def test_codex_launch_does_not_wait_for_the_rollout_on_the_loop(
    home, tmp_path, monkeypatch
):
    """The launch-time rollout wait runs in a thread: create() returns at
    once, and the loop keeps serving while Codex takes its time. Measured
    before the change: 2.03 s of loop stall per Codex spawn (claunch-wpd0).
    """
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    monkeypatch.setattr(codex_sessions, "snapshot", lambda _home: {"old"})

    def slow_claim(_home, cwd, known, *, timeout=2.0, poll=0.02):
        if timeout == 0:
            return None
        time.sleep(0.5)
        return "codex-thread-slow"

    monkeypatch.setattr(codex_sessions, "claim_new", slow_claim)

    async def run():
        mgr = _manager()
        t0 = time.monotonic()
        session = mgr.create(SessionDef(
            name="cx", profile="codex", cwd=str(tmp_path)
        ))
        assert time.monotonic() - t0 < 0.4, "create() waited on the scan"
        # The loop is live while the thread waits.
        ticks = 0
        while session.sdef.conversation_id is None and ticks < 200:
            await asyncio.sleep(0.01)
            ticks += 1
        assert session.sdef.conversation_id == "codex-thread-slow"
        assert ticks >= 10, "the loop did not turn while the scan ran"
        await mgr.shutdown_all()

    asyncio.run(run())


def test_codex_conversation_id_is_claimed_and_persisted(
    home, tmp_path, monkeypatch
):
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    monkeypatch.setattr(codex_sessions, "snapshot", lambda _home: {"old"})
    monkeypatch.setattr(
        codex_sessions, "claim_new",
        lambda _home, cwd, known, *, timeout=2.0, poll=0.02: "codex-thread-1",
    )

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(
            name="cx", profile="codex", cwd=str(tmp_path)
        ))
        # Claimed off the loop: create() returns before the scan does.
        assert session.sdef.conversation_id is None
        await _settled(lambda: session.sdef.conversation_id)
        assert session.sdef.conversation_id == "codex-thread-1"
        entries = db.open_default().load_all()
        assert entries[0]["def"]["conversation_id"] == "codex-thread-1"
        await mgr.shutdown_all()

    asyncio.run(run())


def test_codex_conversation_id_is_retried_after_a_slow_rollout(
    home, tmp_path, monkeypatch
):
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    monkeypatch.setattr(codex_sessions, "snapshot", lambda _home: {"old"})
    attempts = []

    def delayed_claim(_home, cwd, known, *, timeout=2.0, poll=0.02):
        attempts.append(timeout)
        return None if len(attempts) == 1 else "codex-thread-late"

    monkeypatch.setattr(codex_sessions, "claim_new", delayed_claim)

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(
            name="cx", profile="codex", cwd=str(tmp_path)
        ))
        assert session.sdef.conversation_id is None

        # The dashboard's next non-blocking list poll scans (timeout 0) and,
        # missing, leaves the claim pending; the launch-time wait, running
        # off the loop, is the other scanner. Whichever scans second wins.
        mgr.list()
        await _settled(lambda: session.sdef.conversation_id)
        assert session.sdef.conversation_id == "codex-thread-late"
        assert sorted(attempts) == [0, 2.0]
        entries = db.open_default().load_all()
        assert entries[0]["def"]["conversation_id"] == "codex-thread-late"

        # Once claimed, later polls do not scan for this session again.
        mgr.list()
        assert sorted(attempts) == [0, 2.0]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_codex_new_replaces_and_persists_the_conversation_id(
    home, tmp_path, monkeypatch
):
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    snapshots = [{"older"}, {"older", "codex-thread-1"}]
    claims = ["codex-thread-1", "codex-thread-2"]
    seen = []

    def snapshot(_home):
        return snapshots.pop(0)

    def claim(_home, cwd, known, *, timeout=2.0, poll=0.02):
        seen.append((set(known), timeout))
        return claims.pop(0)

    monkeypatch.setattr(codex_sessions, "snapshot", snapshot)
    monkeypatch.setattr(codex_sessions, "claim_new", claim)

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(
            name="cx", profile="codex", cwd=str(tmp_path)
        ))
        # The launch-time claim lands off the loop (see _claim_codex_launch).
        await _settled(lambda: session.sdef.conversation_id)
        assert session.sdef.conversation_id == "codex-thread-1"

        await session.send_keys(["/new", "Enter"])
        # Shutdown can begin before the background filesystem claim gets its
        # first event-loop turn. It waits for the replacement ID before the
        # restore definition is persisted.
        await mgr.shutdown_all()

        assert session.sdef.conversation_id == "codex-thread-2"
        entries = db.open_default().load_all()
        assert entries[0]["def"]["conversation_id"] == "codex-thread-2"
        assert seen == [
            ({"older"}, 2.0),
            ({"older", "codex-thread-1"}, 3.0),
        ]

    asyncio.run(run())


def test_pending_codex_claim_is_scanned_once_per_retry_window(
    home, tmp_path, monkeypatch
):
    """Repeated manager reads in one API request do not repeat the same scan."""
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    monkeypatch.setattr(codex_sessions, "snapshot", lambda _home: {"old"})
    attempts = []

    def missing_claim(_home, cwd, known, *, timeout=2.0, poll=0.02):
        attempts.append(timeout)
        return None

    monkeypatch.setattr(codex_sessions, "claim_new", missing_claim)
    monkeypatch.setattr(
        manager_mod, "time", SimpleNamespace(monotonic=lambda: 100.0)
    )

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx", profile="codex", cwd=str(tmp_path)))
        # Nothing scanned on the loop at launch: the 2 s wait is a thread's.
        assert attempts == []
        mgr.list()
        for _ in range(100):
            mgr.get("cx")
            mgr.list()
        assert attempts == [0]
        await _settled(lambda: 2.0 in attempts)
        assert sorted(attempts) == [0, 2.0]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_restore_gives_a_relaunched_session_its_scrollback_back(home, tmp_path):
    """A restart must not cost the web terminal its wheel.

    The daemon's pyte history is the only scrollback the browser has (its
    xterm is built with ``scrollback: 0``), and a restored session used to get
    a brand-new screen with nobody replaying the log into it — so the wheel
    had nothing to scroll while megabytes of it sat on disk.
    """
    _register_py_harness()
    nl = chr(10)

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(name="deep", harness="py", cwd=str(tmp_path)))
        await _wait_screen(session, "READY")
        # More lines than the grid is tall, so they genuinely scroll off.
        for i in range(60):
            await session.send_keys(["scrollback-line-%d" % i, "Enter"])
        await _wait_screen(session, "echo:scrollback-line-59")
        # The render is deferred (ScreenFeeder), so the grid — and the history
        # rows below it — lag the byte stream until this returns.
        await session.screen_synced()
        assert session.screen.history_len > 0
        mgr.persist()
        await mgr.shutdown_all()

        mgr2 = _manager()
        assert mgr2.restore_all() == []
        revived = mgr2.get("deep")
        assert revived.screen.history_len > 0, "the restart wiped the scrollback"
        assert "scrollback-line-" in nl.join(revived.capture(history=True))
        await mgr2.shutdown_all()

    asyncio.run(run())


def test_seeding_a_restored_screen_drops_the_dead_program_modes(home, tmp_path):
    """The replayed log's private modes belong to a program that is gone.

    Carrying them over would encode ``send-keys`` arrows for a DECCKM nobody
    turned on, and would make the repaint claim an alternate screen the
    relaunched program may not have entered. The scrollback is the point of
    the replay; the modes are debris that comes with it.
    """
    _register_py_harness()
    esc = chr(27)
    nl = chr(10)

    # A log written by a program that asserted DECCKM and the alternate screen
    # and then printed more than a screenful.
    log = paths.session_log("modes")
    log.parent.mkdir(parents=True, exist_ok=True)
    body = esc + "[?1049h" + esc + "[?1h"
    body += "".join("old-line-%d" % i + chr(13) + nl for i in range(60))
    log.write_bytes(body.encode())

    async def run():
        mgr = _manager()
        session = mgr.stage(SessionDef(name="modes", harness="py", cwd=str(tmp_path)))
        try:
            session.seed_screen_from_log()
            # The scrollback came back...
            assert session.screen.history_len > 0
            assert "old-line-" in nl.join(session.capture(history=True))
            # ...and the dead program's modes did not.
            assert session.screen.app_cursor_keys is False
            assert session.screen.alt_screen is False
            # The grid itself is blank: the last frame belongs to a program
            # that is gone, so it is rolled into history rather than left on
            # screen pretending to be live.
            assert not [line for line in session.capture() if line.strip()]
        finally:
            mgr.discard("modes")

    asyncio.run(run())


def test_restart_keeps_exited_sessions_respawnable(home, tmp_path):
    """A daemon restart must never *lose* a session: what it does not relaunch
    (already exited, or --no-restore) comes back as an exited record that can
    still be respawned, with its pinned definition intact."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        gone = mgr.create(SessionDef(name="gone", harness="py", cwd=str(tmp_path)))
        await _wait_screen(gone, "READY")
        await gone.send_keys(["quit", "Enter"])
        await gone.wait_for("exited", timeout=10.0, threshold=0.5)
        mgr.create(SessionDef(name="opted-out", harness="py", cwd=str(tmp_path),
                              restore=False))
        await mgr.shutdown_all()

        mgr2 = _manager()  # the "restarted daemon"
        assert mgr2.restore_all() == []
        assert sorted(s.sdef.name for s in mgr2.list()) == ["gone", "opted-out"]
        record = mgr2.get("gone")
        assert record.exited and record.status() == "exited"
        assert record.sdef.cwd == str(tmp_path)
        assert record.info()["status"] == "exited"
        # the retired record still shows what the session last printed
        assert "READY" in "\n".join(record.capture())
        # and it is a record, not a child: driving it is refused, not crashed
        with pytest.raises(SessionGone):
            await record.send_keys(["x"])

        revived = mgr2.respawn("gone")
        await _wait_screen(revived, "READY")
        assert not revived.exited

        # a second restart keeps carrying the remaining record
        await mgr2.shutdown_all()
        mgr3 = _manager()
        mgr3.restore_all()
        assert mgr3.get("opted-out").exited
        await mgr3.shutdown_all()

    asyncio.run(run())


def test_ws_attach_to_a_retired_record(home, tmp_path):
    """A viewer can open a session the daemon only has a *record* of: it gets
    the final screen and an 'exited' status (resume is the way out from there)
    instead of a broken socket."""
    _register_py_harness()
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(name="ghost", harness="py", cwd=str(tmp_path)))
        last_pid = session.pid
        await _wait_screen(session, "READY")
        await session.send_keys(["quit", "Enter"])
        await session.wait_for("exited", timeout=10.0, threshold=0.5)
        await mgr.shutdown_all()

        mgr2 = _manager()  # the restarted daemon keeps it as a record
        mgr2.restore_all()
        assert mgr2.get("ghost").exited
        app = build_app(mgr2, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                "/api/sessions/ghost/ws", headers={"Authorization": "Bearer sekrit"}
            )
            init = json.loads((await ws.receive(timeout=10)).data)
            assert init["type"] == "init" and init["status"] == "exited"
            # the pid of the child it *had*: an incarnation tag that survives
            # the restart, so a viewer can spot a later respawn
            assert init["pid"] == last_pid
            # ...which is exactly why the frame carries the daemon's boot id as
            # well. The pid here belongs to the *previous* daemon's child, so a
            # browser reconnecting across the restart cannot tell from the pid
            # alone that its socket is looking at something new. The boot id
            # can: it is this process's, and it matches what /api/health says.
            assert init["boot_id"] == app["boot_id"]
            health = await (await client.get("/api/health")).json()
            assert health["boot_id"] == app["boot_id"]
            # and when this daemon came up, as a wall clock — what the
            # restart notice prints next to when the page noticed
            assert health["started_at"] == app["started_wall"]
            repaint = await ws.receive(timeout=10)
            assert repaint.type == aiohttp.WSMsgType.BINARY
            assert b"READY" in repaint.data  # its last screen, replayed from the log
            await ws.close()
        finally:
            await mgr2.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_clear_drops_only_exited_records(home, tmp_path):
    """Clearing is the user's explicit cleanup: exited records go, running
    sessions stay, and --logs frees the auto-generated names again."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            dead = mgr.create(SessionDef(name="", harness="py", cwd=str(tmp_path)))
            live = mgr.create(SessionDef(name="", harness="py", cwd=str(tmp_path)))
            assert [dead.sdef.name, live.sdef.name] == ["s0", "s1"]
            await _wait_screen(dead, "READY")
            await dead.send_keys(["quit", "Enter"])
            await dead.wait_for("exited", timeout=10.0, threshold=0.5)

            # a fresh auto-name never reuses a taken one
            assert mgr.create(SessionDef(name="", harness="py", cwd=str(tmp_path))).sdef.name == "s2"

            resp = await client.delete("/api/sessions", headers=bearer)
            assert resp.status == 200
            assert (await resp.json())["removed"] == ["s0"]
            assert sorted(s.sdef.name for s in mgr.list()) == ["s1", "s2"]
            assert paths.session_dir("s0").is_dir()  # its log survives the record

            # the name stays taken while that log is on disk
            assert mgr.create(SessionDef(name="", harness="py", cwd=str(tmp_path))).sdef.name == "s3"

            info = await (await client.get("/api/daemon", headers=bearer)).json()
            assert info["sessions"] == 3 and info["running"] == 3

            mgr.kill("s3")
            await mgr.get("s3").wait_for("exited", timeout=10.0, threshold=0.5)
            resp = await client.delete("/api/sessions?logs=1", headers=bearer)
            assert (await resp.json())["removed"] == ["s3"]
            assert not paths.session_dir("s3").is_dir()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_bulk_stop_resume_delete(home, tmp_path):
    """The rail's bulk verbs, on a rail of three.

    Their whole point is that they differ in what survives: stopping ends the
    programs and keeps every record respawnable, resuming brings the lot back
    under their own names, and only the delete forgets anything. Checked in
    that order on the same three sessions, because each one is the setup for
    the next and a verb that quietly did a neighbour's job would pass on its
    own.
    """
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            names = ["b0", "b1", "b2"]
            for name in names:
                s = mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await _wait_screen(s, "READY")
            # respawning a live one is refused
            resp = await client.post("/api/sessions/b0/respawn", headers=bearer)
            assert resp.status == 400
            # --- stop: programs end, records stay ---------------------------
            resp = await client.post("/api/sessions/kill", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["killed"] == names and doc["failed"] == []
            for name in names:
                await mgr.get(name).wait_for("exited", timeout=10.0, threshold=0.5)
            assert sorted(s.sdef.name for s in mgr.list()) == names

            # a second stop has nothing to stop, and says so rather than failing
            doc = await (await client.post("/api/sessions/kill", headers=bearer)).json()
            assert doc["killed"] == [] and doc["failed"] == []

            # --- resume: all three come back under their own names ----------
            resp = await client.post("/api/sessions/respawn", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["respawned"] == names and doc["failed"] == []
            for name in names:
                revived = mgr.get(name)
                assert not revived.exited
                await _wait_screen(revived, "READY")

            # --- delete: running ones are stopped first, then forgotten -----
            # Without ?running=1 there is nothing exited to drop, which is the
            # distinction the flag exists for.
            doc = await (await client.delete("/api/sessions", headers=bearer)).json()
            assert doc["removed"] == [] and doc["stopped"] == []
            assert sorted(s.sdef.name for s in mgr.list()) == names

            resp = await client.delete("/api/sessions?running=1", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["stopped"] == names
            assert sorted(doc["removed"]) == names and doc["kept"] == []
            assert mgr.list() == []
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_cflow_monitoring(home, tmp_path, monkeypatch):
    """The dashboard endpoint surfaces runs (and their step reports) keyed
    by (cwd, scope); a run maps 1:1 to the session named like its scope."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.cflow import engine as cflow_engine
    from claude_launcher.daemon import api as api_mod

    event_thread = threading.get_ident()
    entry_threads = []
    original_cflow_entry = api_mod._cflow_entry

    def tracked_cflow_entry(*args, **kwargs):
        entry_threads.append(threading.get_ident())
        return original_cflow_entry(*args, **kwargs)

    monkeypatch.setattr(api_mod, "_cflow_entry", tracked_cflow_entry)

    (tmp_path / "wf.yaml").write_text(
        "name: demo\nsteps:\n  one:\n    instructions: do one\n    next: two\n"
        "  two:\n    instructions: do two\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", context="demo task", cwd=str(tmp_path), scope="wf1")
    cflow_engine.report("one is done", "evidence here", cwd=str(tmp_path), scope="wf1")
    cflow_engine.next_step(cwd=str(tmp_path), scope="wf1")

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}
            resp = await client.get("/api/cflow")
            assert resp.status == 401  # authed like everything else

            # the run registered itself at start: visible without any session
            resp = await client.get("/api/cflow", headers=bearer)
            runs = (await resp.json())["runs"]
            assert len(runs) == 1
            assert runs[0]["workflow"] == "demo"
            assert runs[0]["scope"] == "wf1"
            assert runs[0]["status"] == "step"
            assert runs[0]["step_id"] == "two"
            assert runs[0]["sessions"] == []  # its session is not alive yet
            assert runs[0]["reports"][-1]["summary"] == "one is done"
            assert runs[0]["reports"][-1]["details"] == "evidence here"
            assert entry_threads
            assert all(thread_id != event_thread for thread_id in entry_threads)

            # The global background poll has no row to annotate yet, so it
            # transfers none of this historical run or its reports.
            resp = await client.get(
                "/api/cflow", params={"view": "rail"}, headers=bearer
            )
            assert (await resp.json())["runs"] == []

            # explicit ?cwd= inspection also works (and does not duplicate)
            resp = await client.get(
                "/api/cflow", params={"cwd": str(tmp_path)}, headers=bearer
            )
            assert len((await resp.json())["runs"]) == 1

            # the session named like the scope binds 1:1 to the run
            mgr.create(SessionDef(name="wf1", harness="py", cwd=str(tmp_path)))
            resp = await client.get("/api/cflow", headers=bearer)
            runs = (await resp.json())["runs"]
            assert len(runs) == 1
            assert runs[0]["sessions"] == ["wf1"]
            resp = await client.get(
                "/api/cflow", params={"view": "rail"}, headers=bearer
            )
            rail = (await resp.json())["runs"]
            assert len(rail) == 1
            assert rail[0]["sessions"] == ["wf1"]
            assert "reports" not in rail[0]
            assert "instructions" not in rail[0]

            # an unrelated session in that cwd binds to nothing: a second
            # run in ITS scope lists separately, and neither leaks sessions
            mgr.create(SessionDef(name="wf2", harness="py", cwd=str(tmp_path)))
            cflow_engine.start("wf.yaml", cwd=str(tmp_path), scope="wf2")
            resp = await client.get("/api/cflow", headers=bearer)
            runs = {r["scope"]: r for r in (await resp.json())["runs"]}
            assert set(runs) == {"wf1", "wf2"}
            assert runs["wf1"]["sessions"] == ["wf1"]
            assert runs["wf2"]["sessions"] == ["wf2"]
            assert runs["wf1"]["step_id"] == "two"
            assert runs["wf2"]["step_id"] == "one"  # independent positions

            # a session whose cwd has no run stays out of the listing
            other = tmp_path / "other"
            other.mkdir()
            mgr.create(SessionDef(name="idle1", harness="py", cwd=str(other)))
            resp = await client.get("/api/cflow", headers=bearer)
            assert len((await resp.json())["runs"]) == 2

            # The scope arrives from the query string and goes on to BE a
            # directory name, so it is checked before anything opens a path
            # with it. This daemon is reachable through the relay; '../..'
            # must not be a file browser.
            escape = "../../../../etc"
            for params in (
                {"cwd": str(tmp_path), "scope": escape},
                {"cwd": str(tmp_path), "scope": "has space"},
            ):
                resp = await client.get("/api/cflow", params=params, headers=bearer)
                assert resp.status == 400
                resp = await client.get(
                    "/api/cflow/run", params=params, headers=bearer
                )
                assert resp.status == 400
            # ...and the write side, which would otherwise create the slot
            resp = await client.post(
                "/api/cflow/request",
                json={"cwd": str(tmp_path), "scope": escape, "workflow": "wf.yaml"},
                headers=bearer,
            )
            assert resp.status == 400
            assert not (tmp_path / ".cflow" / "runs" / escape).exists()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_cflow_nudge_goes_through_deliver(home, tmp_path, monkeypatch):
    """A cflow nudge reaches the session as a paste plus its own CR.

    The echo harness the other tests use is a plain line reader, so it submits
    whatever chunking it gets — only a real bracketed-paste TUI (Claude Code)
    folds a CR that shares the paste's chunk into the composer, leaving the
    nudge typed but never sent. So assert the write shape, not the echo. The
    rule itself lives in ``Session.deliver``; see test_delivery_contract.py.
    """
    from pathlib import Path

    from claude_launcher.daemon import api as api_mod, session as session_mod
    from claude_launcher.daemon.screen import ScreenState

    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0.0)
    # Pin the wall-clock stamp deliver() prefixes; its format has its own
    # test in test_delivery_contract.py.
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")
    cwd = str(Path(tmp_path).resolve())
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="n1", cwd=cwd)
        screen = ScreenState(80, 24)
        paste = session_mod.Session.paste
        deliver = session_mod.Session.deliver
        _deliver = session_mod.Session._deliver
        _await_readable = session_mod.Session._await_readable
        await_keyboard_quiet = session_mod.Session.await_keyboard_quiet
        keyboard_busy = session_mod.Session.keyboard_busy
        draft_open = session_mod.Session.draft_open
        _started_mono = 0.0
        _input_ready = True  # a session already up; nothing to wait for
        _last_human_input = 0.0  # nobody has typed here
        _last_terminal_input = 0.0
        _draft_open = False  # and nothing half-written is sitting in it
        _delivery_lock = asyncio.Lock()
        _deferred_deliveries = set()
        _deferred_delivery_lock = asyncio.Lock()
        queue_delivery = session_mod.Session.queue_delivery

        async def write_bytes(self, data: bytes) -> None:
            writes.append(data)

    session = FakeSession()
    session.screen.feed(b"\x1b[?2004h")  # a TUI that opted into bracketed paste

    class FakeManager:
        def list(self):
            return [session]

        def get(self, name):
            assert name == "n1"
            return session

    async def run():
        accepted = await api_mod._nudge_sessions(FakeManager(), cwd, "n1", "cflow: go")
        await asyncio.gather(*session._deferred_deliveries)
        return accepted

    nudged = asyncio.run(run())
    assert nudged == ["n1"]
    assert writes == [b"\x1b[200~[T]\rcflow: go\x1b[201~", b"\r"]


def test_scheduled_cflow_start_nudge_is_forced(home, tmp_path):
    """An explicit Start action must not disappear behind an open draft."""
    from pathlib import Path

    from claude_launcher.daemon import api as api_mod

    cwd = str(Path(tmp_path).resolve())
    deliveries = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="n1", cwd=cwd)

        async def deliver(self, text, *, force=False):
            deliveries.append((text, force))
            return True

    session = FakeSession()

    class FakeManager:
        def list(self):
            return [session]

        def get(self, name):
            assert name == "n1"
            return session

    async def run():
        app = {"manager": FakeManager(), "cflow_nudge_tasks": set()}
        assert api_mod._schedule_cflow_nudges(
            app, cwd, "n1", "cflow: start", force=True
        ) == ["n1"]
        await asyncio.gather(*set(app["cflow_nudge_tasks"]))

    asyncio.run(run())
    assert deliveries == [("cflow: start", True)]


def test_api_session_meta_and_workflow_request(home, tmp_path, monkeypatch):
    """A session's own page: its definition, its mesh memberships, and the
    cflow slot it owns — plus the two ways to create a run in that slot."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import workspaces
    from claude_launcher.cflow import engine as cflow_engine
    from claude_launcher.daemon import session as session_mod

    monkeypatch.setattr(session_mod, "INPUT_READY_TIMEOUT", 0.2)
    monkeypatch.setattr(session_mod, "INPUT_SETTLE", 0.0)

    (tmp_path / ".claunch" / "workflows").mkdir(parents=True)
    (tmp_path / ".claunch" / "workflows" / "demo.yaml").write_text(
        "name: demo\ndescription: two steps\nsteps:\n"
        "  one:\n    instructions: do one\n    next: two\n"
        "  two:\n    instructions: do two\n",
        encoding="utf-8",
    )
    workspaces.add(str(tmp_path), "proj")

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}
            # A dashboard action can follow session creation before its
            # harness has accepted input.  The API returns after binding the
            # nudge to the session; the background delivery waits for
            # readiness without turning the start into an unscoped run.
            session = mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))

            resp = await client.get("/api/sessions/s1/meta")
            assert resp.status == 401  # authed like the rest of /api

            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            meta = await resp.json()
            assert meta["session"]["harness"] == "py"
            assert meta["workspace"]["name"] == "proj"
            assert meta["meshes"] == []
            # the slot this session owns: keyed by (its cwd, its own name)
            assert meta["cflow"]["scope"] == "s1"
            assert meta["cflow"]["status"] == "idle"
            assert meta["cflow"]["sessions"] == ["s1"]
            assert [w["name"] for w in meta["workflows"]] == ["demo"]

            # path 1: ask the session's agent to start it. Nothing is started;
            # the request is recorded and typed into the session.
            resp = await client.post(
                "/api/cflow/request",
                json={"cwd": str(tmp_path), "scope": "s1",
                      "workflow": "demo", "context": "from the web"},
                headers=bearer,
            )
            doc = await resp.json()
            assert resp.status == 200
            assert doc["nudge_scheduled_sessions"] == ["s1"]
            await _wait_screen(session, "a start of workflow 'demo' was requested")

            # a mesh membership shows up on the session's page
            app["mesh"].create("dev")
            await app["mesh"].join("dev", "s1", handle="impl")
            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            assert (await resp.json())["meshes"][0]["mesh"] == "dev"

            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            flow = (await resp.json())["cflow"]
            assert flow["status"] == "idle"          # still nothing running
            assert flow["pending_start"]["workflow"] == "demo"
            assert flow["pending_start"]["context"] == "from the web"
            # and the waiting slot is listed on the dashboard
            resp = await client.get("/api/cflow", headers=bearer)
            runs = (await resp.json())["runs"]
            assert [r["scope"] for r in runs] == ["s1"]
            assert runs[0]["pending_start"]["workflow"] == "demo"

            # the agent (its own process) performs the start, consuming it
            cflow_engine.start("demo", "from the web", cwd=str(tmp_path), scope="s1")
            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            flow = (await resp.json())["cflow"]
            assert flow["status"] == "step" and flow["step_id"] == "one"
            assert flow.get("pending_start") is None

            # a second request is refused while that run is active
            resp = await client.post(
                "/api/cflow/request",
                json={"cwd": str(tmp_path), "scope": "s1", "workflow": "demo"},
                headers=bearer,
            )
            assert resp.status == 400
            assert "already active" in (await resp.json())["error"]

            # path 2: after archiving, start directly (the run exists at once)
            await client.post(
                "/api/cflow/archive",
                json={"cwd": str(tmp_path), "scope": "s1"}, headers=bearer,
            )
            resp = await client.post(
                "/api/cflow/start",
                json={"cwd": str(tmp_path), "scope": "s1", "workflow": "demo"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["step_id"] == "one"
            assert doc["nudge_scheduled_sessions"] == ["s1"]
            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            assert (await resp.json())["cflow"]["status"] == "step"

            # Archive must return without waiting for terminal delivery, and
            # the immediately following Ask path still reaches the session.
            resp = await client.post(
                "/api/cflow/archive",
                json={"cwd": str(tmp_path), "scope": "s1"}, headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudge_scheduled_sessions"] == ["s1"]
            resp = await client.post(
                "/api/cflow/request",
                json={"cwd": str(tmp_path), "scope": "s1", "workflow": "demo"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudge_scheduled_sessions"] == ["s1"]
            await _wait_screen(session, "a start of workflow 'demo' was requested")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_cflow_request_can_be_withdrawn(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    (tmp_path / "wf.yaml").write_text(
        "name: demo\nsteps:\n  one:\n    instructions: do one\n", encoding="utf-8"
    )

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}
            body = {"cwd": str(tmp_path), "scope": "ghost", "workflow": "wf.yaml"}
            resp = await client.post("/api/cflow/request", json=body, headers=bearer)
            assert resp.status == 200
            # no session named 'ghost' is alive: nothing to nudge, but the
            # request stands for whoever attaches next
            assert (await resp.json())["nudge_scheduled_sessions"] == []

            resp = await client.post(
                "/api/cflow/request/cancel",
                json={"cwd": str(tmp_path), "scope": "ghost"}, headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "request_cancelled"

            resp = await client.get("/api/cflow", headers=bearer)
            assert (await resp.json())["runs"] == []

            resp = await client.post(
                "/api/cflow/request/cancel",
                json={"cwd": str(tmp_path), "scope": "ghost"}, headers=bearer,
            )
            assert resp.status == 400  # nothing left to withdraw
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_cflow_skip_advances_the_loop(home, tmp_path, monkeypatch):
    """/api/cflow/skip cuts the ACTIVE round short: the run is archived and
    the same workflow (same source file, same context) is requested again at
    round + 1 — the count keeps climbing, it never rewinds. A finished round
    has already filed its own next-round request, so there is nothing to
    skip between rounds."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.cflow import engine as cflow_engine

    (tmp_path / "wf.yaml").write_text(
        "name: loop\nrecur: true\nsteps:\n  one:\n    instructions: do one\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", context="serve", cwd=str(tmp_path), scope="s1")
    st = cflow_engine.status(cwd=str(tmp_path), scope="s1")
    assert st["recur"] is True  # status says the run loops

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}
            body = {"cwd": str(tmp_path), "scope": "s1"}

            # mid-round: the active round-1 run is archived, round 2 requested
            resp = await client.post("/api/cflow/skip", json=body, headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "start_requested"
            req = doc["request"]
            assert req["by"] == "web-skip"
            assert req["name"] == "loop"
            assert req["context"] == "serve"
            assert req["round"] == 2  # the count climbed, it did not rewind

            # the cut-short run was archived; the request holds the slot
            st2 = cflow_engine.status(cwd=str(tmp_path), scope="s1")
            assert st2["status"] == "idle"
            assert st2["pending_start"]["id"] == req["id"]

            # fulfilling the skip's request joins the loop at round 2
            started = cflow_engine.start(
                req["workflow"], context=req["context"],
                cwd=str(tmp_path), scope="s1",
            )
            assert started["round"] == 2

            # a finished round already filed its own next-round request —
            # skipping is refused and that request is left standing
            cflow_engine.report("served", "evidence", cwd=str(tmp_path), scope="s1")
            assert cflow_engine.next_step(cwd=str(tmp_path), scope="s1")["status"] == "done"
            st3 = cflow_engine.status(cwd=str(tmp_path), scope="s1")
            assert st3["pending_start"]["by"] == "recur"
            assert st3["pending_start"]["round"] == 3
            resp = await client.post("/api/cflow/skip", json=body, headers=bearer)
            assert resp.status == 400
            st4 = cflow_engine.status(cwd=str(tmp_path), scope="s1")
            assert st4["pending_start"]["round"] == 3

            # a finished run with no request, and an empty slot: nothing to skip
            cflow_engine.cancel_request(cwd=str(tmp_path), scope="s1")
            resp = await client.post("/api/cflow/skip", json=body, headers=bearer)
            assert resp.status == 400
            cflow_engine.archive(by="test", cwd=str(tmp_path), scope="s1")
            resp = await client.post("/api/cflow/skip", json=body, headers=bearer)
            assert resp.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_ws_init_identifies_the_incarnation(home, tmp_path):
    """The terminal socket's init frame carries the child's pid, so a viewer
    can tell its socket is bound to a session that has since been respawned
    (same name, new child) — that is how the web UI follows a resume."""
    _register_py_harness()
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            session = mgr.create(SessionDef(name="w2", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            ws = await client.ws_connect("/api/sessions/w2/ws", headers=bearer)
            msg = await ws.receive(timeout=10)
            assert msg.type == aiohttp.WSMsgType.TEXT
            init = json.loads(msg.data)
            assert init["type"] == "init"
            assert init["pid"] == session.pid
            await ws.close()

            await session.send_keys(["quit", "Enter"])
            await session.wait_for("exited", timeout=10.0, threshold=0.5)
            resp = await client.post("/api/sessions/w2/respawn", headers=bearer)
            assert resp.status == 200

            ws = await client.ws_connect("/api/sessions/w2/ws", headers=bearer)
            msg = await ws.receive(timeout=10)
            revived = json.loads(msg.data)
            assert revived["pid"] == mgr.get("w2").pid != init["pid"]
            await ws.close()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_ws_scroll_control_serves_history_and_freezes_data(home, tmp_path):
    """The scroll control is answered with the clamped offset and a history
    repaint; while frozen the socket gets no live data; scrolling far past
    the bottom snaps back to live and the repaint shows the newest output."""
    _register_py_harness()
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            # A small terminal, so even a short exchange scrolls real lines
            # into the daemon's pyte history (SessionDef rows=10).
            session = mgr.create(
                SessionDef(name="wsc", harness="py", cwd=str(tmp_path), rows=10)
            )
            await _wait_screen(session, "READY")
            await session.send_keys(
                [key for i in range(30) for key in (f"l{i}", "Enter")]
            )
            await _wait_screen(session, "echo:l29")
            assert session.screen.history_len > 0

            async def next_json(pred):
                while True:
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    assert msg.type == aiohttp.WSMsgType.TEXT, msg.type
                    data = json.loads(msg.data)
                    if pred(data):
                        return data

            # A client that asks for the scrollback gets it before the grid,
            # so its own terminal can serve the wheel natively.
            asked = await client.ws_connect(
                "/api/sessions/wsc/ws?scrollback=1", headers=bearer
            )
            try:
                assert json.loads((await asked.receive(timeout=10)).data)["type"] == "init"
                hist = await asked.receive(timeout=10)
                assert hist.type == aiohttp.WSMsgType.BINARY
                assert hist.data.startswith(b"\x1b[?1049l")
                assert b"echo:l0" in hist.data, "the seed carries real history"
                grid = await asked.receive(timeout=10)
                assert grid.type == aiohttp.WSMsgType.BINARY
            finally:
                await asked.close()

            ws = await client.ws_connect("/api/sessions/wsc/ws", headers=bearer)
            try:
                msg = await ws.receive(timeout=10)
                assert msg.type == aiohttp.WSMsgType.TEXT
                init = json.loads(msg.data)
                assert init["type"] == "init"
                assert init["alt"] is False  # the echo harness lives in the main buffer
                assert init["mouse"] is False  # and it never asks for the mouse

                # A client that asked for nothing gets nothing: the very next
                # binary frame is the grid, not five thousand lines of history
                # it never opted into. `claunch attach` is that client.
                seed = await ws.receive(timeout=10)
                assert seed.type == aiohttp.WSMsgType.BINARY
                assert b"echo:l0" not in seed.data, "no unrequested scrollback"

                await ws.send_str(json.dumps({"type": "scroll", "lines": 5}))
                scrolled = await next_json(lambda d: d.get("type") == "scrolled")
                assert scrolled["offset"] == 5
                repaint = await ws.receive(timeout=10)
                assert repaint.type == aiohttp.WSMsgType.BINARY
                assert repaint.data.startswith(b"\x1b[?1049l")

                # Frozen: whatever the PTY says next must not reach the socket.
                await session.send_keys(["fresh", "Enter"])
                await _wait_screen(session, "echo:fresh")
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    # state/resize text may slip in; a data frame must not
                    assert msg.type == aiohttp.WSMsgType.TEXT, msg.type
                    assert not json.loads(msg.data).get("type") == "scrolled"
                except asyncio.TimeoutError:
                    pass

                # Snap to live: the new offset is 0 and the repaint that
                # answers carries the output that was suppressed while frozen.
                await ws.send_str(json.dumps({"type": "scroll", "lines": -999}))
                back = await next_json(lambda d: d.get("type") == "scrolled")
                assert back["offset"] == 0
                repaint0 = await ws.receive(timeout=10)
                assert repaint0.type == aiohttp.WSMsgType.BINARY
                assert b"echo:fresh" in repaint0.data
            finally:
                await ws.close()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_ws_hands_the_wheel_to_a_program_that_takes_the_mouse(home, tmp_path):
    """A program asserting ``?1000h`` is asking for wheel ticks itself.

    Two things have to reach the viewer for that to work: the ``mouse`` frame
    (so the page stops spending ticks on scroll controls) and the modes
    themselves in every repaint (so a terminal that attached late reports the
    wheel like one that watched the program start). Without the second, a
    viewer's xterm swallows the wheel and the session reads as unscrollable —
    which is how it read before.
    """
    _register_py_harness()
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            session = mgr.create(
                SessionDef(name="wsm", harness="py", cwd=str(tmp_path), rows=10)
            )
            await _wait_screen(session, "READY")

            ws = await client.ws_connect("/api/sessions/wsm/ws", headers=bearer)
            try:
                init = json.loads((await ws.receive(timeout=10)).data)
                assert init["mouse"] is False, "premise: nobody has the mouse yet"
                await ws.receive(timeout=10)          # the repaint seed

                await session.send_keys(["mouseon", "Enter"])
                await _wait_screen(session, "mouse:h")

                # The frame that tells the page the wheel changed hands.
                deadline = time.monotonic() + 10
                took = None
                while took is None and time.monotonic() < deadline:
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("type") == "mouse":
                            took = data
                assert took == {"type": "mouse", "tracking": True}
                assert session.screen.mouse_tracking is True
                assert session.screen.wheel_is_the_programs is True

                # And a viewer arriving now learns the modes from the repaint.
                late = await client.ws_connect(
                    "/api/sessions/wsm/ws", headers=bearer
                )
                try:
                    late_init = json.loads((await late.receive(timeout=10)).data)
                    assert late_init["mouse"] is True
                    repaint = await late.receive(timeout=10)
                    assert repaint.type == aiohttp.WSMsgType.BINARY
                    for mode in (b"?1000h", b"?1002h", b"?1003h", b"?1006h"):
                        assert mode in repaint.data, mode
                finally:
                    await late.close()

                # Handing it back is announced too.
                await session.send_keys(["mouseoff", "Enter"])
                await _wait_screen(session, "mouse:l")
                deadline = time.monotonic() + 10
                gave = None
                while gave is None and time.monotonic() < deadline:
                    msg = await asyncio.wait_for(ws.receive(), timeout=10)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("type") == "mouse":
                            gave = data
                assert gave == {"type": "mouse", "tracking": False}
                assert session.screen.mouse_tracking is False
            finally:
                await ws.close()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_transcript_pages_the_conversation(home, tmp_path, monkeypatch):
    """The route behind the transcript pane, paged newest-last.

    The pane exists because the terminal cannot answer this: a claude session
    repaints the alternate screen instead of scrolling it, so the readable
    record of what it said lives only in claude's own jsonl.
    """
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer
    from claude_launcher.daemon import transcript_view

    conv = tmp_path / "conv.jsonl"
    conv.write_text(
        "".join(
            json.dumps({
                "type": "user" if i % 2 == 0 else "assistant",
                "timestamp": "2026-08-25T00:00:00Z",
                "message": {"role": "user" if i % 2 == 0 else "assistant",
                            "content": f"turn {i}"},
            }) + "\n"
            for i in range(30)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(transcript_view, "locate_transcript", lambda sdef: conv)

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            mgr.create(SessionDef(name="tx", harness="py", cwd=str(tmp_path)))

            r = await client.get(
                "/api/sessions/tx/transcript?limit=10", headers=bearer
            )
            assert r.status == 200
            tail = await r.json()
            assert tail["total"] == 30
            assert [b["text"] for rec in tail["records"] for b in rec["blocks"]][-1] \
                == "turn 29"
            assert tail["has_more"] is True

            r2 = await client.get(
                f"/api/sessions/tx/transcript?limit=10&before={tail['cursor']}",
                headers=bearer,
            )
            older = await r2.json()
            assert [b["text"] for rec in older["records"] for b in rec["blocks"]][0] \
                == "turn 10"

            # The index is the storage the daemon keeps for this session: it
            # sits beside the session's log and survives a restart, so the
            # next page is a seek rather than a re-read of the whole file.
            assert transcript_view.index_path("tx").is_file()

            bad = await client.get(
                "/api/sessions/tx/transcript?before=nope", headers=bearer
            )
            assert bad.status == 400
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_shutdown_not_blocked_by_open_websocket(home, tmp_path):
    """A dashboard tab left open must not stall daemon teardown (it used to
    wait aiohttp's 60s shutdown timeout per lingering terminal socket)."""
    _register_py_harness()
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        session = mgr.create(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
        await _wait_screen(session, "READY")
        ws = await client.ws_connect(
            "/api/sessions/w1/ws", headers={"Authorization": "Bearer sekrit"}
        )
        msg = await ws.receive(timeout=10)  # init control frame
        assert msg.type == aiohttp.WSMsgType.TEXT

        start = time.monotonic()
        await mgr.shutdown_all()
        await asyncio.wait_for(client.close(), timeout=15)
        assert time.monotonic() - start < 15

    asyncio.run(run())


def test_api_cflow_actions(home, tmp_path, monkeypatch):
    """The run-detail endpoint, and human approve/select over the web API."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.cflow import engine as cflow_engine

    gated = tmp_path / "gated"
    gated.mkdir()
    (gated / "wf.yaml").write_text(
        "name: gated\nsteps:\n  ship:\n    gate: approve shipping\n"
        "    instructions: ship it\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", cwd=str(gated), scope="n1")

    chooser = tmp_path / "chooser"
    chooser.mkdir()
    (chooser / "wf.yaml").write_text(
        "name: pick\nsteps:\n  triage:\n    select:\n      prompt: pick\n"
        "      chooser: user\n      options:\n"
        "        a: {description: A, next: work}\n"
        "        b: {description: B, next: work}\n"
        "  work:\n    instructions: do it\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", cwd=str(chooser))

    # A plain two-step run, for the request an AGENT files to be moved to a
    # step its workflow declares no route to. It needs two steps and no gate:
    # the question here is the position, not entry to it.
    jumper = tmp_path / "jumper"
    jumper.mkdir()
    (jumper / "wf.yaml").write_text(
        "name: jump\nsteps:\n  one:\n    instructions: do one\n    next: two\n"
        "  two:\n    instructions: do two\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", cwd=str(jumper), scope="n2")
    cflow_engine.report("did one", cwd=str(jumper), scope="n2")
    cflow_engine.next_step(cwd=str(jumper), scope="n2")

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            bearer = {"Authorization": "Bearer sekrit"}

            # detail: live status + the full graph for the diagram
            resp = await client.get(
                "/api/cflow/run",
                params={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["scope"] == "n1"
            assert doc["run"]["status"] == "waiting_approval"
            assert [s["id"] for s in doc["workflow"]["steps"]] == ["ship"]
            assert doc["workflow"]["steps"][0]["gate"] == "approve shipping"
            # the dashboard shows each step's instructions in the detail panel
            assert doc["workflow"]["steps"][0]["instructions"] == "ship it"

            resp = await client.get("/api/cflow/run", headers=bearer)
            assert resp.status == 400  # cwd required

            # the run's own session (name == scope) gets nudged on approve
            worker = mgr.create(
                SessionDef(name="n1", harness="py", cwd=str(gated))
            )
            await _wait_screen(worker, "READY")

            # approve over HTTP opens the gate; the agent then fetches the step
            resp = await client.post(
                "/api/cflow/approve",
                json={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "approved"
            assert doc["nudge_scheduled_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: approved")
            assert (
                cflow_engine.next_step(cwd=str(gated), scope="n1")["status"] == "step"
            )

            # manual (repeat) nudge from the dashboard
            resp = await client.post(
                "/api/cflow/nudge",
                json={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudge_scheduled_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: continue")

            # forced state set (goto) from the dashboard: re-gates + nudges
            resp = await client.post(
                "/api/cflow/goto",
                json={"cwd": str(gated), "scope": "n1", "step": "nope"},
                headers=bearer,
            )
            assert resp.status == 400
            resp = await client.post(
                "/api/cflow/goto",
                json={"cwd": str(gated), "scope": "n1", "step": "ship",
                      "reason": "redo"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "state_set"
            assert doc["visit"] == 2
            assert doc["nudge_scheduled_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: current step forced")

            # the forced revisit re-closed the gate: approve opens it again...
            resp = await client.post(
                "/api/cflow/approve",
                json={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 200
            # ...and with nothing left waiting, approve is a clean 400, not a 500
            resp = await client.post(
                "/api/cflow/approve",
                json={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 400

            # an agent's step-change request, answered from the dashboard.
            worker2 = mgr.create(
                SessionDef(name="n2", harness="py", cwd=str(jumper))
            )
            await _wait_screen(worker2, "READY")
            cflow_engine.request_goto(
                "one", "the merge turned up a case step one never handled",
                by="n2", cwd=str(jumper), scope="n2",
            )
            # The run reports the stop, so the dashboard can draw the question.
            assert cflow_engine.status(
                cwd=str(jumper), scope="n2"
            )["status"] == "waiting_goto"

            resp = await client.post(
                "/api/cflow/goto/resolve",
                json={"cwd": str(jumper), "scope": "n2", "decision": "maybe"},
                headers=bearer,
            )
            assert resp.status == 400  # only approve/deny answer this

            # refusing leaves the position where it was and tells the agent
            resp = await client.post(
                "/api/cflow/goto/resolve",
                json={"cwd": str(jumper), "scope": "n2", "decision": "deny",
                      "reason": "step one's outcome still holds"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "goto_denied"
            assert doc["nudge_scheduled_sessions"] == ["n2"]
            assert cflow_engine.status(
                cwd=str(jumper), scope="n2"
            )["step_id"] == "two"
            await _wait_screen(worker2, "echo:cflow: your step-change request")

            # granting one moves the run to the step that was asked for
            cflow_engine.next_step(cwd=str(jumper), scope="n2")  # clears the refusal
            cflow_engine.request_goto(
                "one", "still the right step", by="n2",
                cwd=str(jumper), scope="n2",
            )
            resp = await client.post(
                "/api/cflow/goto/resolve",
                json={"cwd": str(jumper), "scope": "n2", "decision": "approve"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "state_set" and doc["step_id"] == "one"
            assert doc["nudge_scheduled_sessions"] == ["n2"]
            await _wait_screen(worker2, "echo:cflow: current step forced")

            # and with nothing left waiting it is a clean 400, not a 500
            resp = await client.post(
                "/api/cflow/goto/resolve",
                json={"cwd": str(jumper), "scope": "n2", "decision": "deny"},
                headers=bearer,
            )
            assert resp.status == 400

            # user-chooser select over HTTP: bad option 400, good option confirms
            resp = await client.post(
                "/api/cflow/select",
                json={"cwd": str(chooser), "option": "zzz"},
                headers=bearer,
            )
            assert resp.status == 400
            resp = await client.post(
                "/api/cflow/select",
                json={"cwd": str(chooser), "option": "b", "reason": "picked b"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "selected"
            assert cflow_engine.next_step(cwd=str(chooser))["step_id"] == "work"

            # workflow listing feeds the dashboard's start picker
            wfdir = chooser / ".claunch" / "workflows"
            wfdir.mkdir(parents=True)
            (wfdir / "tiny.yaml").write_text(
                "name: tiny\ndescription: one step\n"
                "steps:\n  only:\n    instructions: do\n",
                encoding="utf-8",
            )
            resp = await client.get(
                "/api/cflow/workflows", params={"cwd": str(chooser)}, headers=bearer
            )
            assert resp.status == 200
            flows = (await resp.json())["workflows"]
            assert any(f["name"] == "tiny" and f["steps"] == 1 for f in flows)
            # the picker shows a name, so the wire has to carry the file: which
            # layer answered, and what that answer overrode
            tiny = next(f for f in flows if f["name"] == "tiny")
            assert tiny["path"] == str(wfdir / "tiny.yaml")
            assert tiny["origin"] == "project"
            assert tiny["shadowed"] == []

            # web start over a still-active run: clean 400 pointing at archive
            resp = await client.post(
                "/api/cflow/start",
                json={"cwd": str(chooser), "workflow": "tiny"},
                headers=bearer,
            )
            assert resp.status == 400
            assert "archive" in (await resp.json())["error"]

            # archive retires the active run (aborting it) and frees the slot
            resp = await client.post(
                "/api/cflow/archive", json={"cwd": str(chooser)}, headers=bearer
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "archived"
            assert doc["was"] == "running"
            resp = await client.post(  # nothing left to archive -> 400, not 500
                "/api/cflow/archive", json={"cwd": str(chooser)}, headers=bearer
            )
            assert resp.status == 400

            # slot is free: web start works; a default-scope run has no
            # session of its own to nudge
            resp = await client.post(
                "/api/cflow/start",
                json={"cwd": str(chooser), "workflow": "tiny", "context": "web"},
                headers=bearer,
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "step"
            assert doc["step_id"] == "only"
            assert doc["nudge_scheduled_sessions"] == []

            # for a session-bound run, archive + start nudges the session so
            # its agent picks the new workflow up
            gwfdir = gated / ".claunch" / "workflows"
            gwfdir.mkdir(parents=True)
            (gwfdir / "tiny.yaml").write_text(
                "name: tiny\nsteps:\n  only:\n    instructions: do\n",
                encoding="utf-8",
            )
            resp = await client.post(
                "/api/cflow/archive",
                json={"cwd": str(gated), "scope": "n1"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudge_scheduled_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: run archived")
            resp = await client.post(
                "/api/cflow/start",
                json={"cwd": str(gated), "scope": "n1", "workflow": "tiny"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudge_scheduled_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: a new workflow run")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_end_to_end(home, tmp_path):
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # health is open; everything else requires auth
            resp = await client.get("/api/health")
            assert resp.status == 200
            health = await resp.json()
            resp = await client.get("/api/sessions")
            assert resp.status == 401

            bearer = {"Authorization": "Bearer sekrit"}
            resp = await client.get("/api/sessions", headers=bearer)
            assert resp.status == 200

            # A browser that lost its cookie in a restart has only the open
            # endpoint left, so that is where the daemon's identity has to be
            # readable: same id from behind the auth wall, and a different one
            # from a different process (which is what makes it worth reading).
            resp = await client.get("/api/daemon", headers=bearer)
            assert (await resp.json())["boot_id"] == health["boot_id"]
            other = build_app(_manager(), "sekrit", started_at=time.monotonic())
            assert other["boot_id"] != health["boot_id"]

            # browser cookie flow
            resp = await client.post("/api/auth/session", json={"token": "wrong"})
            assert resp.status == 401
            # non-ASCII wrong token must be a 401, not a compare_digest 500
            resp = await client.post("/api/auth/session", json={"token": "비밀토큰"})
            assert resp.status == 401
            resp = await client.post("/api/auth/session", json={"token": "sekrit"})
            assert resp.status == 200
            resp = await client.get("/api/sessions")  # cookie jar now has it
            assert resp.status == 200

            # create -> send-keys -> capture -> wait -> kill, all over HTTP
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "dead-picker",
                    "profile": "py",
                    "harness": "py",
                    "cwd": str(tmp_path),
                },
                headers=bearer,
            )
            assert resp.status == 400
            assert "read-only" in (await resp.json())["error"]
            resp = await client.post(
                "/api/sessions",
                json={"name": "api1", "profile": "py", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 201, await resp.text()

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                resp = await client.get("/api/sessions/api1/capture", headers=bearer)
                if "READY" in await resp.text():
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("READY never appeared via capture API")

            resp = await client.post(
                "/api/sessions/api1/keys",
                json={"keys": ["ping", "Enter"]},
                headers=bearer,
            )
            assert resp.status == 200

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                resp = await client.get("/api/sessions/api1/capture", headers=bearer)
                if "echo:ping" in await resp.text():
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("echo:ping never appeared via capture API")

            resp = await client.get(
                "/api/sessions/api1/wait?state=idle&timeout=10&threshold=0.5",
                headers=bearer,
            )
            assert resp.status == 200

            # /deliver: the door for automated senders outside the daemon
            resp = await client.post(
                "/api/sessions/api1/deliver", json={"text": ""}, headers=bearer
            )
            assert resp.status == 400  # a message must have content
            resp = await client.post(
                "/api/sessions/api1/deliver",
                json={"text": "a message"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["delivered"] is True
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                resp = await client.get("/api/sessions/api1/capture", headers=bearer)
                if "echo:a message" in await resp.text():
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("the delivered message was never submitted")

            resp = await client.post(
                "/api/sessions/api1/keys",
                json={"keys": ["quit", "Enter"]},
                headers=bearer,
            )
            assert resp.status == 200
            resp = await client.get(
                "/api/sessions/api1/wait?state=exited&timeout=10",
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "exited"

            resp = await client.delete("/api/sessions/api1", headers=bearer)
            assert resp.status == 200

            # duplicate names conflict
            resp = await client.post(
                "/api/sessions",
                json={"name": "dup", "profile": "py", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 201
            resp = await client.post(
                "/api/sessions",
                json={"name": "dup", "profile": "py", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 409
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_resume_names_a_session_and_stores_its_conversation(home, tmp_path):
    """Callers name the session they can SEE (in the web picker, in `claunch
    sessions`); the registry is the only place that maps it to a conversation,
    so the stored definition only ever holds the id."""
    mgr = _manager()
    cid = "11111111-2222-3333-4444-555555555555"
    mgr._retire(
        SessionDef(name="old", profile="p", cwd=str(tmp_path), conversation_id=cid),
        {},
    )
    resolved = mgr._resolve_resume(SessionDef(name="new", resume="old"))
    assert resolved.resume == cid
    # A uuid (or a conversation only claude knows about) passes straight through
    assert mgr._resolve_resume(SessionDef(name="new", resume=cid)).resume == cid

    mgr._retire(SessionDef(name="raw", profile="p", cwd=str(tmp_path)), {})
    with pytest.raises(ManagerError, match="no pinned conversation"):
        mgr._resolve_resume(SessionDef(name="new", resume="raw"))


def test_api_harnesses_report_declared_and_installed_separately(home, tmp_path):
    """The picker lists a harness claunch knows about even when the machine
    cannot run it — hiding it would read as "claunch does not support pi"."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            store.update(
                lambda doc: doc.update(
                    {
                        "harnesses": {
                            "codex": {"command": sys.executable},
                            "pi": {
                                "command": "no-such-program-xyz",
                                "auth": "api-key",
                                "token_env": "ANTHROPIC_API_KEY",
                            },
                        }
                    }
                )
            )
            pi_profile = profile.create("pi-profile")
            lineage.set_harness(pi_profile, "pi")
            credentials.save_token(pi_profile, "pi-profile-secret")
            codex_only = profile.create("codex-only")
            lineage.set_harness(codex_only, "codex")
            store.set_profile_field(
                codex_only.name, "allowed_harnesses", ["codex"]
            )
            restricted = profile.create("restricted")
            store.update(
                lambda doc: (
                    doc.setdefault("providers", {}).update(
                        {
                            "claude-only": {
                                "env": {},
                                "allowed_harnesses": ["claude"],
                            }
                        }
                    ),
                    doc["profiles"][restricted.name].update(
                        {
                            "provider": "claude-only",
                            "allowed_harnesses": ["claude", "pi"],
                        }
                    ),
                )
            )
            resp = await client.get("/api/profiles", headers=bearer)
            profile_doc = await resp.json()
            details = {
                item["name"]: item
                for item in profile_doc["profile_details"]
            }
            assert details["pi-profile"]["harness"] == "pi"
            assert details["pi-profile"]["harness_available"] is False
            assert details["pi-profile"]["borrow_allowed"] is True
            assert details["pi-profile"]["borrow_mode"] == "token"
            assert details["pi-profile:claude"]["harness"] == "claude"
            assert details["pi-profile:claude"]["borrow_mode"] == "provider-token"
            assert details["pi-profile:pi"]["harness"] == "pi"
            assert details["restricted:claude"]["harness_allowed"] is True
            assert details["restricted:pi"]["harness_allowed"] is False
            assert "provider 'claude-only'" in details["restricted:pi"]["harness_policy"]["reason"]
            assert profile_doc["profiles"] == [
                "codex-only", "pi-profile", "restricted"
            ]
            assert "codex-only:codex" in profile_doc["profile_selectors"]
            assert "codex-only:claude" not in profile_doc["profile_selectors"]
            assert "pi-profile:claude" in profile_doc["profile_selectors"]
            assert "pi-profile:pi" in profile_doc["profile_selectors"]
            assert "restricted:claude" in profile_doc["profile_selectors"]
            assert "restricted:pi" not in profile_doc["profile_selectors"]
            options = {
                item["value"]: item for item in profile_doc["profile_options"]
            }
            assert options["pi-profile:pi"]["label"] == "pi-profile/pi"
            assert options["restricted:claude"]["label"] == "restricted/claude"
            assert options["codex-only:codex"]["label"] == "codex-only/codex"
            assert options["pi-profile:pi"]["default"] is True

            resp = await client.get(
                "/api/borrow-options?profile=pi-profile", headers=bearer
            )
            assert resp.status == 200
            borrow_doc = await resp.json()
            lenders = {item["name"]: item for item in borrow_doc["options"]}
            assert lenders["pi-profile"]["status"] == "ready"
            assert lenders["pi-profile"]["selectable"] is True
            assert "pi-profile-secret" not in json.dumps(borrow_doc)
            assert lenders["restricted"]["status"] == "harness-policy-denied"
            assert lenders["restricted"]["selectable"] is False
            assert lenders["codex-only"]["status"] == "harness-policy-denied"
            assert lenders["codex-only"]["selectable"] is False
            resp = await client.get("/api/harnesses", headers=bearer)
            assert resp.status == 200
            by_name = {h["name"]: h for h in (await resp.json())["harnesses"]}
            assert by_name["codex"]["available"] is True
            assert by_name["pi"]["available"] is False
            assert by_name["pi"]["program"] == "no-such-program-xyz"
            assert by_name["claude"]["builtin"] is True
            assert by_name["claude"]["btw"] == {
                "command": "/btw",
                "aliases": [],
                "minimum_version": "2.1.73",
                "requires_started_conversation": False,
                "available_while_busy": True,
                "context": "current-conversation",
                "history": "ephemeral",
                "tool_access": "none",
                "response_mode": "single-response",
            }

            # ...and a harness that is declared but not installed is refused
            # with a message that says so, not a spawn failure
            resp = await client.post(
                "/api/sessions",
                json={"name": "nope", "profile": "pi-profile", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 400
            assert "not found on PATH" in (await resp.json())["error"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_profiles_publish_what_each_model_choice_reaches(home, tmp_path):
    """The dashboard's model rows carry the id behind each alias.

    A form cannot work that out: an alias is what a launch passes, and the id
    behind it is written by provider and profile config no form reads. So the
    daemon resolves it once per profile and publishes it beside the harness's
    own choices -- and on a profile that pins nothing, the answer is an empty
    map rather than an invented id.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            store.update(
                lambda doc: doc.setdefault("providers", {}).update(
                    {
                        "backend": {
                            "models": {
                                "default": "backend-sonnet",
                                "large": "backend-opus",
                            }
                        }
                    }
                )
            )
            pinned = profile.create("pinned")
            store.set_profile_field(pinned.name, "provider", "backend")
            profile.create("plain")

            resp = await client.get("/api/profiles", headers=bearer)
            assert resp.status == 200
            details = {
                row["name"]: row
                for row in (await resp.json())["profile_details"]
            }

            assert details["pinned"]["harness"] == "claude"
            assert details["pinned"]["model_ids"] == {
                "sonnet": "backend-sonnet",
                "opus": "backend-opus",
                # `xlarge` follows `large` where nobody sets it, so the alias
                # the runtime calls fable reaches opus's model.
                "fable": "backend-opus",
            }
            # The same answer under the harness-qualified selector the
            # dashboard's profile picker hands back.
            assert details["pinned:claude"]["model_ids"]["sonnet"] == (
                "backend-sonnet"
            )
            # Codex resolves its own aliases, which no profile decides.
            assert details["pinned:codex"]["model_ids"]["luna"] == "gpt-5.6-luna"
            # Nothing is pinned: Claude Code picks its own sonnet, and the map
            # says so by being empty.
            assert details["plain"]["model_ids"] == {}
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_workspaces_feed_the_directory_picker(home, tmp_path):
    """The web form picks a directory from this list instead of taking one
    typed free-hand — so the endpoint has to carry enough to render it."""
    from aiohttp.test_utils import TestClient, TestServer
    from claude_launcher import workspaces

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/workspaces", headers=bearer)
            assert resp.status == 200
            assert (await resp.json())["workspaces"] == []

            (tmp_path / "proj").mkdir()
            workspaces.add(str(tmp_path / "proj"))
            # read live from the config file: no daemon restart needed after
            # a `claunch workspace add` in another window
            resp = await client.get("/api/workspaces", headers=bearer)
            (entry,) = (await resp.json())["workspaces"]
            assert entry["name"] == "proj"
            assert entry["exists"] is True
            assert entry["path"] == str((tmp_path / "proj").resolve())

        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_workspaces_can_be_registered_and_dropped(home, tmp_path):
    """The registry is editable from the browser as well as the shell.

    The picker stays a picker: a path is typed once, *here*, and checked
    against the daemon's filesystem before it is stored — which is what makes
    it never worth typing again at spawn time.
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            (tmp_path / "hq").mkdir()

            resp = await client.post(
                "/api/workspaces",
                json={"path": str(tmp_path / "hq"), "name": "hq"},
                headers=bearer,
            )
            assert resp.status == 201
            assert (await resp.json())["workspace"] == {
                "name": "hq",
                "path": str((tmp_path / "hq").resolve()),
                "exists": True,
            }
            resp = await client.get("/api/workspaces", headers=bearer)
            assert [w["name"] for w in (await resp.json())["workspaces"]] == ["hq"]

            # a directory that is not there is refused with the reason, which
            # is the whole point of registering rather than typing at spawn
            resp = await client.post(
                "/api/workspaces",
                json={"path": str(tmp_path / "ghost")},
                headers=bearer,
            )
            assert resp.status == 400
            assert "no such directory" in (await resp.json())["error"]

            # ...and an empty body is a bad request, not a 500
            resp = await client.post("/api/workspaces", json={}, headers=bearer)
            assert resp.status == 400

            resp = await client.delete("/api/workspaces/hq", headers=bearer)
            assert resp.status == 200
            resp = await client.get("/api/workspaces", headers=bearer)
            assert (await resp.json())["workspaces"] == []
            # the directory itself is never touched — only the entry goes
            assert (tmp_path / "hq").is_dir()

            resp = await client.delete("/api/workspaces/hq", headers=bearer)
            assert resp.status == 404
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_api_roles_offers_the_packaged_preview_and_roles_are_harness_neutral(
    home, tmp_path
):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.get("/api/roles", headers=bearer)
            assert resp.status == 200
            roles = (await resp.json())["roles"]
            by_name = {r["name"]: r for r in roles}
            assert {"leader", "worker", "reviewer"} <= set(by_name)
            assert "mod" in by_name["leader"]["aliases"]
            assert by_name["reviewer"]["stance"]
            assert "prompt" not in by_name["reviewer"]

            # A role is a mesh membership and the non-Claude harness takes it.
            _register_py_harness()
            app["mesh"].create("team")
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "roled", "profile": "py", "cwd": str(tmp_path),
                    "mesh": "team", "role": "worker",
                },
                headers=bearer,
            )
            assert resp.status == 201, await resp.text()
            assert mgr.get("roled").sdef.role is None
            assert app["mesh"].member_for_session(
                app["mesh"].get("team"), "roled"
            ).role == "worker"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_web_assets_must_revalidate(home, tmp_path):
    """index.html names 'static/app.js' with no version, so if the daemon
    sends no Cache-Control the browser picks a heuristic freshness lifetime
    and stops asking. An upgraded daemon then keeps serving a UI its own
    users cannot get rid of — the failure looks like "the fix did not ship".
    """
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for path in ("/", "/static/app.js", "/static/style.css"):
                resp = await client.get(path)
                assert resp.status == 200, path
                assert "no-cache" in resp.headers.get("Cache-Control", ""), path
                # no-cache is only affordable because the revalidation is
                # cheap: the static handler must still offer a validator
                if path != "/":
                    assert resp.headers.get("ETag") or resp.headers.get(
                        "Last-Modified"
                    ), path

            # API responses are not in the business of caching either way
            resp = await client.get("/api/health")
            assert resp.status == 200
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_codex_claim_that_never_matches_backs_off_and_is_abandoned(
    home, tmp_path, monkeypatch
):
    """A rollout that never records this cwd (a workspace/cwd mismatch on
    restore) must not mean one filesystem scan per second forever: on
    2026-09-11 that scan, slowed by a bloated heap, kept the daemon from
    answering. The interval doubles, and after the grace window the claim
    is abandoned with a warning and an on-screen notice."""
    store.update(lambda doc: doc.update({"harnesses": {"codex": {
        "command": [sys.executable, "-u", "-c", CHILD],
        "home_env": "CODEX_HOME",
        "restore_args": ["resume", "--last"],
    }}}))
    lineage.set_harness(profile.create("codex"), "codex")
    monkeypatch.setattr(codex_sessions, "snapshot", lambda _home: {"old"})
    clock = {"now": 100.0}
    scans = []

    def missing_claim(_home, cwd, known, *, timeout=2.0, poll=0.02):
        scans.append(clock["now"])
        return None

    monkeypatch.setattr(codex_sessions, "claim_new", missing_claim)
    monkeypatch.setattr(
        manager_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="cx", profile="codex", cwd=str(tmp_path)))
        # The launch wait scans from a thread; give it its turn now so its
        # miss is recorded at t=100 like the synchronous wait it replaced.
        await asyncio.sleep(0.05)
        session = mgr.get("cx")
        notices = []
        monkeypatch.setattr(
            session, "notify", lambda text, **kw: notices.append((text, kw)) or 0
        )
        # scans so far: the launch wait, then get()'s first retry, both at t=100.
        # Poll every 0.5 s of fake time for 15 minutes; count the scans.
        while clock["now"] < 100.0 + 15 * 60:
            clock["now"] += 0.5
            mgr.get("cx")
        gaps = [b - a for a, b in zip(scans, scans[1:])]
        await mgr.shutdown_all()
        return gaps, notices, mgr._pending_codex_claims

    gaps, notices, pending = asyncio.run(run())
    assert gaps[1:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]   # doubling...
    assert max(gaps) == 30.0                            # ...capped
    assert len(gaps) < 40                               # not ~900 one-a-second scans
    assert scans[-1] - scans[0] <= 600.0 + 30.0        # abandoned after the window
    assert not pending
    assert len(notices) == 1 and notices[0][1]["level"] == "warn"
    assert "unpinned" in notices[0][0]
