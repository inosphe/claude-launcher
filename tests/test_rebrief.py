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
from dataclasses import replace

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
# content ids: what is addressable, what is merely recallable, and the pull
# --------------------------------------------------------------------------- #
def test_the_task_rides_with_its_id_and_the_id_names_the_uncut_text(
    home, tmp_path
):
    """The id is printed next to the prose, never on its own, because the
    check it exists for is "is this id attached to text in my context?".
    And it digests the WHOLE task, so the block's own cut does not change
    it -- which is exactly what makes recalling it worth a turn."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    task = "count the beans. " * 200          # comfortably past TASK_LIMIT
    block = _staged_compose(
        mgr, mm,
        SessionDef(name="w1", harness="py", cwd=str(tmp_path), task=task),
    )
    ident = rebrief.block_digest(task)
    assert f"text id: {ident}" in block
    assert "task cut for the re-briefing" in block       # the copy IS cut
    # ...and the id survived the cut: it is the uncut task's digest, not the
    # printed excerpt's.
    assert ident != rebrief.block_digest(block.split("text id: ")[1])

    found = rebrief.recall("w1", ident, manager=mgr, mesh_mgr=mm)
    assert found["status"] == "recalled"
    assert found["kind"] == "task"
    assert found["text"] == task.strip()                 # whole, not excerpt


def test_a_stale_id_is_told_so_and_never_served_the_nearest_thing(
    home, tmp_path
):
    """The one answer a recall must not give is a plausible substitute: a
    stance the mesh has replaced is precisely what an agent must stop acting
    from."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)

    async def scenario():
        mgr.stage(
            SessionDef(name="w1", harness="py", cwd=str(tmp_path), task="dig")
        )
        return rebrief.recall("w1", "ffffffffffff", manager=mgr, mesh_mgr=mm)

    found = asyncio.run(scenario())
    assert found["status"] == "stale_id"
    assert "text" not in found
    assert [row["kind"] for row in found["current"]] == ["task"]
    assert "do not go on acting from what you remember" in found["note"]


def test_a_recall_needs_an_id_at_all(home, tmp_path):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)
    found = rebrief.recall("w1", "  ", manager=mgr, mesh_mgr=mm)
    assert found["status"] == "error"


def test_a_stance_the_session_already_carries_is_addressable_but_not_named(
    home, tmp_path
):
    """The split ``given`` draws, and the whole reason it exists.

    A session spawned INTO its role carries the stance in its own system
    prompt, so a briefing pastes a pointer and no id ever arrives next to
    prose. Naming that id at a reminder would guarantee a miss and buy a
    recall of text the agent already holds -- so ``given_ids`` withholds it,
    while ``recall`` still serves it to anyone who asks by name."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)

    async def joins():
        mm.create("team")
        with mm.defer_briefing("w1"):
            # The SAME role the session was spawned as: a clash between the
            # two is the third pasted shape, and not what this test is about.
            await mm.join("team", "w1", handle="w1", role="worker")

    async def scenario():
        mgr.stage(SessionDef(name="w1", harness="py", cwd=str(tmp_path), task="dig"))
        # A role is a claude-harness flag, so it cannot be staged onto the
        # stub harness these tests run on -- but the state it produces can
        # be, and that state is all `stance_carried` reads.
        sess = mgr.get("w1")
        sess.sdef = replace(sess.sdef, role="worker")
        await joins()
        return (
            rebrief.addressable("w1", manager=mgr, mesh_mgr=mm),
            rebrief.given_ids("w1", manager=mgr, mesh_mgr=mm),
            rebrief.compose("w1", manager=mgr, mesh_mgr=mm),
        )

    known, named, block = asyncio.run(scenario())
    stance = [d for d, e in known.items() if e["kind"].startswith("stance")]
    assert stance, known
    ident = stance[0]
    # Addressable: a pull by that id still works.
    assert rebrief.recall(
        "w1", ident, manager=mgr, mesh_mgr=mm
    )["status"] == "recalled"
    # Not named, and not pasted -- the two go together.
    assert ident not in [d for d, _ in named]
    assert ident not in block
    assert [k for _, k in named] == ["task"]


def test_a_stance_the_session_does_not_carry_is_pasted_with_its_id(
    home, tmp_path
):
    """The mirror case: no role on the session, so the briefing's paste is
    the only copy the agent will ever get -- and the id rides on it."""
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr)

    async def scenario():
        mgr.stage(SessionDef(name="w1", harness="py", cwd=str(tmp_path)))
        mm.create("team")
        with mm.defer_briefing("w1"):
            await mm.join("team", "w1", handle="w1", role="worker")
        return (
            rebrief.given_ids("w1", manager=mgr, mesh_mgr=mm),
            rebrief.compose("w1", manager=mgr, mesh_mgr=mm),
        )

    named, block = asyncio.run(scenario())
    assert [k for _, k in named] == ["stance (team)"]
    ident = named[0][0]
    assert f"[text id: {ident}]" in block
    # And it is the canonical stance's id, so a recall answers with the whole
    # text even where _INLINE_STANCE cut the pasted copy.
    found = rebrief.recall("w1", ident, manager=mgr, mesh_mgr=mm)
    assert found["status"] == "recalled"
    assert found["kind"] == "stance (team)"


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
            # ?id= is the narrow door: one addressed block, by the id the
            # briefing printed beside it. GET only -- a pull is the asking
            # agent's own turn, not something to type at somebody.
            ident = rebrief.block_digest("carry the beans")
            resp = await client.get(
                f"/api/sessions/kid/rebrief?id={ident}", headers=BEARER
            )
            body = await resp.json()
            assert body["status"] == "recalled"
            assert body["kind"] == "task"
            assert body["text"] == "carry the beans"
            assert "block" not in body          # the block is the other door
            resp = await client.get(
                "/api/sessions/kid/rebrief?id=ffffffffffff", headers=BEARER
            )
            assert (await resp.json())["status"] == "stale_id"
            # and an unknown session is refused, exactly as the whole-block
            # door refuses it -- the narrow door does not become a way to
            # ask about sessions that are not there.
            resp = await client.get(
                f"/api/sessions/ghost/rebrief?id={ident}", headers=BEARER
            )
            assert resp.status == 400
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


def test_an_unreachable_daemon_points_at_the_command_that_was_actually_run(
    home, monkeypatch, capsys
):
    """After ``--id``, the recovery line has to name ``--id``.

    The whole-briefing form tells the caller to run ``claunch rebrief`` again
    because "it is the same command". For a session that called with an id
    that sentence is wrong twice: it is a different command, and a caller
    following it literally gets the whole briefing back without the one block
    it came for. That left the ``--id`` caller with a visible error and no
    visible way out -- half of what this branch exists to give it.
    """
    from claude_launcher import cli_sessions
    from claude_launcher.daemon_client import DaemonClientError

    monkeypatch.setenv("CLAUNCH_SESSION", "s9")

    def down(*a, **kw):
        raise DaemonClientError("daemon did not come up within 15s")

    monkeypatch.setattr(cli_sessions.daemon_client, "ensure_running", down)
    rc = cli_sessions._cmd_rebrief(
        argparse.Namespace(session=None, id="a3f9c2b10de4")
    )
    out, err = capsys.readouterr()

    assert rc == 0  # still a hook; failing it un-compacts nothing
    assert "claunch rebrief --id a3f9c2b10de4" in out
    # ...and not the sentence written for the other caller, which would send
    # this one off to fetch the whole briefing instead of its block.
    assert "it is the same command" not in out
    assert "who is reachable on your mesh" not in out
    # 'could not reach the daemon' and 'no such id' stay distinguishable, and
    # this block says which one it is by naming where the other one arrives.
    assert "exit 1" in out
    assert not err  # the half claude reads back is stdout


def test_the_two_unavailable_forms_name_different_missing_things(home):
    """One caller lost the whole briefing, the other lost one block.

    Both reach this branch through the same failure, so nothing but the
    argument tells them apart. Naming the whole briefing at a caller that
    asked for one id overstates what is gone.
    """
    from claude_launcher import cli_sessions

    whole = cli_sessions._rebrief_unavailable("s9", Exception("no daemon"))
    one = cli_sessions._rebrief_unavailable(
        "s9", Exception("no daemon"), "c0ffee123456"
    )

    assert "opening task" in whole and "opening task" not in one
    assert "c0ffee123456" in one and "c0ffee123456" not in whole
    assert "[text id: c0ffee123456]" in one.splitlines()[1]
    for block in (whole, one):
        assert block.startswith("---") and block.rstrip().endswith("---")
        assert "no daemon" in block  # the cause survives either way


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
