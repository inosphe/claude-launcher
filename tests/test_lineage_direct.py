"""Direct parent/child pairs talk across mesh wiring, meshes and projects,
and ``detach`` ends the pair (claunch-mz09h).

What this file pins: the tree question the mesh asks (``direct_kin``), the
detach guards, the member graph letting a kin pair past a recorded cut, a
send to a parent or child that is not in the sender's mesh being carried on
the pair's own channel, and the detach route cutting that channel again.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshError, MeshManager

from test_daemon_wedge import _stub_connect
from test_reparent import BEARER, _register_py_harness, _serve, _tree


# --------------------------------------------------------------------------- #
# the manager: kin and detach
# --------------------------------------------------------------------------- #
def test_direct_kin_is_one_generation_either_way(home):
    mgr = _tree()  # lead -> (w1 -> w1a, w2), solo
    assert mgr.direct_kin("lead", "w1") and mgr.direct_kin("w1", "lead")
    assert mgr.direct_kin("w1", "w1a")
    assert not mgr.direct_kin("lead", "w1a")  # a grandchild is not kin here
    assert not mgr.direct_kin("w1", "w2")  # siblings are not
    assert not mgr.direct_kin("solo", "lead")
    assert not mgr.direct_kin("w1", "w1")
    assert not mgr.direct_kin("w1", "nope")


def test_detach_makes_a_root_and_keeps_the_subtree(home):
    mgr = _tree()
    out = mgr.detach("w1")
    assert out == {"session": "w1", "previous": "lead"}
    assert mgr.get("w1").sdef.parent is None
    assert mgr.children("w1") == ["w1a"]
    assert not mgr.direct_kin("lead", "w1")
    with pytest.raises(ManagerError) as exc:
        mgr.detach("w1")
    assert "no parent" in str(exc.value)


def test_detach_authority_runs_down_the_tree(home):
    mgr = _tree()
    with pytest.raises(ManagerError) as exc:
        mgr.detach("w1", actor="w1")
    assert "cannot detach itself" in str(exc.value)
    with pytest.raises(ManagerError) as exc:
        mgr.detach("w2", actor="w1")
    assert "does not command" in str(exc.value)
    # a grandparent commands the grandchild
    assert mgr.detach("w1a", actor="lead")["previous"] == "w1"
    assert mgr.detach("w2", actor="lead")["previous"] == "lead"


# --------------------------------------------------------------------------- #
# the mesh: the member graph and the send
# --------------------------------------------------------------------------- #
def _live(mgr, tmp_path, *pairs):
    for name, parent in pairs:
        mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path), parent=parent))


def test_a_kin_pair_is_connected_over_a_cut_and_a_detach_ends_it(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            _live(mgr, tmp_path, ("lead", None), ("w1", "lead"), ("w2", None))
            mm.create("team")
            for name in ("lead", "w1", "w2"):
                await mm.join("team", name, handle=name)
            await mm.set_member_link("team", "lead", "w1", enabled=False)
            await mm.set_member_link("team", "lead", "w2", enabled=False)
            mesh = mm.get("team")
            assert mesh.connected("lead", "w1")  # kin wins over the cut
            assert not mesh.connected("lead", "w2")  # a stranger stays cut
            result = await mm.send("team", "lead", "w1", "hello")
            assert result.get("id")
            with pytest.raises(MeshError):
                await mm.send("team", "lead", "w2", "hello")

            mgr.detach("w1")
            assert not mesh.connected("lead", "w1")
            with pytest.raises(MeshError):
                await mm.send("team", "lead", "w1", "again")
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_send_to_kin_outside_the_mesh_takes_the_pair_channel(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        try:
            _live(mgr, tmp_path, ("lead", None), ("kid", "lead"), ("other", None))
            mm.create("alpha")
            mm.create("beta")
            await mm.join("alpha", "lead", handle="lead")
            await mm.join("alpha", "other", handle="other")
            await mm.join("beta", "kid", handle="kid")

            result = await mm.send("alpha", "lead", "kid", "from the parent")
            routed = result["rerouted"]
            assert [r["session"] for r in routed] == ["kid"]
            channel = routed[0]["mesh"]
            assert channel not in ("alpha", "beta")
            ch = mm.get(channel)
            assert ch.parent_channel == ["lead", "kid"]
            assert ch.messages[-1]["body"] == "from the parent"
            assert ch.messages[-1]["to"] in ("kid", ["kid"])

            # the child answers by session name from its own mesh, on the
            # same channel (reused, not a second one)
            back = await mm.send("beta", "kid", "lead", "from the child")
            assert back["rerouted"][0]["mesh"] == channel

            # a mixed address: the member here goes the ordinary way
            mixed = await mm.send("alpha", "lead", ["other", "kid"], "both")
            assert [r["session"] for r in mixed["rerouted"]] == ["kid"]
            assert mm.get("alpha").messages[-1]["to"] in ("other", ["other"])

            # a stranger outside the mesh is still an unknown recipient
            with pytest.raises(MeshError) as exc:
                await mm.send("beta", "kid", "other", "x")
            assert "unknown recipient" in str(exc.value)
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the route and the tool
# --------------------------------------------------------------------------- #
def test_the_detach_route_cuts_the_private_channel(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            _live(mgr, tmp_path, ("lead", None), ("kid", "lead"), ("w2", "lead"))
            route = await mm.ensure_parent_channel("kid", "lead")
            ch = mm.get(route["mesh"])
            assert ch.connected("lead", "kid")

            resp = await client.post(
                "/api/sessions/kid/detach", json={"actor": "w2"}, headers=BEARER
            )
            assert resp.status == 403
            resp = await client.post(
                "/api/sessions/kid/detach", json={"actor": "kid"}, headers=BEARER
            )
            assert resp.status == 403
            resp = await client.post("/api/sessions/nope/detach", json={}, headers=BEARER)
            assert resp.status == 404

            resp = await client.post(
                "/api/sessions/kid/detach", json={"actor": "lead"}, headers=BEARER
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["previous"] == "lead"
            assert body["cut"] == [{"mesh": route["mesh"], "a": "kid", "b": "lead"}]
            assert mgr.get("kid").sdef.parent is None
            assert not ch.connected("lead", "kid")

            resp = await client.post("/api/sessions/kid/detach", json={}, headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
            await mgr.shutdown_all()

    asyncio.run(run())


def test_the_detach_tool_sends_the_caller_as_the_actor(home, monkeypatch):
    from claude_launcher import mesh_mcp

    posted = []

    class Client:
        def post(self, path, body, **kw):
            posted.append((path, body))
            return {"session": "w1", "previous": "lead", "cut": []}

    _stub_connect(monkeypatch, mesh_mcp.daemon_client, Client)
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")
    assert mesh_mcp.call_tool("detach", {"session": "w1"})["previous"] == "lead"
    assert posted == [("/api/sessions/w1/detach", {"actor": "lead"})]
    with pytest.raises(mesh_mcp.MeshMcpError):
        mesh_mcp.call_tool("detach", {})
