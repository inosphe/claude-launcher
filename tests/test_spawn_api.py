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
