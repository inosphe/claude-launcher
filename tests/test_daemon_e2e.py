"""End-to-end: a real PTY child driven through the SessionManager and the API.

Uses a tiny Python echo program as the harness so the tests run anywhere the
package installs — no claude needed. Exercises the reader-thread pump, pyte
capture, send-keys, wait-for and the aiohttp surface (auth included).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import paths
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
    "    print('echo:' + line)\n"
)


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _screen_text(session) -> str:
    return "\n".join(session.capture())


async def _wait_screen(session, needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _screen_text(session):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{needle!r} never appeared on screen; got:\n{_screen_text(session)}"
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

        # exited sessions stay listed until killed/removed
        assert mgr.get("t1").exited
        mgr.kill("t1")
        with pytest.raises(Exception):
            mgr.get("t1")

    asyncio.run(run())


def test_sessions_json_persistence(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="keep", harness="py", cwd=str(tmp_path)))
        entries = json.loads(paths.sessions_json().read_text(encoding="utf-8"))
        assert entries[0]["def"]["name"] == "keep"
        assert entries[0]["was_running"] is True
        await mgr.shutdown_all()

    asyncio.run(run())


def test_restore_relaunches_recorded_sessions(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="phoenix", harness="py", cwd=str(tmp_path)))
        mgr.persist()
        await mgr.shutdown_all()

        mgr2 = _manager()
        failed = mgr2.restore_all()
        assert failed == []
        session = mgr2.get("phoenix")
        await _wait_screen(session, "READY")
        await mgr2.shutdown_all()

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


def test_api_cflow_monitoring(home, tmp_path, monkeypatch):
    """The dashboard endpoint surfaces runs (and their step reports) keyed
    by (cwd, scope); a run maps 1:1 to the session named like its scope."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.cflow import engine as cflow_engine

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
    cwd = str(Path(tmp_path).resolve())
    writes: list = []

    class FakeSession:
        exited = False
        sdef = SessionDef(name="n1", cwd=cwd)
        screen = ScreenState(80, 24)
        paste = session_mod.Session.paste
        deliver = session_mod.Session.deliver
        _await_readable = session_mod.Session._await_readable
        _started_mono = 0.0
        _input_ready = True  # a session already up; nothing to wait for

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

    nudged = asyncio.run(
        api_mod._nudge_sessions(FakeManager(), cwd, "n1", "cflow: go")
    )
    assert nudged == ["n1"]
    assert writes == [b"\x1b[200~cflow: go\x1b[201~", b"\r"]


def test_api_session_meta_and_workflow_request(home, tmp_path, monkeypatch):
    """A session's own page: its definition, its mesh memberships, and the
    cflow slot it owns — plus the two ways to create a run in that slot."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher import workspaces
    from claude_launcher.cflow import engine as cflow_engine

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
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(mgr.get("s1"), "READY")

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

            # a mesh membership shows up on the session's page
            app["mesh"].create("dev")
            await app["mesh"].join("dev", "s1", handle="impl")
            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            assert (await resp.json())["meshes"][0]["mesh"] == "dev"

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
            assert doc["nudged_sessions"] == ["s1"]
            await _wait_screen(mgr.get("s1"), "a start of workflow 'demo' was requested")

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
            assert (await resp.json())["step_id"] == "one"
            resp = await client.get("/api/sessions/s1/meta", headers=bearer)
            assert (await resp.json())["cflow"]["status"] == "step"
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
            assert (await resp.json())["nudged_sessions"] == []

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


def test_api_session_respawn(home, tmp_path):
    """An exited session relaunches under its own name and definition;
    respawning a live one is refused."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            session = mgr.create(SessionDef(name="rz", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            resp = await client.post("/api/sessions/rz/respawn", headers=bearer)
            assert resp.status == 400  # still running

            await session.send_keys(["quit", "Enter"])
            await session.wait_for("exited", timeout=10.0, threshold=0.5)

            resp = await client.post("/api/sessions/rz/respawn", headers=bearer)
            assert resp.status == 200
            info = await resp.json()
            assert info["name"] == "rz"
            assert info["status"] != "exited"

            revived = mgr.get("rz")
            assert revived is not session
            await _wait_screen(revived, "READY")
            await revived.send_keys(["back", "Enter"])
            await _wait_screen(revived, "echo:back")
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
            assert doc["nudged_sessions"] == ["n1"]
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
            assert (await resp.json())["nudged_sessions"] == ["n1"]
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
            assert doc["nudged_sessions"] == ["n1"]
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
            assert doc["nudged_sessions"] == []

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
            resp = await client.post(
                "/api/cflow/start",
                json={"cwd": str(gated), "scope": "n1", "workflow": "tiny"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["nudged_sessions"] == ["n1"]
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
                json={"name": "api1", "harness": "py", "cwd": str(tmp_path)},
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
                json={"name": "dup", "harness": "py", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 201
            resp = await client.post(
                "/api/sessions",
                json={"name": "dup", "harness": "py", "cwd": str(tmp_path)},
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
                            "pi": {"command": "no-such-program-xyz"},
                        }
                    }
                )
            )
            resp = await client.get("/api/harnesses", headers=bearer)
            assert resp.status == 200
            by_name = {h["name"]: h for h in (await resp.json())["harnesses"]}
            assert by_name["codex"]["available"] is True
            assert by_name["pi"]["available"] is False
            assert by_name["pi"]["program"] == "no-such-program-xyz"
            assert by_name["claude"]["builtin"] is True

            # ...and a harness that is declared but not installed is refused
            # with a message that says so, not a spawn failure
            resp = await client.post(
                "/api/sessions",
                json={"name": "nope", "harness": "pi", "cwd": str(tmp_path)},
                headers=bearer,
            )
            assert resp.status == 400
            assert "not found on PATH" in (await resp.json())["error"]
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


def test_api_roles_offers_the_spawnable_vocabulary(home, tmp_path):
    """The spawn form needs the stance BEFORE anyone commits to a role — it
    is the one thing about a session you cannot read back off its terminal."""
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
            assert "reviewer" in by_name["reviewer"]["prompt"]

            # ...and it is claude's flag, so another harness must not take it
            _register_py_harness()
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "roled", "harness": "py", "cwd": str(tmp_path),
                    "role": "worker",
                },
                headers=bearer,
            )
            assert resp.status == 400
            assert "claude harness" in (await resp.json())["error"]
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
