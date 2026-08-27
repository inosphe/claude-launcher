"""Re-briefing: the composition, the task record that feeds it, and its doors.

The unit half exercises :func:`rebrief.compose` against staged (never
started) sessions — a mesh join and a cflow run both key on the registered
name, so nothing here needs a PTY. The API half spins the real app up once
and walks the two verbs plus the task-recording paths (create and spawn),
which is the wiring a client cannot get right on its own.
"""

from __future__ import annotations

import argparse
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
    done_when: the diff is committed
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
    # ...with one exception, and it is the line a resuming agent needs
    # most: whether the step it is being handed back is nearly done.
    # The step BODY still stays behind the pointer.
    assert "done when: the diff is committed" in block
    assert "do one" not in block


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


# --------------------------------------------------------------------------- #
# the hook's own failure: an unreachable daemon must not cost the block
# --------------------------------------------------------------------------- #
def test_an_unreachable_daemon_answers_on_stdout_not_stderr(
    home, monkeypatch, capsys
):
    """The hook fires once, at the moment the context was lost.

    Before: ``ensure_running`` raised, the CLI printed ``error: ...`` on
    stderr and exited 1, and stdout — the half claude reads back into context
    — was empty. The session came out of its compaction with no re-briefing
    and no idea that it was missing one. Observed once in the wild: s167,
    2026-08-26T13:19:27Z, "daemon did not come up within 15s", 21s after its
    compaction (daemon.log agrees at 22:19:27 KST, a spawned daemon losing the
    singleton lock).
    """
    from claude_launcher import cli_sessions
    from claude_launcher.daemon_client import DaemonClientError

    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    def down(*a, **kw):
        raise DaemonClientError("daemon did not come up within 15s")

    monkeypatch.setattr(cli_sessions.daemon_client, "ensure_running", down)
    rc = cli_sessions._cmd_rebrief(argparse.Namespace(session=None))
    out, err = capsys.readouterr()

    assert rc == 0  # a non-blocking hook; failing it un-compacts nothing
    assert "claunch rebrief: unavailable" in out
    assert "daemon did not come up within 15s" in out
    assert "claunch rebrief" in out  # the command that recovers it
    assert out.strip()  # the part that matters is never on stderr alone
    assert "unavailable" not in err


def test_the_unavailable_block_states_what_is_missing_without_guessing_it(home):
    """It names the sections it could not fetch and stops there.

    Every one of them is read from the daemon, and the daemon is what failed.
    A block that filled them in from the environment would hand the agent
    stale answers it cannot tell from fresh ones — which is worse than the
    gap, because the gap is visible.
    """
    from claude_launcher import cli_sessions

    block = cli_sessions._rebrief_unavailable("s9", Exception("no daemon"))
    assert "session: s9" in block
    for missing in ("mesh", "cflow run", "parent", "opening task"):
        assert missing in block
    assert block.startswith("---") and block.rstrip().endswith("---")


def test_a_page_long_completion_test_is_capped(home, tmp_path, monkeypatch):
    """``done_when`` is a sentence by design; the cap is for the one that
    isn't, so a runaway workflow cannot crowd out the sections nothing else
    can serve."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "wordy.yaml").write_text(
        "name: wordy\nsteps:\n  one:\n    instructions: do one\n"
        "    done_when: >\n      " + ("every last box is ticked. " * 60) + "\n",
        encoding="utf-8",
    )
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    cflow_engine.start("wordy", cwd=str(proj), scope="w1")
    block = _staged_compose(
        mgr, mm, SessionDef(name="w1", harness="py", cwd=str(proj))
    )
    assert "'status' has it whole" in block
    line = [ln for ln in block.splitlines() if ln.startswith("done when: ")][0]
    assert len(line) < rebrief.DONE_WHEN_LIMIT + 80
