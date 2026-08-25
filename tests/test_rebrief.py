"""Re-briefing: the composition, the task record that feeds it, and its doors.

The unit half exercises :func:`rebrief.compose` against staged (never
started) sessions — a mesh join and a cflow run both key on the registered
name, so nothing here needs a PTY. The API half spins the real app up once
and walks the two verbs plus the task-recording paths (create and spawn),
which is the wiring a client cannot get right on its own.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import rebrief
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


# --------------------------------------------------------------------------- #
# compose
# --------------------------------------------------------------------------- #
def _staged_compose(mgr, mm, *sdefs, target=None, arrange=None) -> str:
    """Stage ``sdefs`` and compose for ``target`` (default: the last one).

    Inside one event loop, because a :class:`Session` builds asyncio
    primitives at construction — staging needs a running loop even though the
    harness never starts.
    """

    async def scenario() -> str:
        for sdef in sdefs:
            mgr.stage(sdef)
        if arrange is not None:
            await arrange()
        return rebrief.compose(
            target or sdefs[-1].name, manager=mgr, mesh_mgr=mm
        )

    return asyncio.run(scenario())


def test_a_bare_session_has_nothing_to_be_told(home, tmp_path):
    """No mesh, no run, no kin, no task -> "" — so the SessionStart hook
    prints nothing and a bare session's /clear stays as quiet as ever."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    block = _staged_compose(
        mgr, mm, SessionDef(name="solo", harness="py", cwd=str(tmp_path))
    )
    assert block == ""


def test_an_unknown_session_is_refused(home):
    mgr = _manager()
    mm = MeshManager(mgr)
    with pytest.raises(ManagerError):
        rebrief.compose("ghost", manager=mgr, mesh_mgr=mm)


def test_compose_restates_parent_mesh_and_task(home, tmp_path):
    """The three sections that carry the onboarding's derived half: who is
    waiting (with the reply command filled in), the live mesh briefing, and
    the recorded opening task."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)

    async def joins():
        mm.create("team")
        with mm.defer_briefing("boss"):
            await mm.join("team", "boss", handle="boss")
        with mm.defer_briefing("w1"):
            await mm.join("team", "w1", handle="w1")

    block = _staged_compose(
        mgr, mm,
        SessionDef(name="boss", harness="py", cwd=str(tmp_path)),
        SessionDef(
            name="w1", harness="py", cwd=str(tmp_path),
            parent="boss", task="build the widget",
        ),
        arrange=joins,
    )
    assert "session: w1" in block
    assert "parent: boss" in block
    assert 'claunch mesh send team boss "..."' in block
    assert "mesh: team" in block  # the live join briefing, reused verbatim
    assert "build the widget" in block
    # pointer style: the task record is restated, never replayed as a turn
    assert "as recorded at creation" in block


def test_compose_names_the_run_this_session_drives(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    cflow_engine.start("linear", cwd=str(proj), scope="w1")
    block = _staged_compose(
        mgr, mm, SessionDef(name="w1", harness="py", cwd=str(proj))
    )
    assert "workflow: linear" in block
    assert "scope: w1" in block
    # the protocol pointer, not the state: status is fetched at read time
    assert "cflow 'status' tool" in block


def test_a_long_task_is_cut_not_dumped(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    block = _staged_compose(
        mgr, mm,
        SessionDef(
            name="w1", harness="py", cwd=str(tmp_path),
            task="x" * (rebrief.TASK_LIMIT + 500),
        ),
    )
    assert "task cut for the re-briefing" in block
    assert len(block) < rebrief.TASK_LIMIT + 1000


# --------------------------------------------------------------------------- #
# the API doors, and the task record end to end
# --------------------------------------------------------------------------- #
def test_the_api_composes_and_delivers(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr, settle=0.05)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions", headers=BEARER,
                json={
                    "name": "root", "profile": "py", "cwd": str(tmp_path),
                    "task": "count the beans",
                },
            )
            assert resp.status == 201, await resp.text()
            # The task was recorded at creation, so the rebrief can restate it.
            resp = await client.get("/api/sessions/root/rebrief", headers=BEARER)
            body = await resp.json()
            assert "session: root" in body["block"]
            assert "count the beans" in body["block"]
            # The spawn path records the child's task the same way.
            resp = await client.post(
                "/api/sessions/root/children", headers=BEARER,
                json={"name": "kid", "task": "carry the beans", "mesh": "-"},
            )
            assert resp.status == 201, await resp.text()
            assert mgr.get("kid").sdef.task == "carry the beans"
            # POST is the operator's push: same text, typed into the terminal.
            resp = await client.post("/api/sessions/kid/rebrief", headers=BEARER)
            body = await resp.json()
            assert body["ok"] is True
            assert body["empty"] is False
        finally:
            for name in ("kid", "root"):
                try:
                    mgr.kill(name, force=True)
                except Exception:
                    pass
            await client.close()

    asyncio.run(scenario())
