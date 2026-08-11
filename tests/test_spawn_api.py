"""The spawn endpoint end to end: one call, a whole teammate.

``POST /api/sessions/{name}/children`` is where the three features meet — a
child session, its mesh membership, and the topology it starts with. The unit
tests cover each piece; this covers the wiring between them, which is the part
a client cannot get right on its own.
"""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import store, workspaces
from claude_launcher.cflow import state as cflow_state
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client



def _declare_review_workflow(cwd) -> None:
    """A one-step workflow declared in ``cwd``, so preflight can find it."""
    d = cwd / ".claunch" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.yaml").write_text(
        "name: review\n"
        "start: look\n"
        "steps:\n"
        "  look:\n"
        "    title: Look\n"
        "    instructions: look at it\n",
        encoding="utf-8",
    )


def test_spawn_creates_a_child_that_inherits_and_joins(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(
                SessionDef(name="lead", harness="py", cwd=str(tmp_path), args=["-x"])
            )
            await mm.join("team", "lead", handle="lead")

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "mesh": "team", "role": "worker"},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()

            # the child is a copy of its parent, and knows whose it is
            child = body["session"]
            assert child["name"] == "w1"
            assert child["harness"] == "py"
            assert child["cwd"] == str(tmp_path)
            assert child["args"] == ["-x"]
            assert child["parent"] == "lead"
            # never restored: the tree is a runtime arrangement (see spawn())
            assert child["restore"] is False

            # ... and it is in the mesh, reaching its parent only
            assert body["mesh"]["ok"] is True
            assert body["mesh"]["handle"] == "w1"
            assert body["mesh"]["connected_to"] == ["lead"]
            assert mm.get("team").neighbours("w1") == ["lead"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_child_lands_in_its_parents_mesh_without_being_told(home, tmp_path):
    """The default that makes a spawn a pair.

    Naming no mesh used to make a child nobody could speak to — the failure
    this repo actually hit: a session created, briefed with its workflow, and
    left with nowhere to send the result.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="boss")

            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            assert resp.status == 201
            body = await resp.json()
            assert body["mesh"]["ok"] is True
            assert body["mesh"]["mesh"] == "team"
            # ...connected to the parent under the handle the parent actually
            # holds, which is not its session name here
            assert body["mesh"]["connected_to"] == ["boss"]
            assert mm.get("team").neighbours("w1") == ["boss"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_parent_in_no_mesh_gets_one_opened_for_the_pair(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            assert mm.list() == []

            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            assert resp.status == 201
            body = await resp.json()
            # named after the parent, holding exactly the two of them
            assert body["mesh"]["mesh"] == "lead"
            assert sorted(mm.get("lead").members) == ["lead", "w1"]
            assert mm.get("lead").connected("lead", "w1") is True

            # ...and the next spawn finds that one mesh and joins it, rather
            # than opening a second: the subtree stays in one room
            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w2"}, headers=BEARER
            )
            assert resp.status == 201
            assert (await resp.json())["mesh"]["mesh"] == "lead"
            assert [m.name for m in mm.list()] == ["lead"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_parent_in_several_meshes_must_say_which(home, tmp_path):
    """Guessing is the one mistake that cannot be walked back: a wrong guess
    does not fail, it broadcasts."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            for name in ("alpha", "beta"):
                mm.create(name)
                await mm.join(name, "lead", handle="lead")

            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            assert resp.status == 400
            error = (await resp.json())["error"]
            assert "alpha, beta" in error
            assert mgr.children("lead") == []  # nothing half-made

            # naming one settles it
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "mesh": "beta"},
                headers=BEARER,
            )
            assert resp.status == 201
            assert (await resp.json())["mesh"]["mesh"] == "beta"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_child_can_be_started_outside_every_mesh(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "mesh": "-"},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            assert "mesh" not in body
            assert list(mm.get("team").members) == ["lead"]
            # still a child, though: lineage is not a mesh property
            assert body["session"]["parent"] == "lead"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_second_child_cannot_reach_the_first_until_connected(home, tmp_path):
    """The arrangement the whole feature exists for: a lead with two workers
    that do not talk to each other until it says so."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")
            for name in ("w1", "w2"):
                resp = await client.post(
                    "/api/sessions/lead/children",
                    json={"name": name, "mesh": "team"},
                    headers=BEARER,
                )
                assert resp.status == 201

            mesh = mm.get("team")
            assert mesh.connected("w1", "w2") is False
            assert mesh.neighbours("lead") == ["w1", "w2"]

            # the lead wires them together — it spawned both, so it may
            resp = await client.patch(
                "/api/mesh/team/members/w1/links/w2",
                json={"enabled": True, "actor": "lead"},
                headers=BEARER,
            )
            assert resp.status == 200
            assert mesh.connected("w1", "w2") is True

            # a worker may not undo it: it spawned nothing
            resp = await client.patch(
                "/api/mesh/team/members/w1/links/w2",
                json={"enabled": False, "actor": "w1"},
                headers=BEARER,
            )
            assert resp.status == 400
            assert mesh.connected("w1", "w2") is True

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_connect_lets_a_child_reach_a_named_peer_from_the_start(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")
            mgr.create(SessionDef(name="qa", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "qa", handle="qa")

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "mesh": "team", "connect": ["qa"]},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            assert body["mesh"]["connected_to"] == ["lead", "qa"]
            assert mm.get("team").neighbours("w1") == ["lead", "qa"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_policy_refuses_with_403_not_400(home, tmp_path):
    """A refused spawn is well-formed, so it is not a bad request — the
    distinction is what tells an agent to stop retrying and ask a human."""
    _register_py_harness()
    store.update(lambda doc: doc.update({"spawn": {"max_children": 1}}))

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            assert resp.status == 201
            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w2"}, headers=BEARER
            )
            assert resp.status == 403
            assert "max_children" in (await resp.json())["error"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_children_reports_the_subtree_and_the_remaining_budget(home, tmp_path):
    _register_py_harness()
    store.update(lambda doc: doc.update({"spawn": {"max_children": 3}}))

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            await client.post(
                "/api/sessions/w1/children", json={"name": "w1a"}, headers=BEARER
            )

            resp = await client.get("/api/sessions/lead/children", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert [c["name"] for c in body["children"]] == ["w1"]
            assert body["descendants"] == ["w1", "w1a"]  # the whole subtree
            assert body["children_remaining"] == 2
            assert body["can_spawn"] is True

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_child_runs_in_the_workspace_it_was_given(home, tmp_path):
    """The end the feature exists for: an agent names a workspace and the
    child's PTY actually starts there, without a path ever being typed."""
    _register_py_harness()
    elsewhere = tmp_path / "hq"
    elsewhere.mkdir()
    workspaces.add(str(elsewhere), name="hq")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "workspace": "hq"},
                headers=BEARER,
            )
            assert resp.status == 201
            assert (await resp.json())["session"]["cwd"] == str(elsewhere.resolve())

            # the parent stayed where it was — a child moves, not the tree
            assert mgr.get("lead").sdef.cwd == str(tmp_path)

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_children_lists_the_workspaces_a_child_may_be_sent_to(home, tmp_path):
    """An agent cannot read ~/.claunch.yaml's registry, so the budget report
    is where the names come from — otherwise 'workspace' is a field it can
    only guess at."""
    _register_py_harness()
    elsewhere = tmp_path / "hq"
    elsewhere.mkdir()
    workspaces.add(str(elsewhere), name="hq")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))

            resp = await client.get("/api/sessions/lead/children", headers=BEARER)
            body = await resp.json()
            assert "workspace" in body["may_choose"]
            assert [w["name"] for w in body["workspaces"]] == ["hq"]

            store.update(
                lambda doc: doc.update({"spawn": {"allow_workspace": False}})
            )
            resp = await client.get("/api/sessions/lead/children", headers=BEARER)
            body = await resp.json()
            assert "workspaces" not in body  # locked: no list, no field

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_an_unregistered_workspace_is_refused_with_403(home, tmp_path):
    _register_py_harness()
    store.update(lambda doc: doc.update({"spawn": {"allow_workspace": True}}))

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "workspace": "nowhere"},
                headers=BEARER,
            )
            assert resp.status == 403
            assert "no workspace named" in (await resp.json())["error"]
            # refused before anything was built: no half-made session left
            assert mgr.children("lead") == []

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_an_exited_parent_cannot_spawn(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            mgr.kill("lead", force=True)
            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER
            )
            assert resp.status == 400
            assert "exited" in (await resp.json())["error"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_spawn_role_becomes_the_members_role_in_the_mesh(home, tmp_path):
    """`role` on a spawn reaches the mesh join. (Its other half — the system
    prompt stance — is claude-harness only; see test_spawn.py for that split,
    which cannot be exercised here without spawning a real claude.)"""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")

            resp = await client.post(
                "/api/sessions/lead/children",
                # 'qa1' would infer reviewer anyway; 'w' would infer worker.
                # Naming the role explicitly is what is under test.
                json={"name": "qa1", "mesh": "team", "handle": "w",
                      "role": "reviewer"},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            assert body["mesh"]["role"] == "reviewer"
            assert mm.get("team").members["w"].role == "reviewer"
            # ...and the py harness takes no stance, without failing the spawn
            assert body["session"]["role"] is None

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# onboarding: a session created with a job, not just a program
#
# The composition (create -> join -> start run -> brief) has always existed on
# the spawn endpoint. These cover it on the human path, and cover the two
# properties that made it worth lifting out: nothing is built until the
# request is known to be honourable, and the briefing arrives as ONE block.
# --------------------------------------------------------------------------- #
def test_create_joins_a_mesh_and_starts_a_run_in_one_call(home, tmp_path):
    _register_py_harness()
    _declare_review_workflow(tmp_path)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "w1", "harness": "py", "cwd": str(tmp_path),
                    "mesh": "team", "handle": "worker_1",
                    "workflow": "review", "task": "take the API",
                },
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()

            # the session's own fields stay at the top level, as they always
            # have, with each onboarding leg reported beside them
            assert body["name"] == "w1"
            assert body["mesh"]["ok"] is True
            assert body["mesh"]["handle"] == "worker_1"
            assert body["workflow"]["ok"] is True, body["workflow"]
            assert body["workflow"]["scope"] == "w1"
            assert body["task"]["ok"] is True
            assert mm.get("team").members["worker_1"].session == "w1"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_an_unknown_mesh_is_refused_before_anything_is_built(home, tmp_path):
    """The reason preflight exists: the system prompt is fixed when the PTY
    starts, so the mesh has to be known first — which also turns 'the session
    came up, then the join failed' into a 400 with nothing left behind."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            resp = await client.post(
                "/api/sessions",
                json={"name": "w1", "harness": "py", "cwd": str(tmp_path),
                      "mesh": "nope"},
                headers=BEARER,
            )
            assert resp.status == 400
            assert "no mesh named" in (await resp.json())["error"]
            assert [s.sdef.name for s in mgr.list()] == []

            resp = await client.post(
                "/api/sessions",
                json={"name": "w1", "harness": "py", "cwd": str(tmp_path),
                      "workflow": "ghost"},
                headers=BEARER,
            )
            assert resp.status == 400
            assert "no workflow named" in (await resp.json())["error"]
            assert [s.sdef.name for s in mgr.list()] == []

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_taken_handle_is_refused_before_the_session_exists(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")

            resp = await client.post(
                "/api/sessions",
                json={"name": "w1", "harness": "py", "cwd": str(tmp_path),
                      "mesh": "team", "handle": "lead"},
                headers=BEARER,
            )
            assert resp.status == 400
            assert "already taken" in (await resp.json())["error"]
            assert "w1" not in [s.sdef.name for s in mgr.list()]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_identity_goes_to_the_system_prompt_but_the_roster_never_does(home, tmp_path):
    """The split the two channels are for: a handle holds for the session's
    life and belongs in the prompt; who it can reach is rewired mid-session
    and must stay in the briefing, which is re-derived every time."""
    from claude_launcher.daemon import onboard

    _declare_review_workflow(tmp_path)
    plan = onboard.preflight(
        {"mesh": "team", "handle": "worker_1", "workflow": "review"},
        mesh_mgr=_FakeMeshMgr(),
        session_name="w1",
        cwd=str(tmp_path),
        harness="claude",
    )
    assert "worker_1" in plan.identity
    assert "review" in plan.identity
    # never the roster: it goes stale the moment connect/disconnect runs
    assert "members:" not in plan.identity
    assert "reach" in plan.identity  # ...it points at the briefing instead

    # and nothing at all for a harness with no system prompt to append to
    bare = onboard.preflight(
        {"mesh": "team", "handle": "worker_1"},
        mesh_mgr=_FakeMeshMgr(), session_name="w1",
        cwd=str(tmp_path), harness="py",
    )
    assert bare.identity == ""


def test_a_child_is_told_who_is_waiting_on_it(home, tmp_path):
    """Both channels, because they answer different questions.

    The system prompt survives a compaction, so it is where "whose work is
    this" belongs. The opening block is what is on screen during the first
    turn — the turn where a child decides whether reporting back is part of
    the job — so it carries the same fact and the command that acts on it.
    """
    from claude_launcher.daemon import onboard

    plan = onboard.preflight(
        {"mesh": "team", "handle": "w1"},
        mesh_mgr=_FakeMeshMgr(),
        session_name="w1",
        cwd=str(tmp_path),
        harness="claude",
        parent="lead",
    )
    assert "'lead'" in plan.identity
    # the handle the parent actually holds, not its session name
    assert plan.parent_handle == "boss"
    assert "'boss'" in plan.identity

    block = onboard._parent_block(plan)
    assert "parent: lead" in block
    assert 'claunch mesh send team boss "..."' in block


def test_a_child_with_no_channel_back_is_told_that_too(home, tmp_path):
    """The silent failure this feature exists to end. A child that cannot
    reach its parent must say so, not work on and finish into the void."""
    from claude_launcher.daemon import onboard

    plan = onboard.preflight(
        {}, mesh_mgr=_FakeMeshMgr(), session_name="w1",
        cwd=str(tmp_path), harness="claude", parent="lead",
    )
    assert "no mesh" in plan.identity
    assert "no shared mesh" in onboard._parent_block(plan)


def test_the_opening_block_leads_with_the_parent(home, tmp_path):
    """Ordering, not just presence: an assignment read before its author is
    an assignment whose result has nowhere to go."""
    _register_py_harness()
    _declare_review_workflow(tmp_path)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        seen: dict = {}
        real_build = harness_mod.build_command

        def spy(sdef, **kw):
            if sdef.name == "w1":
                seen["opening"] = kw.get("opening", "")
            return real_build(sdef, **kw)

        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")
            harness_mod.build_command = spy
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "workflow": "review", "task": "take the API"},
                headers=BEARER,
            )
            assert resp.status == 201
        finally:
            harness_mod.build_command = real_build
            await mgr.shutdown_all()
            await client.close()

        opening = seen["opening"]
        assert opening.startswith("---\n# claunch: the session that created you")
        assert "parent: lead" in opening
        # ...ahead of the mesh briefing, the run assignment and the task
        assert opening.index("parent: lead") < opening.index("workflow: review")
        assert opening.endswith("take the API")

    asyncio.run(run())


class _FakeMeshMgr:
    """Just the lookups preflight makes — no daemon, no event loop."""

    class _Member:
        handle = "boss"

    class _Mesh:
        name = "team"
        members: dict = {}

    def get(self, name):
        if name != "team":
            from claude_launcher.daemon.mesh import MeshError

            raise MeshError(f"no mesh named {name!r}")
        return self._Mesh()

    def list(self):
        return [self._Mesh()]

    def resolve_sender(self, name, sender):
        return self._Member() if name == "team" and sender == "lead" else None


def test_the_join_and_the_run_happen_before_the_harness_starts(home, tmp_path):
    """The ordering the opening message depends on.

    A harness that takes its first message as an argument has to be handed one
    before it is spawned -- which means the mesh join and the cflow run, whose
    briefing and assignment make up that message, have to be done while the
    session exists but is not yet running. So by the time argv is built, both
    have already landed.
    """
    _register_py_harness()
    _declare_review_workflow(tmp_path)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        seen: dict = {}
        real_build = harness_mod.build_command

        def spy(sdef, **kw):
            seen["opening"] = kw.get("opening", "")
            seen["in_mesh"] = "worker_1" in mm.get("team").members
            seen["has_run"] = sdef.name in cflow_state.scopes_in(sdef.cwd)
            return real_build(sdef, **kw)

        try:
            mm.create("team")
            harness_mod.build_command = spy
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "w1", "harness": "py", "cwd": str(tmp_path),
                    "mesh": "team", "handle": "worker_1",
                    "workflow": "review", "task": "take the API",
                },
                headers=BEARER,
            )
            assert resp.status == 201
        finally:
            harness_mod.build_command = real_build
            await mgr.shutdown_all()
            await client.close()

        assert seen["in_mesh"] is True, "joined after the harness was spawned"
        assert seen["has_run"] is True, "run started after the harness was spawned"
        # ...and the whole assignment was composed and available as one message
        assert "worker_1" in seen["opening"]
        assert "review" in seen["opening"]
        assert seen["opening"].endswith("take the API")

    asyncio.run(run())


def test_a_session_that_fails_to_start_does_not_leave_its_mesh_seat_behind(
    home, tmp_path
):
    """Arranging before starting means there is something to undo.

    The name goes straight back into circulation when the create fails, so a
    membership left behind would not merely leak -- the next session to take
    the name would inherit it.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        real_build = harness_mod.build_command

        def boom(sdef, **kw):
            raise harness_mod.HarnessError("no such program")

        try:
            mm.create("team")
            harness_mod.build_command = boom
            resp = await client.post(
                "/api/sessions",
                json={
                    "name": "w1", "harness": "py", "cwd": str(tmp_path),
                    "mesh": "team", "handle": "worker_1",
                },
                headers=BEARER,
            )
            assert resp.status == 400
        finally:
            harness_mod.build_command = real_build

        try:
            assert "worker_1" not in mm.get("team").members
            # and the name is genuinely free again
            resp = await client.get("/api/sessions", headers=BEARER)
            assert [s["name"] for s in (await resp.json())["sessions"]] == []
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
