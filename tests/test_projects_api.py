"""Projects end to end: the registry routes, the project a session and a mesh
are filed under, the default workspace a project hands a new session, and the
per-project listings.

Reuses the tiny Python echo harness of the spawn API tests so a session really
starts (in the directory the project chose) rather than being asserted on a
dict.
"""

from __future__ import annotations

import asyncio
import json

from claude_launcher import projects, store, workspaces
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshError, MeshManager

from test_spawn_api import BEARER, _manager, _register_py_harness, _serve


def _workspace(tmp_path, name="hq"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return workspaces.add(str(d), name=name)


# --------------------------------------------------------------------------- #
# the registry routes
# --------------------------------------------------------------------------- #
def test_projects_routes_add_update_list_and_remove(home, tmp_path):
    _register_py_harness()
    ws = _workspace(tmp_path)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            resp = await client.get("/api/projects", headers=BEARER)
            assert [p["name"] for p in (await resp.json())["projects"]] == ["default"]

            resp = await client.post(
                "/api/projects", headers=BEARER,
                json={"name": "launcher", "default_workspace": "hq"},
            )
            assert resp.status == 201, await resp.text()
            doc = (await resp.json())["project"]
            assert doc == {
                "name": "launcher", "default_workspace": "hq",
                "default_cwd": ws.path, "is_default": False,
            }

            # an unregistered workspace is refused, naming the registered ones
            resp = await client.post(
                "/api/projects", headers=BEARER,
                json={"name": "other", "default_workspace": "nope"},
            )
            assert resp.status == 400
            assert "no workspace named 'nope'" in (await resp.json())["error"]

            resp = await client.patch(
                "/api/projects/launcher", headers=BEARER,
                json={"default_workspace": None},
            )
            assert resp.status == 200
            assert (await resp.json())["project"]["default_workspace"] is None

            resp = await client.patch(
                "/api/projects/zzz", headers=BEARER, json={"default_workspace": "hq"},
            )
            assert resp.status == 404

            resp = await client.delete("/api/projects/default", headers=BEARER)
            assert resp.status == 400
            resp = await client.delete("/api/projects/launcher", headers=BEARER)
            assert resp.status == 200
            resp = await client.get("/api/projects", headers=BEARER)
            assert [p["name"] for p in (await resp.json())["projects"]] == ["default"]
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def test_a_session_is_filed_under_its_project_and_starts_in_its_default(home, tmp_path):
    """The whole feature from the create form's side: name a project and
    no directory, and the session starts in that project's default
    workspace, filed under the project."""
    _register_py_harness()
    ws = _workspace(tmp_path)
    projects.add("launcher", default_workspace="hq")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "filed", "profile": "py", "project": "launcher", "beads": False,
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["project"] == "launcher"
            assert doc["cwd"] == ws.path

            # an explicit directory wins: the default is a default
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "placed", "profile": "py", "project": "launcher",
                "cwd": str(tmp_path), "beads": False,
            })
            doc = await resp.json()
            assert resp.status == 201, doc
            assert doc["cwd"] == str(tmp_path)

            # an unknown project is a 400 with no session left behind
            resp = await client.post("/api/sessions", headers=BEARER, json={
                "name": "lost", "profile": "py", "project": "nowhere", "beads": False,
            })
            assert resp.status == 400
            assert "no project named 'nowhere'" in (await resp.json())["error"]
            assert not any(s.sdef.name == "lost" for s in mgr.list())

            # the record carries the project across a persist/restore cycle
            row = next(e for e in mgr._store.load_all() if e["def"]["name"] == "filed")
            assert row["def"]["project"] == "launcher"
            # ...and a session that named none writes no key at all
            row = next(e for e in mgr._store.load_all() if e["def"]["name"] == "placed")
            assert row["def"]["project"] == "launcher"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_session_list_filters_by_project(home, tmp_path):
    _register_py_harness()
    projects.add("a")
    projects.add("b")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="s-a", harness="py", cwd=str(tmp_path), project="a"))
            mgr.create(SessionDef(name="s-b", harness="py", cwd=str(tmp_path), project="b"))
            mgr.create(SessionDef(name="s-none", harness="py", cwd=str(tmp_path)))

            async def names(query=""):
                resp = await client.get(f"/api/sessions{query}", headers=BEARER)
                assert resp.status == 200
                return sorted(s["name"] for s in (await resp.json())["sessions"])

            assert await names() == ["s-a", "s-b", "s-none"]
            assert await names("?project=a") == ["s-a"]
            assert await names("?project=b") == ["s-b"]
            # the unfiled record is the default project's
            assert await names("?project=default") == ["s-none"]
            assert await names("?project=zzz") == []
            resp = await client.get("/api/sessions", headers=BEARER)
            rows = {s["name"]: s for s in (await resp.json())["sessions"]}
            assert rows["s-a"]["project"] == "a"
            assert "project" not in rows["s-none"]

            # The page polls the rail view unfiltered and narrows by this
            # key itself, and the rail view answers from an allow-list: a
            # row that lost the key would be drawn under the default project
            # and vanish from its own (claunch-20c0v).
            resp = await client.get("/api/sessions?view=rail", headers=BEARER)
            rail = {s["name"]: s for s in (await resp.json())["sessions"]}
            assert rail["s-a"]["project"] == "a"
            assert rail["s-b"]["project"] == "b"
            assert "project" not in rail["s-none"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_child_is_filed_under_its_parent_and_may_move_project(home, tmp_path):
    """A spawn inherits the parent's project; naming another one files the
    child there and, with no directory of its own, starts it in that
    project's default workspace."""
    _register_py_harness()
    ws = _workspace(tmp_path)
    projects.add("lead-proj")
    projects.add("side", default_workspace="hq")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(
                name="lead", harness="py", cwd=str(tmp_path), project="lead-proj",
            ))
            resp = await client.post(
                "/api/sessions/lead/children", json={"name": "w1"}, headers=BEARER,
            )
            assert resp.status == 201, await resp.text()
            child = (await resp.json())["session"]
            assert child["project"] == "lead-proj"
            assert child["cwd"] == str(tmp_path)

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w2", "project": "side"}, headers=BEARER,
            )
            assert resp.status == 201, await resp.text()
            child = (await resp.json())["session"]
            assert child["project"] == "side"
            assert child["cwd"] == ws.path

            # a named directory beats the project's default
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w3", "project": "side", "workspace": "hq"},
                headers=BEARER,
            )
            assert resp.status == 201, await resp.text()

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w4", "project": "nowhere"}, headers=BEARER,
            )
            assert resp.status == 403

            # the budget report lists the projects, gate or no gate
            resp = await client.get("/api/sessions/lead/children", headers=BEARER)
            body = await resp.json()
            assert [p["name"] for p in body["projects"]] == ["default", "lead-proj", "side"]

            # the mesh opened for the pair is filed with the parent
            meshes = mm.meshes_for_session("lead")
            assert len(meshes) == 1
            assert mm.get(meshes[0]["mesh"]).project == "lead-proj"

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_default_workspace_is_gated_like_a_workspace_pick(home, tmp_path):
    """With spawn.allow_workspace off, a child asked into a project with a
    default workspace stays in its parent's directory — warned, not refused."""
    _register_py_harness()
    _workspace(tmp_path)
    projects.add("side", default_workspace="hq")
    store.update(lambda doc: doc.update({"spawn": {"allow_workspace": False}}))

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "w1", "project": "side"}, headers=BEARER,
            )
            body = await resp.json()
            assert resp.status == 201, body
            assert body["session"]["project"] == "side"
            assert body["session"]["cwd"] == str(tmp_path)
            assert any("allow_workspace" in w for w in body.get("warnings", []))
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# meshes
# --------------------------------------------------------------------------- #
def test_a_mesh_is_filed_under_a_project_and_the_list_filters(home, tmp_path):
    _register_py_harness()
    projects.add("a")

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            resp = await client.post(
                "/api/mesh", headers=BEARER, json={"name": "room", "project": "a"},
            )
            assert resp.status == 201, await resp.text()
            assert (await resp.json())["project"] == "a"
            resp = await client.post("/api/mesh", headers=BEARER, json={"name": "plain"})
            assert resp.status == 201
            assert (await resp.json())["project"] == "default"

            async def names(query=""):
                resp = await client.get(f"/api/mesh{query}", headers=BEARER)
                assert resp.status == 200
                return sorted(m["name"] for m in (await resp.json())["meshes"])

            assert await names() == ["plain", "room"]
            assert await names("?project=a") == ["room"]
            assert await names("?view=rail&project=default") == ["plain"]

            # persisted: the field is on disk only for the filed mesh, and a
            # reload reads both back the same way
            filed = json.loads((tmp_path / "mesh" / "room" / "mesh.json").read_text("utf-8"))
            plain = json.loads((tmp_path / "mesh" / "plain" / "mesh.json").read_text("utf-8"))
            assert filed["project"] == "a"
            assert "project" not in plain
            again = MeshManager(mgr, root=tmp_path / "mesh")
            again.load_all()
            assert again.get("room").project == "a"
            assert again.get("plain").project == ""
        finally:
            await client.close()

    asyncio.run(run())


def test_a_mesh_cannot_be_filed_under_an_unknown_project(home, tmp_path):
    mgr = _manager()
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    try:
        mm.create("room", project="nowhere")
    except MeshError as exc:
        assert "no project named 'nowhere'" in str(exc)
    else:
        raise AssertionError("an unknown project was accepted")
    assert not (tmp_path / "mesh" / "room").exists()
