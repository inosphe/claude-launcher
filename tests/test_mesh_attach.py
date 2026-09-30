"""Daemon attach: a daemon joins a mesh with no member of its own.

Scenario matrix, derived from docs/mesh-design.md "Daemon attach":

V. Visibility & discovery
   V1 only public meshes answer /peer/meshes, and never a mirror; a row
      carries name, project and counts only (the route is unauthenticated)
   V2 discover() is the union over every relay peer plus received offers;
      each row is keyed by the daemon that answered (one hop)
   V3 a private visibility withdraws every outstanding offer

A. Attach
   A1 an offered mesh attaches in one call: mirror + link, no member; the
      offer is spent on both sides
   A2 a public mesh attach without an offer pends; approve delivers the
      grant, deny clears it
   A3 once attached, a local session joins by the bare name with no approval
   A4 attaching an already-mirrored mesh is a no-op; a local mesh of the
      same name coexists with the mirror (addresses)
   A5 detach removes the link and the daemon's members on the owner and
      drops the mirror here
   A6 received offers and a pending attach survive a reload
   A8 an attach and a session join pending side by side: both approvals
      land in one mirror, and the link both sides hold is the same one;
      a grant the guest rejects puts the guest's earlier link back and
      undoes only the member and its wiring
   A7 a mirror is filed under a project here: the one attach names (also
      through a pending approval), else the joining session's; it can be
      moved later, and an unknown project is refused

O. Offer authenticity (the relay does not say who sent a request)
   O1 an offer is stored only when its claimed owner confirms the token
      was offered to this daemon (/peer/mesh/offer/check)
   O2 a withdrawal drops the row on the stored token, or when the owner no
      longer reports that token live; otherwise it is refused, row kept
   O3 an owner with no check route (plain 404) gets the old behaviour
   O4 no relay, an unreachable owner or another failure: nothing stored,
      nothing dropped without the token
   O5 the owner records an offer before pushing it, and rolls back on a
      failed push
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import projects
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshError, MeshManager

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
    if path == "/peer/mesh/offer/check":
        return mm.peer_offer_check(
            body["mesh"], body["machine"], body.get("token") or ""
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
        # unauthenticated: a row is what public publishes, nothing more
        assert set(mm_a.peer_meshes_list()[0]) == {
            "mesh", "project", "members", "peers", "created_at"}
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
        # a local mesh of the same name is another mesh: both live here
        mm_b.create("own")
        mm_a.create("own")
        await mm_a.set_visibility("own", "public")
        await mm_a.offer_mesh("own", "pcB")
        res = await mm_b.attach("own@pcA")
        assert res["mesh"] == "own@pcA" and res["name"] == "own"
        assert mm_b.get("own").origin == "" and mm_b.get("own@pcA").origin == "pcA"
        rows = {r["address"]: r for r in (await mm_b.discover())["meshes"]}
        assert rows["own@pcA"]["state"] == "attached"
        assert rows["own@pcA"]["key"] == "own@pcA"
        with pytest.raises(MeshError):
            await mm_b.attach("own@local")  # our own: nothing to attach
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

        # the owner no longer knows the link (it was dropped there): a plain
        # detach reports the refusal, --force drops the mirror here anyway
        await mm_a.offer_mesh("m", "pcB")
        await mm_b.attach("m@pcA")
        mm_a._remove_guest(mm_a.get("m"), "pcB")
        with pytest.raises(MeshError, match="refused the detach"):
            await mm_b.detach("m@pcA")
        assert [x.name for x in mm_b.list()] == ["m@pcA"]
        res = await mm_b.detach("m@pcA", force=True)
        assert res["notified"] is False
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


# --------------------------------------------------------------------------- #
# O1-O5
# --------------------------------------------------------------------------- #
def test_an_offer_is_stored_only_when_its_owner_confirms_it(home, tmp_path):
    """O1: the relay does not say who sent an offer, so the receiver asks
    the claimed owner back; a token the owner did not offer *this* daemon
    is refused and nothing is stored."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})

        # a token pcA never issued
        with pytest.raises(MeshError, match="does not confirm"):
            await mm_b.peer_offer_accept("m", "pcA", "forged", members=9)
        assert mm_b.offers_received() == []

        # a real token, offered to pcC, replayed at pcB by pcC
        await mm_a.offer_mesh("m", "pcC")
        stolen = mm_a.get("m").offers["pcC"]["token"]
        with pytest.raises(MeshError, match="does not confirm"):
            await mm_b.peer_offer_accept("m", "pcA", stolen)
        assert mm_b.offers_received() == []

        # a daemon naming itself as the owner is refused without asking
        with pytest.raises(MeshError):
            await mm_b.peer_offer_accept("x", "pcB", "t")

        # the real offer is confirmed while the push is still in flight
        await mm_a.offer_mesh("m", "pcB")
        assert [o["mesh"] for o in mm_b.offers_received()] == ["m"]
        assert (await mm_b.attach("m@pcA"))["attached"] is True

        # only the authority answers for a mesh: a mirror says no, whatever
        # its record holds
        mm_b.get("m@pcA").offers["pcC"] = {"token": "k", "created_at": ""}
        assert mm_b.peer_offer_check("m", "pcC", "k") == {"live": False}

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_withdrawal_needs_the_token_or_the_owner(home, tmp_path):
    """O2: a withdrawal that does not carry the stored token is refused
    while the owner still reports the offer live; the owner's own
    withdrawal carries it; a row its owner dropped goes on any withdrawal."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        await mm_a.offer_mesh("m", "pcB")
        held = mm_b._offers["m@pcA"]["token"]

        for token in ("", "other"):
            with pytest.raises(MeshError, match="still live"):
                await mm_b.peer_offer_accept("m", "pcA", token, cancel=True)
            assert [o["mesh"] for o in mm_b.offers_received()] == ["m"]
            assert mm_b._offers["m@pcA"]["token"] == held

        # a second offer under the same key cannot swap the token either
        with pytest.raises(MeshError, match="does not confirm"):
            await mm_b.peer_offer_accept("m", "pcA", "other")
        assert mm_b._offers["m@pcA"]["token"] == held

        # the owner withdraws: the push carries the token and the row goes
        res = await mm_a.cancel_offer("m", "pcB")
        assert res["notified"] is True
        assert mm_b.offers_received() == []
        # withdrawing a row that is gone is not an error
        assert (await mm_b.peer_offer_accept("m", "pcA", "", cancel=True))[
            "cancelled"] is True

        # the owner lost its record without telling us (a missed push):
        # the owner now reports the stored token as not live
        await mm_a.offer_mesh("m", "pcB")
        mm_a.get("m").offers.clear()
        await mm_b.peer_offer_accept("m", "pcA", "", cancel=True)
        assert mm_b.offers_received() == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_an_owner_without_the_check_and_an_owner_out_of_reach(home, tmp_path):
    """O3: an owner that predates the check (a plain 404 on its route)
    gets the old behaviour, logged; O4: an owner that cannot be asked, or
    no relay at all, stores nothing and keeps what is stored."""
    from claude_launcher.daemon.mesh import PeerUnreachable

    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        mm_b.machine = "pcB"
        failure = {}

        async def call(machine, path, body):
            assert (machine, path) == ("pcOld", "/peer/mesh/offer/check")
            raise failure["exc"]

        # O4: no relay to ask through
        with pytest.raises(MeshError, match="relay uplink"):
            await mm_b.peer_offer_accept("m", "pcOld", "t1")

        mm_b.peer_transport = call
        # O3: 404 — stored unverified, and a tokenless withdrawal drops it
        failure["exc"] = PeerUnreachable("non-JSON body (status 404)",
                                         status=404)
        await mm_b.peer_offer_accept("m", "pcOld", "t1")
        assert [o["mesh"] for o in mm_b.offers_received()] == ["m"]
        await mm_b.peer_offer_accept("m", "pcOld", "", cancel=True)
        assert mm_b.offers_received() == []

        # O4: unreachable, or another HTTP status — nothing stored
        await mm_b.peer_offer_accept("m", "pcOld", "t1")  # 404 again
        for exc in (PeerUnreachable("relay down"),
                    PeerUnreachable("non-JSON body (status 500)", status=500)):
            failure["exc"] = exc
            with pytest.raises(MeshError, match="cannot confirm"):
                await mm_b.peer_offer_accept("n", "pcOld", "t2")
            # and a withdrawal without the token keeps the stored row
            with pytest.raises(MeshError, match="cannot confirm"):
                await mm_b.peer_offer_accept("m", "pcOld", "", cancel=True)
            assert [o["mesh"] for o in mm_b.offers_received()] == ["m"]
        # the stored token still withdraws it without asking anyone
        await mm_b.peer_offer_accept("m", "pcOld", "t1", cancel=True)
        assert mm_b.offers_received() == []

    asyncio.run(run())


def test_owner_records_before_the_push_and_rolls_back(home, tmp_path):
    """O5: the owner's record exists while the push is in flight (the
    receiver's check finds it) and is put back as it was when the push
    fails."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_a.machine = "pcA"
        mm_a.create("m")
        seen = {}

        async def call(machine, path, body):
            seen["during"] = dict(mm_a.get("m").offers.get(machine) or {})
            seen["check"] = mm_a.peer_offer_check("m", machine, body["token"])
            raise MeshError("peer 'pcB' rejected /peer/mesh/offer: no")

        mm_a.peer_transport = call
        with pytest.raises(MeshError):
            await mm_a.offer_mesh("m", "pcB")
        assert seen["check"] == {"live": True}
        assert mm_a.get("m").offers == {}
        assert mm_a.get("m").visibility == "private"

        # a re-offer that fails keeps the earlier offer and its token
        mm_a.get("m").offers["pcB"] = {"token": "kept", "created_at": "t0"}
        with pytest.raises(MeshError):
            await mm_a.offer_mesh("m", "pcB")
        assert seen["during"]["token"] == "kept"
        assert mm_a.get("m").offers == {
            "pcB": {"token": "kept", "created_at": "t0"}}

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
            # the project a mesh is filed under here; unknown is refused
            resp = await client.put(
                "/api/mesh/web/project", json={"project": "nosuch"},
                headers=bearer,
            )
            assert resp.status == 400
            resp = await client.put(
                "/api/mesh/web/project", json={"project": "default"},
                headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json()) == {"mesh": "web", "project": "default"}

            # /peer/meshes needs no daemon token: it lists public names only
            resp = await client.post("/peer/meshes", json={})
            assert resp.status == 200
            assert [m["mesh"] for m in (await resp.json())["meshes"]] == ["web"]

            # /peer/mesh/offer/check needs none either, and says only yes/no
            mm.get("web").offers["pcQ"] = {"token": "live1", "created_at": ""}
            for body, live in (
                ({"mesh": "web", "machine": "pcQ", "token": "live1"}, True),
                ({"mesh": "web", "machine": "pcQ", "token": "nope"}, False),
                ({"mesh": "web", "machine": "pcR", "token": "live1"}, False),
                ({"mesh": "nosuch", "machine": "pcQ", "token": "live1"}, False),
                ({}, False),
            ):
                resp = await client.post("/peer/mesh/offer/check", json=body)
                assert resp.status == 200
                assert await resp.json() == {"live": live}, body
            mm.get("web").offers.clear()

            # no relay here: discovery answers with the reason, not a 500
            resp = await client.get("/api/relay/meshes", headers=bearer)
            assert resp.status == 200
            doc = await resp.json()
            assert doc["meshes"] == [] and "relay" in doc["errors"]

            # an offer nobody can confirm is not stored: no relay here
            offer = {"mesh": "far", "machine": "pcZ", "token": "t0k",
                     "members": 2}
            resp = await client.post("/peer/mesh/offer", json=offer)
            assert resp.status == 400
            assert mm.offers_received() == []

            # an offer arrives over /peer, its owner confirms it, and it is
            # listed without its token
            asked = []

            async def owner(machine, path, body):
                asked.append((machine, path, body))
                return {"live": body["token"] == "t0k"}

            mm.machine = "pcA"
            mm.peer_transport = owner
            resp = await client.post("/peer/mesh/offer", json=offer)
            assert resp.status == 200
            assert asked == [("pcZ", "/peer/mesh/offer/check",
                              {"mesh": "far", "machine": "pcA",
                               "token": "t0k"})]
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


# --------------------------------------------------------------------------- #
# A7
# --------------------------------------------------------------------------- #
def test_mirror_is_filed_under_a_project(home, tmp_path):
    _register_py_harness()
    projects.add("gds6")

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        mm_d = MeshManager(mgr, settle=0.05, root=tmp_path / "meshD")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c, "pcD": mm_d})

        # offered: filed in the one call
        await mm_a.offer_mesh("m", "pcB")
        with pytest.raises(MeshError):
            await mm_b.attach("m@pcA", project="nosuch")
        res = await mm_b.attach("m@pcA", project="gds6")
        assert res["project"] == "gds6"
        assert mm_b.get("m@pcA").project == "gds6"
        again = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        again.load_all()
        assert again.get("m@pcA").project == "gds6"

        # pending: the project rides the request until the grant lands
        await mm_a.set_visibility("m", "public")
        rid = (await mm_c.attach("m@pcA", project="gds6"))["request_id"]
        await mm_a.approve_request("m", rid)
        assert mm_c.get("m@pcA").project == "gds6"

        # a session join files the mirror where the session is
        mgr.create(SessionDef(name="sd", harness="py", cwd=str(tmp_path),
                              rows=80, project="gds6"))
        await mm_d.join("m@pcA", "sd", handle="dora",
                        code=mm_a.invite("m")["code"])
        assert mm_d.get("m@pcA").project == "gds6"

        # moved by hand, both ways; the peers are not told
        mirror = mm_b.get("m@pcA")
        assert mm_b.set_project("m@pcA", "") == {"mesh": "m@pcA", "project": "default"}
        assert mirror.project == ""
        mm_b.set_project("m", "gds6")
        assert mirror.project == "gds6"
        assert mm_a.get("m").project == ""
        with pytest.raises(MeshError):
            mm_b.set_project("m", "nosuch")
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# A8
# --------------------------------------------------------------------------- #
def _link_agrees(mm_a, mm_b, key="m@pcA"):
    owner, mirror = mm_a.get("m"), mm_b.get(key)
    return (owner.links["pcB"]["token_in"] == mirror.links["pcA"]["token_out"]
            and owner.links["pcB"]["token_out"] == mirror.links["pcA"]["token_in"])


def test_attach_and_session_join_pending_together(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        await mm_a.set_visibility("m", "public")
        rid_attach = (await mm_b.attach("m@pcA"))["request_id"]
        rid_join = (await mm_b.join("m@pcA", "sb", handle="bob"))["request_id"]

        await mm_a.approve_request("m", rid_attach)
        assert _link_agrees(mm_a, mm_b)
        out = await mm_a.approve_request("m", rid_join)
        # the second grant is merged into the mirror the first one built
        assert out["delivered"] is True
        mirror = mm_b.get("m@pcA")
        assert set(mirror.members) == {"alice", "bob"}
        assert mm_a.get("m").members["bob"].machine == "pcB"
        assert "pcB" in mm_a.get("m").peers
        assert _link_agrees(mm_a, mm_b)
        assert mm_b.outgoing_list() == []
        # and the re-minted link carries traffic both ways
        await mm_b.send("m@pcA", "bob", "alice", "hi from bob")
        assert [x["body"] for x in mm_a.get("m").messages][-1] == "hi from bob"
        await mgr.shutdown_all()

    asyncio.run(run())


def test_rejected_grant_keeps_the_attach_link(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        _wire({"pcA": mm_a, "pcB": mm_b})
        await mm_a.set_visibility("m", "public")
        rid = (await mm_b.join("m@pcA", "sb", handle="bob"))["request_id"]
        await mm_a.offer_mesh("m", "pcB")
        await mm_b.attach("m@pcA")
        # pcB forgets its request; the owner approves it anyway, and pcB
        # rejects the grant as an unknown request
        mm_b.cancel_request(rid)
        await mm_a.approve_request("m", rid)
        owner = mm_a.get("m")
        assert "bob" not in owner.members
        assert not any("bob" in k.split("|") for k in owner.member_edges)
        # the attach link is intact on both sides, and still carries traffic
        assert "pcB" in owner.peers and _link_agrees(mm_a, mm_b)
        mgr.create(SessionDef(name="sb2", harness="py", cwd=str(tmp_path), rows=80))
        await mm_b.join("m", "sb2", handle="ben")
        assert owner.members["ben"].machine == "pcB"
        await mgr.shutdown_all()

    asyncio.run(run())
