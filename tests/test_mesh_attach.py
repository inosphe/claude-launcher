"""Daemon attach: a daemon joins a mesh with no member of its own.

Scenario matrix, derived from docs/mesh-design.md "Daemon attach":

V. Visibility & discovery
   V1 only public meshes answer /peer/meshes, and never a mirror
   V2 discover() is the union over every relay peer plus received offers;
      each row is keyed by the daemon that answered (one hop)
   V3 a private visibility withdraws every outstanding offer

A. Attach
   A1 an offered mesh attaches in one call: mirror + link, no member; the
      offer is spent on both sides
   A2 a public mesh attach without an offer pends; approve delivers the
      grant, deny clears it
   A3 once attached, a local session joins by the bare name with no approval
   A4 attaching an already-mirrored mesh is a no-op; a name owned locally
      conflicts
   A5 detach removes the link and the daemon's members on the owner and
      drops the mirror here
   A6 received offers and a pending attach survive a reload
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshConflict, MeshError, MeshManager

from test_mesh_join import (
    _dispatch_peer as _dispatch_join_peer,
    _manager,
    _primary_with_alice,
    _register_py_harness,
)


def _dispatch_peer(mm: MeshManager, path: str, body: dict):
    if path == "/peer/mesh/join_request":
        return mm.peer_join_request_accept(
            body["mesh"], body["machine"],
            body.get("session") or "", body.get("handle") or "",
            body.get("role") or "", body.get("reply_token") or "",
            body.get("code") or "", offer=body.get("offer") or "",
        )
    if path == "/peer/meshes":
        return {"meshes": mm.peer_meshes_list()}
    if path == "/peer/mesh/offer":
        return mm.peer_offer_accept(
            body["mesh"], body["machine"], body.get("token") or "",
            cancel=bool(body.get("cancel")),
            project=body.get("project") or "",
            members=body.get("members") or 0,
        )
    if path == "/peer/mesh/detach":
        return mm.peer_detach_accept(
            body["mesh"], body["machine"], body.get("token") or ""
        )
    return _dispatch_join_peer(mm, path, body)


def _wire(machines: dict) -> None:
    async def call(machine, path, body):
        result = _dispatch_peer(machines[machine], path, body)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def lister_for(me):
        return [m for m in machines if m != me]

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True
        mm.peer_lister = (lambda me=name: lister_for(me))


# --------------------------------------------------------------------------- #
# V1/V2/V3
# --------------------------------------------------------------------------- #
def test_discovery_is_public_owned_meshes_plus_offers(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        mm_a.create("hidden")
        mm_a.create("pub")
        mm_c.create("cmesh")

        # V1: private by default — nothing is listed
        assert mm_a.peer_meshes_list() == []
        await mm_a.set_visibility("pub", "public")
        await mm_c.set_visibility("cmesh", "invited")
        assert [m["mesh"] for m in mm_a.peer_meshes_list()] == ["pub"]
        with pytest.raises(MeshError):
            await mm_a.set_visibility("pub", "everyone")

        # an offer is pushed to one daemon only
        await mm_c.offer_mesh("cmesh", "pcB")
        assert mm_c.get("cmesh").offers.keys() == {"pcB"}

        found = await mm_b.discover()
        rows = {(r["mesh"], r["machine"]): r for r in found["meshes"]}
        assert set(rows) == {("pub", "pcA"), ("cmesh", "pcC")}
        assert rows[("pub", "pcA")]["access"] == "approval"
        assert rows[("cmesh", "pcC")]["access"] == "offer"
        assert all(r["state"] == "available" for r in rows.values())
        assert found["errors"] == {}
        # pcA was never offered cmesh, and pcC's invited mesh is not public
        assert {(r["mesh"], r["machine"]) for r in (await mm_a.discover())["meshes"]} == set()

        # V2: a mirror is not re-listed by the daemon holding it (one hop)
        await mm_b.attach("cmesh@pcC")
        assert mm_b.peer_meshes_list() == []
        found_a = await mm_a.discover()
        assert all(r["machine"] != "pcB" for r in found_a["meshes"])

        # V3: going private withdraws outstanding offers on both sides
        mm_c.create("c2")
        await mm_c.offer_mesh("c2", "pcA")
        assert [o["mesh"] for o in mm_a.offers_received()] == ["c2"]
        res = await mm_c.set_visibility("c2", "private")
        assert res["withdrawn"] == ["pcA"]
        assert mm_c.get("c2").offers == {}
        assert mm_a.offers_received() == []

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A1/A3/A4
# --------------------------------------------------------------------------- #
def test_offered_attach_is_one_call_and_sessions_join_freely(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        mgr.create(SessionDef(name="sa2", harness="py", cwd=str(tmp_path), rows=80))
        await mm_a.join("m", "sa2", handle="amy")
        await mm_a.send("m", "alice", "amy", "before attach")
        await mm_a.offer_mesh("m", "pcB")
        assert mm_a.get("m").visibility == "invited"

        res = await mm_b.attach("m@pcA")
        assert res["attached"] is True and res["already"] is False
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        # A1: mirror + link, no member from pcB
        assert mesh_b.primary == "pcA"
        assert set(mesh_b.members) == {"alice", "amy"}
        assert [m["body"] for m in mesh_b.messages] == ["before attach"]
        assert "pcB" in mesh_a.links and "pcB" in mesh_a.peers
        assert mesh_a.links["pcB"]["token_in"] == mesh_b.links["pcA"]["token_out"]
        assert mm_a.request_list("m") == []
        # the offer is spent on both sides
        assert mesh_a.offers == {}
        assert mm_b.offers_received() == []

        # A3: a session joins by the bare name, no code, no approval
        member = await mm_b.join("m", "sb", handle="bob")
        assert member.handle == "bob"
        assert mesh_a.members["bob"].machine == "pcB"

        # A4: attaching again is a no-op; a locally owned name conflicts
        again = await mm_b.attach("m@pcA")
        assert again["already"] is True
        mm_b.create("own")
        with pytest.raises(MeshConflict):
            await mm_b.attach("own@pcA")
        with pytest.raises(MeshError):
            await mm_b.attach("m")  # no machine: not an address

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A2
# --------------------------------------------------------------------------- #
def test_public_attach_pends_then_approve_and_deny(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        await mm_a.set_visibility("m", "public")

        res = await mm_b.attach("m@pcA")
        assert res["pending"] is True
        rid = res["request_id"]
        assert [x.name for x in mm_b.list()] == []
        # asking twice keeps one request on each side
        again = await mm_b.attach("m@pcA")
        assert again["request_id"] == rid
        reqs = mm_a.request_list("m")
        assert len(reqs) == 1 and reqs[0]["attach"] is True
        found = await mm_b.discover()
        assert [r["state"] for r in found["meshes"]] == ["pending"]

        out = await mm_a.approve_request("m", rid)
        assert out["attach"] is True and out["delivered"] is True
        mesh_b = mm_b.get("m")
        assert mesh_b.primary == "pcA" and set(mesh_b.members) == {"alice"}
        assert mm_b.outgoing_list() == []
        assert "pcB" in mm_a.get("m").links

        res_c = await mm_c.attach("m@pcA")
        await mm_a.deny_request("m", res_c["request_id"])
        assert mm_c.outgoing_list() == []
        assert [x.name for x in mm_c.list()] == []
        assert "pcC" not in mm_a.get("m").links

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A5
# --------------------------------------------------------------------------- #
def test_detach_removes_link_and_members(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        await mm_a.offer_mesh("m", "pcB")
        await mm_b.attach("m@pcA")
        await mm_b.join("m", "sb", handle="bob")

        with pytest.raises(MeshError):
            await mm_a.detach("m")  # the owner deletes, it does not detach
        res = await mm_b.detach("m")
        assert res["notified"] is True
        mesh_a = mm_a.get("m")
        assert "pcB" not in mesh_a.links and "pcB" not in mesh_a.peers
        assert "bob" not in mesh_a.members
        assert [x.name for x in mm_b.list()] == []

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A6
# --------------------------------------------------------------------------- #
def test_offers_and_pending_attach_survive_reload(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        mm_c.create("cm")
        await mm_c.offer_mesh("cm", "pcB")
        await mm_a.set_visibility("m", "public")
        res = await mm_b.attach("m@pcA")

        mm_b2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        mm_b2.load_all()
        mm_c2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        mm_c2.load_all()
        _wire({"pcA": mm_a, "pcB": mm_b2, "pcC": mm_c2})
        assert [o["mesh"] for o in mm_b2.offers_received()] == ["cm"]
        assert mm_c2.get("cm").visibility == "invited"
        assert set(mm_c2.get("cm").offers) == {"pcB"}
        # the offer token still admits after both sides reloaded
        assert (await mm_b2.attach("cm@pcC"))["attached"] is True

        # the pending attach still lands
        await mm_a.approve_request("m", res["request_id"])
        assert mm_b2.get("m").primary == "pcA"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_session_join_still_pends_without_an_offer(home, tmp_path):
    """The session-level join (A in the design) is unchanged by attach."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        await mm_a.set_visibility("m", "public")
        res = await mm_b.join("m@pcA", "sb", handle="bob")
        assert isinstance(res, dict) and res["pending"] is True
        assert mm_a.request_list("m")[0]["attach"] is False
        mgr.create(SessionDef(name="unused", harness="py", cwd=str(tmp_path)))
        await mgr.shutdown_all()

    asyncio.run(run())


def test_attach_http_surface(home, tmp_path):
    """The routes: visibility, offers, discovery, attach and the /peer side."""
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "meshA")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            mm.create("web")
            resp = await client.put(
                "/api/mesh/web/visibility", json={"visibility": "public"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["visibility"] == "public"
            resp = await client.put(
                "/api/mesh/web/visibility", json={"visibility": "nope"},
                headers=bearer,
            )
            assert resp.status == 400
            resp = await client.get("/api/mesh/web", headers=bearer)
            assert (await resp.json())["visibility"] == "public"

            # /peer/meshes needs no daemon token: it lists public names only
            resp = await client.post("/peer/meshes", json={})
            assert resp.status == 200
            assert [m["mesh"] for m in (await resp.json())["meshes"]] == ["web"]

            # no relay here: discovery answers with the reason, not a 500
            resp = await client.get("/api/relay/meshes", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["meshes"] == [] and "relay" in doc["errors"]

            # an offer arrives over /peer and is listed without its token
            resp = await client.post("/peer/mesh/offer", json={
                "mesh": "far", "machine": "pcZ", "token": "t0k", "members": 2,
            })
            assert resp.status == 200
            resp = await client.get("/api/relay/meshes", headers=bearer)
            doc = await resp.json()
            assert [(r["mesh"], r["machine"], r["access"]) for r in doc["meshes"]] \
                == [("far", "pcZ", "offer")]
            assert "token" not in doc["offers"][0]

            # attach needs an address and a relay
            resp = await client.post("/api/mesh/web/attach", json={}, headers=bearer)
            assert resp.status == 400
            resp = await client.delete("/api/mesh/web/attach", headers=bearer)
            assert resp.status == 400  # owned here: delete, not detach
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
