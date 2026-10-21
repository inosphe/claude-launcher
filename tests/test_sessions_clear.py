"""The web cleanup: ``DELETE /api/sessions`` and its dry-run plan.

``claunch clear-sessions [--logs]`` has always dropped exited records; the
dashboard's bulk bar now presses the same route, which is why the route grew
``?dry_run=1``. The page cannot count the plan itself — its poll leaves
archived records out and only the daemon knows which records a mesh row
still holds — so the confirmation is built from this answer. What these
tests hold to: the plan changes nothing, the clear drops exactly what the
plan named, the captured output is taken only when asked for, round reports
are never touched (they live outside ``sessions/<name>/`` by design), and a
record a mesh still names survives both calls.
"""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager
from claude_launcher.daemon.session import DeadSession

BEARER = {"Authorization": "Bearer sekrit"}


class FakeLive(DeadSession):
    """A record that reads as running until its shutdown lands.

    Built on ``DeadSession`` so the registry can persist it like any other
    record; what it adds is a live ``status`` and a shutdown that ends the
    program, which is enough to prove a clear leaves running sessions alone.
    """

    def __init__(self, sdef) -> None:
        super().__init__(sdef, exit_code=None)
        self.exited = False
        self.graces: list = []

    def status(self, threshold=None) -> str:
        return "exited" if self.exited else "idle"

    def kill(self, *, force: bool = False) -> None:
        self.graces.append(("kill", force))

    async def shutdown(self, grace: float = 5.0) -> None:
        self.graces.append(("shutdown", grace))
        self.exited = True
        self.exit_code = 0


def _record(mgr: SessionManager, name: str, **kw) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd="C:/work"), exit_code=0, **kw
    )
    mgr._sessions[name] = record
    return record


def _live(mgr: SessionManager, name: str) -> FakeLive:
    record = FakeLive(SessionDef(name=name, harness="claude", cwd="C:/work"))
    mgr._sessions[name] = record
    return record


def _plant(name: str) -> tuple:
    """An output directory and a round report for ``name``, as a real run
    would leave them: the report is a sibling of ``sessions/``, never inside
    it (``daemon/paths.py``), which is the boundary the clear must keep."""
    out = paths.session_dir(name)
    out.mkdir(parents=True, exist_ok=True)
    (out / "output.log").write_text("what the program printed\n", encoding="utf-8")
    report = paths.session_reports(name)
    report.mkdir(parents=True, exist_ok=True)
    (report / "20261021T0000-claunch-q4cqy.1.html").write_text(
        "<html>round</html>", encoding="utf-8"
    )
    return out, report


def _fleet(mgr: SessionManager) -> None:
    """One of each kind. ``held`` is the only exited record a mesh names, so
    it is the one both calls must leave standing."""
    _record(mgr, "one")
    _record(mgr, "paused", paused_at="2026-10-21T00:00:00+00:00")
    _record(mgr, "gone", archived_at="2026-10-20T00:00:00+00:00")
    _live(mgr, "busy")


async def _mesh_with_held(mgr: SessionManager, tmp_path) -> MeshManager:
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    mm.create("team")
    held = _live(mgr, "held")
    await mm.join("team", "held")
    # The row outlives the terminal, which is exactly the state the guard is
    # for: an exited record a mesh still names.
    held.exited = True
    held.exit_code = 0
    return mm


def test_dry_run_names_the_plan_and_changes_nothing(home, tmp_path):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        _fleet(mgr)
        mm = await _mesh_with_held(mgr, tmp_path)
        out, report = _plant("one")
        app = build_app(mgr, "sekrit", started_at=0.0, mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete("/api/sessions?dry_run=1", headers=BEARER)
            assert resp.status == 200, await resp.text()
            plan = await resp.json()
            assert plan["dry_run"] is True
            assert plan["logs"] is False
            assert plan["stopped"] == []
            assert set(plan["removed"]) == {"one", "paused", "gone"}
            assert plan["marks"] == {"paused": 1, "archived": 1}
            assert [k["name"] for k in plan["kept"]] == ["held"]
            assert [m["mesh"] for m in plan["kept"][0]["meshes"]] == ["team"]

            # Nothing moved: every record is still there, and so is the
            # output the plan would take with ?logs=1.
            for name in ("one", "paused", "gone", "held", "busy"):
                assert mgr.get(name) is not None
            assert (out / "output.log").exists()
            assert report.exists()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_dry_run_refuses_the_flags_that_change_the_plan(home, tmp_path):
    # `force` and `running` decide what the clear would do; a plan that
    # ignored them would be a plan for a different call. Refused, not
    # silently dropped.
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        _record(mgr, "one")
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for query in ("?dry_run=1&force=1", "?dry_run=1&running=1"):
                resp = await client.delete("/api/sessions" + query, headers=BEARER)
                assert resp.status == 400, (query, resp.status)
                assert "dry_run" in (await resp.json())["error"]
            assert mgr.get("one") is not None
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_clear_drops_the_records_and_leaves_reports_and_live_sessions(home, tmp_path):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        _fleet(mgr)
        mm = await _mesh_with_held(mgr, tmp_path)
        out, report = _plant("one")
        busy = mgr.get("busy")
        app = build_app(mgr, "sekrit", started_at=0.0, mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete("/api/sessions", headers=BEARER)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert set(body["removed"]) == {"one", "paused", "gone"}
            assert [k["name"] for k in body["kept"]] == ["held"]
            assert body["logs"] is False

            for name in ("one", "paused", "gone"):
                assert name not in mgr._sessions
            # The mesh-held record and the running session are untouched.
            assert mgr.get("held").exited
            assert mgr.get("busy") is busy and not busy.exited
            # No logs: the captured output stays, and so does the report.
            assert (out / "output.log").exists()
            assert report.exists()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_clear_with_logs_takes_the_output_directory_and_never_the_report(
    home, tmp_path
):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        _record(mgr, "one")
        out, report = _plant("one")
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.delete("/api/sessions?logs=1", headers=BEARER)
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["removed"] == ["one"]
            assert body["logs"] is True

            assert "one" not in mgr._sessions
            assert not out.exists()
            # The report is a sibling of sessions/, so the rmtree of the
            # session directory cannot reach it.
            assert report.exists()
            assert (report / "20261021T0000-claunch-q4cqy.1.html").exists()
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_clear_keeps_a_record_a_mesh_names(home, tmp_path):
    # The guard the plan and the clear share: the row and the record go
    # together, and this call is the one that would strand the row.
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = await _mesh_with_held(mgr, tmp_path)
        app = build_app(mgr, "sekrit", started_at=0.0, mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = await (await client.delete("/api/sessions", headers=BEARER)).json()
            assert body["removed"] == []
            assert [k["name"] for k in body["kept"]] == ["held"]
            assert mgr.get("held") is not None
            assert mm.meshes_for_session("held")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
