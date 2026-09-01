"""What the dashboard's recurring polls put on the wire.

The rail and the sidebar are fed by ``/api/sessions`` and ``/api/mesh``
every few seconds, from every open tab. Measured on a working daemon (193
sessions, nine meshes) the pair was 1.3MB per tick: a fifth of the session
list was ``\\uXXXX`` escapes of Korean task text, and 480KB of the mesh
list was the pairwise member graph that only the per-mesh views draw. These
pin the shape that replaced it — UTF-8 bodies, gzip past a threshold, and a
mesh list without the graph — so a later "simplification" cannot put the
megabyte back without a test saying so.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


async def _serve(mgr, mm):
    from aiohttp.test_utils import TestClient, TestServer

    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_json_goes_out_as_utf8_and_a_lone_surrogate_falls_back():
    """Korean text is sent as itself, three bytes a character rather than
    six of escape; a string that cannot be encoded still gets an answer."""
    text = api_mod._dumps({"task": "세션 목록"})
    assert text == '{"task": "세션 목록"}'
    # A lone surrogate (what a filename decoded with surrogateescape looks
    # like) cannot be UTF-8; the payload is escaped instead of raising.
    text = api_mod._dumps({"path": "bad\udcff"})
    assert text == json.dumps({"path": "bad\udcff"})
    assert text.encode("utf-8")  # the escaped form is plain ASCII


def test_large_bodies_are_gzipped_when_asked_and_small_ones_are_not(home, tmp_path):
    """The threshold, seen from the client: a body past ``JSON_GZIP_MIN`` comes
    back compressed for a client that accepts it, a small one as it is, and a
    client that does not accept gzip gets plain text either way."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            # Four FAQ answers near the field's own cap make the catalogue
            # cross the threshold; a real route, since the router is frozen
            # once the server is up.
            for i in range(4):
                resp = await client.post(
                    "/api/briefing/faq", headers=BEARER,
                    json={"question": f"q{i}", "answer": "x" * 4900},
                )
                assert resp.status in (200, 201), await resp.text()
            resp = await client.get(
                "/api/briefing/faq", headers={**BEARER, "Accept-Encoding": "gzip"}
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Encoding") == "gzip"
            big = await resp.json()  # the client inflates it
            assert len(json.dumps(big)) >= api_mod.JSON_GZIP_MIN
            resp = await client.get(
                "/api/briefing/faq", headers={**BEARER, "Accept-Encoding": "identity"}
            )
            assert resp.headers.get("Content-Encoding") is None
            assert (await resp.json()) == big
            resp = await client.get("/api/health", headers={"Accept-Encoding": "gzip"})
            assert resp.headers.get("Content-Encoding") is None
            assert resp.headers["Content-Type"].startswith("application/json")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_mesh_list_leaves_the_member_graph_to_the_mesh_view(home, tmp_path):
    """``/api/mesh?view=rail`` (the sidebar's poll) carries no
    ``member_links``; ``/api/mesh/<name>`` (what the mesh page and the flow
    view fetch) still does, so the diagram that draws the graph has it and
    the rail that never did stops paying for it."""

    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            for name in ("lead", "dev"):
                mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await mm.join("team", name)
            listed = await (
                await client.get("/api/mesh?view=rail", headers=BEARER)
            ).json()
            (entry,) = [m for m in listed["meshes"] if m["name"] == "team"]
            assert "member_links" not in entry
            assert {m["handle"] for m in entry["members"]} == {"lead", "dev"}
            one = await (await client.get("/api/mesh/team", headers=BEARER)).json()
            pairs = {frozenset((e["a"], e["b"])) for e in one["member_links"]}
            assert frozenset(("lead", "dev")) in pairs
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
