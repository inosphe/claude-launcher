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
from claude_launcher.daemon.manager import SessionManager

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


def test_api_cflow_monitoring(home, tmp_path):
    """The dashboard endpoint surfaces runs (and their step reports) by cwd."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.cflow import engine as cflow_engine

    (tmp_path / "wf.yaml").write_text(
        "name: demo\nsteps:\n  one:\n    instructions: do one\n    next: two\n"
        "  two:\n    instructions: do two\n",
        encoding="utf-8",
    )
    cflow_engine.start("wf.yaml", context="demo task", cwd=str(tmp_path))
    cflow_engine.report("one is done", "evidence here", cwd=str(tmp_path))
    cflow_engine.next_step(cwd=str(tmp_path))

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
            assert runs[0]["status"] == "step"
            assert runs[0]["step_id"] == "two"
            assert runs[0]["sessions"] == []
            assert runs[0]["reports"][-1]["summary"] == "one is done"
            assert runs[0]["reports"][-1]["details"] == "evidence here"

            # explicit ?cwd= inspection also works (and does not duplicate)
            resp = await client.get(
                "/api/cflow", params={"cwd": str(tmp_path)}, headers=bearer
            )
            assert len((await resp.json())["runs"]) == 1

            # a managed session in that cwd puts the run in the default scan
            mgr.create(SessionDef(name="wf1", harness="py", cwd=str(tmp_path)))
            resp = await client.get("/api/cflow", headers=bearer)
            runs = (await resp.json())["runs"]
            assert len(runs) == 1
            assert runs[0]["sessions"] == ["wf1"]

            # a session whose cwd has no run stays out of the listing
            other = tmp_path / "other"
            other.mkdir()
            mgr.create(SessionDef(name="idle1", harness="py", cwd=str(other)))
            resp = await client.get("/api/cflow", headers=bearer)
            assert len((await resp.json())["runs"]) == 1
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


def test_api_cflow_actions(home, tmp_path):
    """The run-detail endpoint, and human approve/select over the web API."""
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
    cflow_engine.start("wf.yaml", cwd=str(gated))

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
                "/api/cflow/run", params={"cwd": str(gated)}, headers=bearer
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["run"]["status"] == "waiting_approval"
            assert [s["id"] for s in doc["workflow"]["steps"]] == ["ship"]
            assert doc["workflow"]["steps"][0]["gate"] == "approve shipping"

            resp = await client.get("/api/cflow/run", headers=bearer)
            assert resp.status == 400  # cwd required

            # a live session in the run's directory gets nudged on approve
            worker = mgr.create(
                SessionDef(name="n1", harness="py", cwd=str(gated))
            )
            await _wait_screen(worker, "READY")

            # approve over HTTP opens the gate; the agent then fetches the step
            resp = await client.post(
                "/api/cflow/approve", json={"cwd": str(gated)}, headers=bearer
            )
            assert resp.status == 200
            doc = await resp.json()
            assert doc["status"] == "approved"
            assert doc["nudged_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: approved")
            assert cflow_engine.next_step(cwd=str(gated))["status"] == "step"

            # manual (repeat) nudge from the dashboard
            resp = await client.post(
                "/api/cflow/nudge", json={"cwd": str(gated)}, headers=bearer
            )
            assert resp.status == 200
            assert (await resp.json())["nudged_sessions"] == ["n1"]
            await _wait_screen(worker, "echo:cflow: continue")

            # forced state set (goto) from the dashboard: re-gates + nudges
            resp = await client.post(
                "/api/cflow/goto",
                json={"cwd": str(gated), "step": "nope"},
                headers=bearer,
            )
            assert resp.status == 400
            resp = await client.post(
                "/api/cflow/goto",
                json={"cwd": str(gated), "step": "ship", "reason": "redo"},
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
                "/api/cflow/approve", json={"cwd": str(gated)}, headers=bearer
            )
            assert resp.status == 200
            # ...and with nothing left waiting, approve is a clean 400, not a 500
            resp = await client.post(
                "/api/cflow/approve", json={"cwd": str(gated)}, headers=bearer
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
            resp = await client.get("/api/sessions")
            assert resp.status == 401

            bearer = {"Authorization": "Bearer sekrit"}
            resp = await client.get("/api/sessions", headers=bearer)
            assert resp.status == 200

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
