"""A spawned child can report across project and team mesh boundaries."""

import asyncio

import pytest

from claude_launcher import projects
from claude_launcher.daemon import onboard, rebrief
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshManager
from test_spawn_api import _manager, _register_py_harness, _serve, BEARER


@pytest.mark.parametrize("different_project", [False, True])
@pytest.mark.parametrize("team", ["parent-team", "child-team", "-"])
def test_spawn_parent_channel(home, tmp_path, monkeypatch, different_project, team):
    _register_py_harness()
    projects.add("other")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        client = await _serve(mgr, mm)
        openings = []

        async def launch(session, **kw):
            # At the launch boundary, the channel and opening already exist.
            openings.append(kw["opening"])
            return session

        monkeypatch.setattr(mgr, "launch_async", launch)
        try:
            mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
            mm.create("parent-team")
            mm.create("child-team")
            await mm.join("parent-team", "parent", handle="boss")
            response = await client.post(
                "/api/sessions/parent/children", headers=BEARER,
                json={"name": "child", "mesh": team, "workflow": "-",
                      "project": "other" if different_project else "default",
                      "beads": False, "task": "report the result"},
            )
            result = await response.json()
            assert response.status == 201, result
            channel = result["parent_channel"]
            mesh = mm.get(channel["mesh"])
            assert mesh.connected(channel["parent_handle"], channel["child_handle"])
            assert channel["created"] == (team != "parent-team")
            assert len(mm.list()) == (2 if team == "parent-team" else 3)
            assert 'claunch mesh send ' + channel["mesh"] in openings[0]
            assert openings[0].startswith("---\n# claunch: the session that created you")
            if team != "parent-team":
                assert set(mesh.members) == {"parent", "child"}
                assert set(mm.get("parent-team").members) == {"boss"}
                assert set(mm.get("child-team").members) == ({"child"} if team != "-" else set())
                assert mesh.visibility == "private"
            # Exercise the actual send checks in both directions.
            for sender, target in (("parent", channel["child_handle"]),
                                   ("child", channel["parent_handle"])):
                sent = await mm.send(mesh.name, sender, target, "hello", type="fyi")
                assert sent["recipients"] == [target]
            # Auxiliary channels survive a reload and do not make the parent's
            # next default spawn ambiguous or change the child's chosen team.
            loaded = MeshManager(mgr, root=tmp_path / "meshes")
            loaded.load_all()
            body = {}
            await onboard.inherit_mesh(body, parent="parent", mesh_mgr=loaded)
            assert body["mesh"] == "parent-team"
            if team == "child-team":
                body = {}
                await onboard.inherit_mesh(body, parent="child", mesh_mgr=loaded)
                assert body["mesh"] == "child-team"
            recovered = rebrief._parent_section(mgr.get("child").sdef, mgr, loaded)
            assert channel["mesh"] in recovered
        finally:
            await client.close()

    asyncio.run(run())


def test_failed_launch_removes_parent_channel(home, tmp_path, monkeypatch):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        client = await _serve(mgr, mm)
        mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        mm.create("child-team")

        async def fail(session, **kw):
            assert len(mm.list()) == 2
            raise ValueError("launch failed")

        monkeypatch.setattr(mgr, "launch_async", fail)
        try:
            response = await client.post(
                "/api/sessions/parent/children", headers=BEARER,
                json={"name": "child", "mesh": "child-team", "workflow": "-", "beads": False},
            )
            assert response.status == 400
            assert [m.name for m in mm.list()] == ["child-team"]
            assert not mm.get("child-team").members
            assert not mm.meshes_for_session("child")
            assert not mm.meshes_for_session("parent")
        finally:
            await client.close()

    asyncio.run(run())


def test_parent_channel_does_not_use_another_sessions_handle(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        for name in ("parent", "child", "stranger"):
            mgr.stage(SessionDef(name=name, harness="py", cwd=str(tmp_path),
                                 parent="parent" if name == "child" else None))
        mm.create("team")
        await mm.join("team", "stranger", handle="parent")
        await mm.join("team", "child")
        plan = onboard.preflight({"mesh": "team"}, mesh_mgr=mm,
                                 session_name="child", cwd=str(tmp_path),
                                 harness="py", parent="parent")
        assert not plan.parent_handle
        channel = await mm.ensure_parent_channel("child", "parent")
        assert channel["created"]
        assert set(mm.get(channel["mesh"]).members) == {"parent", "child"}
        again = await mm.ensure_parent_channel("child", "parent")
        assert again["mesh"] == channel["mesh"] and not again["created"]
        assert channel["mesh"] in rebrief._parent_section(mgr.get("child").sdef, mgr, mm)

    asyncio.run(run())


def test_failed_channel_join_cleans_up_pair(home, tmp_path, monkeypatch):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshes")
        for name in ("parent", "child"):
            mgr.stage(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
        join = mm.join

        async def fail_child(mesh, session, **kw):
            if session == "child":
                raise ValueError("join failed")
            return await join(mesh, session, **kw)

        monkeypatch.setattr(mm, "join", fail_child)
        with pytest.raises(ValueError, match="join failed"):
            await mm.ensure_parent_channel("child", "parent")
        assert not mm.list()
        assert not mm.meshes_for_session("parent")

    asyncio.run(run())
