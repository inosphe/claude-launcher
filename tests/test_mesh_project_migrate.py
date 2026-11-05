"""A re-filed session takes the mesh mirrors it built with it.

``MeshManager.join`` files the mirror it builds where the joining session
stands (``_session_project``), and nothing recomputed that afterwards — so
``SessionManager.migrate(project=...)`` re-filed the session and left the
mirror under the project it came from. The mesh then drew in a project tab
its one local member had left, and ``/api/mesh?project=`` hid it from the
one it moved to. The migrate route now asks
``MeshManager.refile_mirrors_for`` after the move.

Two boundaries are pinned here with the regression: a mirror with a second
local member keeps its filing (one value cannot serve two projects, and it
is not this session's to change), and a mesh this daemon owns is filed by
``create``/``set_project`` — a session's migrate never moves it.
"""

from __future__ import annotations

import asyncio
import json
import time

from claude_launcher import projects
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshManager

from test_mesh_join import _manager, _register_py_harness, _wire

BEARER = {"Authorization": "Bearer sekrit"}


async def _guest_with_mirror(mgr, tmp_path):
    """pcB's mirror of pcA's ``m``, joined by ``sb`` — filed under 'hq'."""
    mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
    mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
    _wire({"pcA": mm_a, "pcB": mm_b})
    mgr.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path), rows=80))
    mgr.create(SessionDef(
        name="sb", harness="py", cwd=str(tmp_path), rows=80, project="hq",
    ))
    mm_a.create("m")
    await mm_a.join("m", "sa", handle="alice")
    await mm_b.join("m@pcA", "sb", handle="bob", code=mm_a.invite("m")["code"])
    return mm_a, mm_b


async def _serve(mgr, mm, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_a_migrated_session_takes_its_mirror_with_it(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        projects.add("hq")
        projects.add("ops")
        _mm_a, mm_b = await _guest_with_mirror(mgr, tmp_path)
        # the join seeded the mirror from where the session stood
        assert mm_b.get("m@pcA").project == "hq"
        assert mgr.get("sb").sdef.project == "hq"

        target = tmp_path / "elsewhere"
        target.mkdir()
        client = await _serve(mgr, mm_b, tmp_path)
        try:
            resp = await client.post(
                "/api/sessions/sb/migrate",
                json={"cwd": str(target), "project": "ops"},
                headers=BEARER,
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        finally:
            await client.close()

        assert body["project"] == "ops"
        assert body["mirrors_refiled"] == [
            {"mesh": "m@pcA", "was": "hq", "now": "ops"}
        ]
        # the row and the session now say the same thing...
        assert mm_b.get("m@pcA").project == "ops"
        assert mm_b._session_project("sb") == "ops"
        # ...the listing filter that reads the row follows it...
        assert [
            mesh.name for mesh in mm_b.list()
            if projects.matches(mesh.project, "ops")
        ] == ["m@pcA"]
        # ...and the move was written to disk, not only to memory
        doc = json.loads(
            (tmp_path / "meshB" / "m@pcA" / "mesh.json").read_text()
        )
        assert doc["project"] == "ops"

    asyncio.run(run())


def test_only_the_mirror_of_a_solitary_local_member_moves(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        projects.add("hq")
        projects.add("ops")
        _mm_a, mm_b = await _guest_with_mirror(mgr, tmp_path)

        # a second local session on the same mirror: the one-valued filing is
        # as much theirs as sb's, so a migrate of sb leaves it alone
        mgr.create(SessionDef(
            name="sc", harness="py", cwd=str(tmp_path), rows=80, project="ops",
        ))
        await mm_b.join("m@pcA", "sc", handle="carol")  # trusted machine: no code
        assert mm_b.refile_mirrors_for("sb", "ops") == []
        assert mm_b.get("m@pcA").project == "hq"

        # a mesh this daemon owns is filed by create/set_project, never by a
        # member's re-filing
        mm_b.create("own", project="hq")
        await mm_b.join("own", "sb", handle="bob-own")
        assert mm_b.refile_mirrors_for("sb", "ops") == []
        assert mm_b.get("own").project == "hq"

    asyncio.run(run())
