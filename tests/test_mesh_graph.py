"""Ranked peer graph (phase 7): scenario-derived unit tests.

Scenario matrix, derived from docs/mesh-design.md "Ranked peer graph":

A. Rank
   A1 a mesh's peers are an ordered list; peers[0] holds the authority and
      nothing has to be declared per link
   A2 the lower-ranked side owns an edge, whichever pair it is
   A3 rank order converges on every daemon through the ordinary sync

B. Topology
   B1 a joining peer is linked with EVERY existing peer, not just the
      authority (complete graph), with credentials brokered by the authority
   B2 both ends of a brokered edge agree on the token pair (duplex)
   B3 revoking a peer drops its edges everywhere
   B4 a peer may cut and restore an edge it terminates; the change is
      forwarded to the authority and converges on the far end too
   B5 a peer may NOT touch an edge between two other daemons
   B6 nobody cuts an edge to the authority — it carries the sequenced log

C. Fast path
   C1 with the authority unreachable, a send still reaches a peer's member
      terminal directly, and stays queued for sequencing
   C2 the sequenced copy folds over the fast-path one: it lands in the log
      exactly once and is never injected twice
   C3 a cut edge refuses the fast path; the message waits for the authority
   C4 the fast path is not used while the authority is reachable

D. Migration
   D1 v2 primary/mirror state on disk becomes a rank list on load

E. Reorder / handover
   E1 only the current authority may reorder
   E2 the new order must be a permutation of the peer list
   E3 a reshuffle below rank 0 is not a handover
   E4 a handover moves the authority, bumps the epoch and converges
   E5 sequence numbers continue above everything already written
   E6 a handover to an unreachable successor rolls back entirely
   E7 the roster stays absolute, so delivery keeps following the session

F. HTTP surface
   F1 PUT /peers reorders; F2 a bad permutation 400s without half-applying;
   F3 PATCH /links/{a}/{b} cuts an edge, and refuses the authority's own

G. Deletion is durable
   G1 a retired directory is never mounted again, and every mesh in the
      listing can be looked up by name (key == mesh.name, always)
   G2 delete then reload from disk: gone from the listing, kept on disk
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshError, MeshManager, PeerUnreachable

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _screen_text(session) -> str:
    return "\n".join(session.capture())


async def _wait_idle(session, timeout: float = 20.0) -> None:
    """Delivery is idle-gated, so a test that injects by hand must wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status() == "idle":
            return
        await asyncio.sleep(0.1)
    raise AssertionError("session never went idle")


async def _wait_screen(session, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _screen_text(session):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{needle!r} never appeared on screen; got:\n{_screen_text(session)}"
    )


# --------------------------------------------------------------------------- #
# in-process wiring: the /peer/* HTTP layer, minus the HTTP
# --------------------------------------------------------------------------- #
def _dispatch_peer(mm: MeshManager, path: str, body: dict) -> dict:
    if path == "/peer/mesh/join_request":
        return mm.peer_join_request_accept(
            body["mesh"], body["machine"],
            body.get("session") or "", body.get("handle") or "",
            body.get("role") or "", body.get("reply_token") or "",
            body.get("code") or "",
        )
    if path == "/peer/mesh/grant":
        return mm.peer_grant_accept(
            body["mesh"], body["machine"],
            body.get("request_id") or "", body.get("token") or "",
            bool(body.get("denied")), body.get("grant"),
        )
    if path == "/peer/mesh/unlink":
        return mm.peer_unlink_accept(
            body["mesh"], body["machine"], body.get("token") or ""
        )
    if path == "/peer/mesh/send":
        return mm.peer_send_accept(
            body["mesh"], body["machine"], body["token"], body.get("message") or {}
        )
    if path == "/peer/mesh/deliver":
        return mm.peer_deliver_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("message") or {},
        )
    if path == "/peer/mesh/link":
        return mm.peer_link_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("a") or "", body.get("b") or "",
            bool(body.get("enabled")),
        )
    if path == "/peer/mesh/sync":
        return mm.peer_sync_accept(
            body["mesh"], body["machine"], body["token"],
            int(body.get("base") or 0), body.get("messages") or [],
            body.get("members") or [], body.get("policy"),
            body.get("nudges") or [],
            peers=body.get("peers"), epoch=body.get("epoch"),
            links=body.get("links"), edges=body.get("edges"),
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _wire(machines: dict, *, down=()) -> None:
    """Identity + a direct transport, with ``down`` machines unreachable."""

    async def call(machine, path, body):
        if machine in down:
            raise PeerUnreachable(f"{machine} is down")
        return _dispatch_peer(machines[machine], path, body)

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


async def _trio(mgr, tmp_path):
    """pcA (authority, alice) + pcB (bob) + pcC (carol), all three linked."""
    mms = {
        name: MeshManager(mgr, settle=0.05, root=tmp_path / ("mesh" + name))
        for name in ("pcA", "pcB", "pcC")
    }
    _wire(mms)
    for s in ("sa", "sb", "sc"):
        mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
    mms["pcA"].create("m")
    await mms["pcA"].join("m", "sa", handle="alice")
    await mms["pcB"].join(
        "m@pcA", "sb", handle="bob", code=mms["pcA"].invite("m")["code"]
    )
    await mms["pcC"].join(
        "m@pcA", "sc", handle="carol", code=mms["pcA"].invite("m")["code"]
    )
    # carol joined last, so pcB still has to be told about her
    await mms["pcA"]._flush_guest(mms["pcA"].get("m"), "pcB")
    return mms


# --------------------------------------------------------------------------- #
# A. rank
# --------------------------------------------------------------------------- #
def test_rank_order_is_the_authority_and_edge_roles(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        a, b, c = (mms[n].get("m") for n in ("pcA", "pcB", "pcC"))

        # A1: one ordered list, rank 0 holds the authority
        assert a.peers == ["pcA", "pcB", "pcC"]
        assert a.authority == "pcA"
        assert a.primary == "" and b.primary == "pcA" and c.primary == "pcA"
        assert b.rank("pcB") == 1 and c.rank("pcC") == 2

        # A2: the lower-ranked side owns the edge, on every pair
        assert a.owns_link("pcB") and a.owns_link("pcC")
        assert b.owns_link("pcC") and not b.owns_link("pcA")
        assert not c.owns_link("pcA") and not c.owns_link("pcB")

        # A3: the order converged everywhere through the ordinary sync
        assert b.peers == ["pcA", "pcB", "pcC"] == c.peers

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. topology
# --------------------------------------------------------------------------- #
def test_join_links_every_pair_with_brokered_credentials(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        a, b, c = (mms[n].get("m") for n in ("pcA", "pcB", "pcC"))

        # B1: the complete graph — pcB and pcC are linked to each other even
        # though neither ever spoke to the other during the join
        assert set(b.links) == {"pcA", "pcC"}
        assert set(c.links) == {"pcA", "pcB"}
        assert set(a.links) == {"pcB", "pcC"}

        # B2: both ends agree, and each direction has its own token (duplex)
        assert b.links["pcC"]["token_out"] == c.links["pcB"]["token_in"]
        assert c.links["pcB"]["token_out"] == b.links["pcC"]["token_in"]
        assert b.links["pcC"]["token_out"] != b.links["pcC"]["token_in"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_revoking_a_peer_drops_its_edges_everywhere(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a = mms["pcA"]

        await mm_a.revoke_guest("m", "pcC")
        await mm_a._flush_guest(mm_a.get("m"), "pcB")

        # B3: pcB no longer holds an edge to the revoked peer
        assert set(mm_a.get("m").links) == {"pcB"}
        assert set(mms["pcB"].get("m").links) == {"pcA"}
        assert "carol" not in mms["pcB"].get("m").members

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_peer_edits_its_own_edge_and_only_its_own(home, tmp_path):
    """An edge is duplex, so either end has standing to sever it — but an
    edge between two other daemons is not yours to touch. The peer's edit is
    forwarded to the authority, which owns the table and fans it back out."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b, mm_c = mms["pcA"], mms["pcB"], mms["pcC"]

        # B4: pcB cuts the edge it shares with pcC, from pcB
        await mm_b.set_link("m", "pcB", "pcC", enabled=False)
        assert mm_b.get("m").linked("pcC") is False       # locally, at once
        assert mm_a.get("m").edges["pcB|pcC"] is False    # and at the authority
        await mm_a._flush_guest(mm_a.get("m"), "pcC")
        assert mm_c.get("m").linked("pcB") is False       # ...and at the far end

        # and back again, from the other end this time
        await mm_c.set_link("m", "pcB", "pcC", enabled=True)
        await mm_a._flush_guest(mm_a.get("m"), "pcB")
        assert mm_b.get("m").linked("pcC") is True
        assert mm_c.get("m").linked("pcB") is True

        # B6: the authority's own edges carry the log — revoke, never cut
        for mm in (mm_a, mm_b):
            with pytest.raises(MeshError, match="carries the sequenced log"):
                await mm.set_link("m", "pcA", "pcB", enabled=False)
        assert [e["editable"] for e in mm_b.edge_table(mm_b.get("m"))] == [
            False,  # pcA <-> pcB : authority edge
            False,  # pcA <-> pcC : authority edge
            True,   # pcB <-> pcC : ours
        ]

        # B5: a fourth peer, so that pcB <-> pcD is an edge pcC neither
        # terminates nor may touch — with only three, every such edge is an
        # authority edge and B6 would answer first.
        mms["pcD"] = MeshManager(mgr, settle=0.05, root=tmp_path / "meshpcD")
        _wire(mms)
        mgr.create(SessionDef(name="sd", harness="py", cwd=str(tmp_path)))
        await mms["pcD"].join(
            "m@pcA", "sd", handle="dave", code=mm_a.invite("m")["code"]
        )
        await mm_a._flush_guest(mm_a.get("m"), "pcC")  # pcC learns of pcD
        # Refused locally, and refused again at the authority: a doctored
        # client cannot smuggle it past by calling the peer endpoint direct.
        with pytest.raises(MeshError, match="between two other daemons"):
            await mm_c.set_link("m", "pcB", "pcD", enabled=False)
        with pytest.raises(MeshError, match="only edit the edges it terminates"):
            mm_a.peer_link_accept(
                "m", "pcC", mm_a.get("m").links["pcC"]["token_in"],
                "pcB", "pcD", False,
            )
        assert mm_a.get("m").edges["pcB|pcD"] is True

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. fast path
# --------------------------------------------------------------------------- #
def test_peers_keep_talking_while_the_authority_is_down(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_b, mm_c = mms["pcB"], mms["pcC"]
        carol = mgr.get("sc")
        await _wait_screen(carol, "join briefing")

        # the authority falls over; pcB <-> pcC is untouched
        _wire(mms, down=("pcA",))

        sent = await mm_b.send("m", "bob", "carol", "authority is down, carrying on")
        # C1: queued for sequencing, but delivered directly all the same
        assert sent["queued"] is True
        assert "pcC" in sent["notice"]
        assert [e["body"] for e in mm_b.get("m").outbox] == [
            "authority is down, carrying on"
        ]
        assert len(mm_c.get("m").provisional) == 1
        assert mm_c.get("m").messages == []  # nothing sequenced yet

        await _wait_idle(carol)
        await mm_c._deliver_to(mm_c.get("m"), mm_c.get("m").members["carol"])
        await _wait_screen(carol, "authority is down, carrying on")
        await _wait_idle(carol)

        # C2: the authority returns, sequences it, and the copy folds in
        _wire(mms)
        mm_b.get("m").peer_status.clear()  # skip the reconnect backoff
        await mm_b._flush_upstream(mm_b.get("m"))
        await mms["pcA"]._flush_guest(mms["pcA"].get("m"), "pcC")
        mesh_c = mm_c.get("m")
        assert [m["body"] for m in mesh_c.messages] == [
            "authority is down, carrying on"
        ]
        assert mesh_c.provisional == []
        # ...and delivering again does not re-inject it
        assert mesh_c.pending("carol") == []
        before = _screen_text(carol).count("authority is down, carrying on")
        await mm_c._deliver_to(mesh_c, mesh_c.members["carol"])
        assert _screen_text(carol).count("authority is down, carrying on") == before

        await mgr.shutdown_all()

    asyncio.run(run())


def test_cut_edge_refuses_the_fast_path(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b, mm_c = mms["pcA"], mms["pcB"], mms["pcC"]

        await mm_a.set_link("m", "pcB", "pcC", enabled=False)
        for peer in ("pcB", "pcC"):
            await mm_a._flush_guest(mm_a.get("m"), peer)
        assert mm_b.get("m").linked("pcC") is False

        _wire(mms, down=("pcA",))
        sent = await mm_b.send("m", "bob", "carol", "over a cut edge")

        # C3: no direct hop is attempted; the message waits for the authority
        assert sent["queued"] is True
        assert "delivered directly" not in (sent["notice"] or "")
        assert mm_c.get("m").provisional == []

        _wire(mms)
        await mm_b._flush_upstream(mm_b.get("m"))
        await mm_a._flush_guest(mm_a.get("m"), "pcC")
        assert [m["body"] for m in mm_c.get("m").messages] == ["over a cut edge"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_healthy_authority_carries_the_message_once(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b, mm_c = mms["pcA"], mms["pcB"], mms["pcC"]

        sent = await mm_b.send("m", "bob", "carol", "the normal path")
        # C4: sequenced synchronously, so the fast path never arms
        assert sent["queued"] is False
        assert mm_b.get("m").outbox == []
        assert mm_c.get("m").provisional == []

        await mm_a._flush_guest(mm_a.get("m"), "pcC")
        assert [m["body"] for m in mm_c.get("m").messages] == ["the normal path"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# E. reorder / handover
# --------------------------------------------------------------------------- #
def test_reorder_is_the_authoritys_to_make(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b = mms["pcA"], mms["pcB"]

        # E1: a peer cannot promote itself
        with pytest.raises(MeshError, match="authority's to set"):
            await mm_b.reorder_peers("m", ["pcB", "pcA", "pcC"])
        # E2: the order must be a permutation of the peers
        with pytest.raises(MeshError, match="exactly the mesh's peers"):
            await mm_a.reorder_peers("m", ["pcA", "pcB"])
        with pytest.raises(MeshError, match="unknown: pcZ"):
            await mm_a.reorder_peers("m", ["pcA", "pcB", "pcZ"])

        # E3: a pure reshuffle below rank 0 is not a handover
        result = await mm_a.reorder_peers("m", ["pcA", "pcC", "pcB"])
        assert result["handover"] is False
        assert result["epoch"] == 0
        assert mm_a.get("m").peers == ["pcA", "pcC", "pcB"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_handover_moves_authority_and_bumps_the_epoch(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b, mm_c = mms["pcA"], mms["pcB"], mms["pcC"]
        await mm_a.send("m", "alice", "*", "before the handover")

        result = await mm_a.reorder_peers("m", ["pcB", "pcA", "pcC"])
        assert result["handover"] is True
        assert result["epoch"] == 1 and result["authority"] == "pcB"

        # E4: the old authority is now an ordinary peer, and the new rank
        # order reaches the others through the ordinary sync
        assert mm_a.get("m").primary == "pcB"
        for peer in ("pcB", "pcC"):
            await mm_a._flush_guest(mm_a.get("m"), peer)
        assert mm_b.get("m").peers == ["pcB", "pcA", "pcC"]
        assert mm_b.get("m").primary == ""   # pcB now holds it
        assert mm_c.get("m").primary == "pcB"
        assert mm_b.get("m").authority_epoch == 1

        # E5: sequence numbers continue above everything already written, so
        # (epoch, seq) stays strictly increasing across the handover
        mesh_b = mm_b.get("m")
        assert mesh_b.next_seq >= 1
        sent = mm_b._send_core(mesh_b, "bob", "carol", "after the handover")
        assert sent["epoch"] == 1 and sent["seq"] >= 1
        assert [
            (m.get("epoch"), m.get("seq")) for m in mesh_b.messages
        ] == sorted((m.get("epoch"), m.get("seq")) for m in mesh_b.messages)

        await mgr.shutdown_all()

    asyncio.run(run())


def test_handover_to_an_unreachable_successor_is_refused(home, tmp_path):
    """A half-applied handover would leave nobody sequencing.

    The outgoing authority is the only daemon that knows the new order, and
    once it demotes itself it can no longer push. So the successor's copy is
    load-bearing: if it cannot be delivered, the whole reorder rolls back.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a = mms["pcA"]
        before = list(mm_a.get("m").peers)

        _wire(mms, down=("pcB",))
        with pytest.raises(MeshError, match="could not be told"):
            await mm_a.reorder_peers("m", ["pcB", "pcA", "pcC"])

        # nothing moved: rank order, authority and epoch are all as they were
        mesh_a = mm_a.get("m")
        assert mesh_a.peers == before
        assert mesh_a.primary == "" and mesh_a.authority_epoch == 0
        assert mms["pcC"].get("m").primary == "pcA"

        # ...and it succeeds once that daemon is back
        _wire(mms)
        result = await mm_a.reorder_peers("m", ["pcB", "pcA", "pcC"])
        assert result["handover"] is True
        assert mms["pcB"].get("m").primary == ""

        await mgr.shutdown_all()

    asyncio.run(run())


def test_handover_keeps_delivery_pointed_at_the_right_daemon(home, tmp_path):
    """The roster must stay absolute across a handover.

    Before phase 7 the authority's own members carried a blank machine,
    which was unambiguous only because authority never moved. Left blank,
    the *new* rank 0 would claim them and try to inject into sessions it
    does not host, while the old one would stop delivering to its own.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a, mm_b = mms["pcA"], mms["pcB"]
        alice_session = mgr.get("sa")
        await _wait_screen(alice_session, "join briefing")

        await mm_a.reorder_peers("m", ["pcB", "pcA", "pcC"])
        for peer in ("pcB", "pcC"):
            await mm_a._flush_guest(mm_a.get("m"), peer)

        # alice lives on pcA and still does, whoever holds rank 0
        assert mm_a.get("m").members["alice"].machine == "pcA"
        assert mm_b.get("m").members["alice"].machine == "pcA"
        assert mm_a._is_local(mm_a.get("m"), mm_a.get("m").members["alice"])
        assert not mm_b._is_local(mm_b.get("m"), mm_b.get("m").members["alice"])
        info = mm_b.mesh_info(mm_b.get("m"))
        by = {p["machine"]: p["members"] for p in info["peers"]}
        assert by["pcA"] == ["alice"] and by["pcB"] == ["bob"]

        # ...and a message from the new authority still reaches her terminal
        await mm_b.send("m", "bob", "alice", "still your mail after handover")
        await mm_b._flush_guest(mm_b.get("m"), "pcA")
        await _wait_idle(alice_session)
        mesh_a = mm_a.get("m")
        await mm_a._deliver_to(mesh_a, mesh_a.members["alice"])
        await _wait_screen(alice_session, "still your mail after handover")

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# F. HTTP surface
# --------------------------------------------------------------------------- #
def test_api_exposes_rank_and_link_state(home, tmp_path):
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mm_a = mms["pcA"]
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm_a)
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            info = await (await client.get("/api/mesh/m", headers=bearer)).json()
            assert [p["machine"] for p in info["peers"]] == ["pcA", "pcB", "pcC"]
            assert info["authority"] == "pcA" and info["epoch"] == 0

            # F1: reorder below rank 0 keeps the authority put
            resp = await client.put(
                "/api/mesh/m/peers",
                json={"order": ["pcA", "pcC", "pcB"]}, headers=bearer,
            )
            assert resp.status == 200
            assert (await resp.json())["handover"] is False

            # F2: a bad permutation is a 400, not a half-applied order
            resp = await client.put(
                "/api/mesh/m/peers", json={"order": ["pcA"]}, headers=bearer
            )
            assert resp.status == 400
            assert mm_a.get("m").peers == ["pcA", "pcC", "pcB"]

            # F3: cutting a peer-to-peer edge, and the refusal to cut the
            # edge that carries the sequenced log
            resp = await client.patch(
                "/api/mesh/m/links/pcB/pcC", json={"enabled": False},
                headers=bearer,
            )
            assert resp.status == 200 and (await resp.json())["enabled"] is False
            resp = await client.patch(
                "/api/mesh/m/links/pcA/pcB", json={"enabled": False},
                headers=bearer,
            )
            assert resp.status == 400
            assert "authority" in (await resp.json())["error"]

            info = await (await client.get("/api/mesh/m", headers=bearer)).json()
            assert info["peers"][0]["self"] is True
            assert info["peers"][0]["members"] == ["alice"]
        finally:
            await client.close()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. migration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "doc, machine, expect_peers, expect_links",
    [
        (
            {  # a v2 primary with two guests, in link order
                "primary": "",
                "guests": {
                    "pcC": {"token_in": "c", "token_out": "C",
                            "created_at": "2026-02-01T00:00:00+00:00"},
                    "pcB": {"token_in": "b", "token_out": "B",
                            "created_at": "2026-01-01T00:00:00+00:00"},
                },
            },
            "pcA",
            ["pcA", "pcB", "pcC"],
            {"pcB", "pcC"},
        ),
        (
            {  # a v2 mirror
                "primary": "pcA",
                "link": {"token_in": "x", "token_out": "y"},
            },
            "pcB",
            ["pcA", "pcB"],
            {"pcA"},
        ),
    ],
)
def test_v2_state_becomes_a_rank_list(home, tmp_path, doc, machine,
                                      expect_peers, expect_links):
    root = tmp_path / "mesh"
    d = root / "m"
    d.mkdir(parents=True)
    (d / "mesh.json").write_text(
        json.dumps({"name": "m", "members": {}, "invites": {}, **doc}),
        encoding="utf-8",
    )
    mm = MeshManager(_manager(), root=root)
    mm.load_all()
    # D1: the relay name arrives after load_all — that is when we can tell
    # where we sit in the order, so migration completes there
    mm.machine = machine
    mesh = mm.get("m")
    assert mesh.peers == expect_peers
    assert set(mesh.links) == expect_links
    assert mesh.authority == "pcA"
    assert json.loads((d / "mesh.json").read_text(encoding="utf-8"))["peers"] == (
        expect_peers
    )


# --------------------------------------------------------------------------- #
# G. deletion is durable
# --------------------------------------------------------------------------- #
def test_a_deleted_mesh_does_not_come_back_on_restart(home, tmp_path):
    """G1. Deleting a mesh retires its directory to '<name>.deleted-<stamp>'
    rather than erasing the history. But load_all mounted every directory
    holding a mesh.json, keyed by the DIRECTORY name, while the object kept
    its own — so after the next daemon restart the deleted mesh reappeared in
    the sidebar as 'mesh0' and every lookup for 'mesh0' answered "no mesh
    named 'mesh0'". Un-openable, and un-deletable: Remove mesh resolves the
    name too. The list and the lookup must not be able to disagree.
    """
    root = tmp_path / "mesh"
    live, dead = root / "keep", root / "gone.deleted-20260808T132143Z"
    for d, name in ((live, "keep"), (dead, "gone")):
        d.mkdir(parents=True)
        (d / "mesh.json").write_text(
            json.dumps({"name": name, "members": {}, "invites": {}}),
            encoding="utf-8",
        )

    mm = MeshManager(_manager(), root=root)
    mm.load_all()
    assert [m.name for m in mm.list()] == ["keep"]
    with pytest.raises(MeshError):
        mm.get("gone")

    # ...and every mesh the listing offers can actually be opened, which is
    # the invariant that was broken: key == mesh.name, for all of them.
    for m in mm.list():
        assert mm.get(m.name) is m


def test_deleting_a_mesh_survives_a_reload(home, tmp_path):
    """G2. The same thing end to end: delete, reload from disk, stay gone."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "mesh"
        mm = MeshManager(mgr, settle=0.05, root=root)
        mm.machine = "pcA"
        mgr.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path)))
        mm.create("doomed")
        await mm.join("doomed", "sa", handle="alice")
        mm.delete("doomed")

        again = MeshManager(_manager(), root=root)
        again.load_all()
        assert [m.name for m in again.list()] == []
        # the history is still on disk — retired, not erased
        left = [p.name for p in root.iterdir() if p.is_dir()]
        assert len(left) == 1 and left[0].startswith("doomed.deleted-")
        assert (root / left[0] / "mesh.json").is_file()
        await mgr.shutdown_all()

    asyncio.run(run())
