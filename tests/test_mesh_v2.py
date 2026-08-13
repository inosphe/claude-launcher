"""Federation v2 (primary/mirror): scenario-derived unit tests.

Scenario matrix, derived from docs/mesh-design.md "Federation v2":

A. Link & roles
   A1 only the primary mints invites (a mirror refuses)
   A2 the first join from a machine creates a mirror with a roster+log+
      policy snapshot and an asymmetric credential pair
   A3 a join address is refused when a local mesh of that name exists
   A4 a mirror refuses policy edits; its name is occupied (create conflicts)
   A5 primary and mirror state survive a reload (guests/cursors; primary/
      link/roster/log/outbox)
   A6 v1 federation state (links / peers cursors) is dropped on load

B. Membership (secondary guest members)
   B1 guest join forwards to the primary: central handle uniqueness,
      roster fanout to every guest
   B2 join fails fast when the primary is unreachable
   B3 guest leave forwards; a primary-side kick reaches guests on sync
   B4 the join briefing lands in the guest member's terminal

C. Messaging (hub sequencing)
   C1 owned-mesh local sends keep v1 semantics (ids, intents, advisories)
   C2 a guest send is sequenced by the primary and delivered into a
      primary-local recipient's terminal; history is identical on both
   C3 guest-to-guest crosses via the hub (three daemons, no B-C link)
   C4 a guest-local DM also goes through the primary (queued while down,
      identical logs after reconnect)
   C5 sections / reply_to / type survive the hop; slices happen at the
      delivering daemon
   C6 duplicate upstream sends and sync replays dedupe by id
   C7 a cursor mismatch triggers the resync handshake and converges

D. Failure
   D1 sends queue durably in the outbox while the primary is unreachable;
      order is preserved when it drains
   D2 primary fanout to an unreachable guest backs off and drains later
   D3 the mirror stays readable while offline

E. Policy (primary-only engine)
   E1 policy edits: primary ok, mirror refuses (see A4)
   E2 guest sync-acks piggyback member activity; the primary stores it
   E3 a remote member's heartbeat is decided by the primary and injected
      by the guest daemon
   E4 stall warnings reach remote leaders as ordinary fyi messages
   E5 tick() on a mirror is a guarded no-op

F. Self-identification ("which member am I?")
   F1 the roster states each row's locality instead of implying it
   F2 a mirror's own member resolves — the blank test never could
   F3 an authority member still resolves after a stamping pass
   F4 a session name colliding with the authority's does not steal its row
   F5 'nobody asked' stays distinct from 'you are nobody'
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon import mesh_policy
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import (
    MeshConflict,
    MeshError,
    MeshManager,
    PeerUnreachable,
    format_delivery,
)

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


async def _wait_screen(session, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _screen_text(session):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{needle!r} never appeared on screen; got:\n{_screen_text(session)}"
    )


async def _wait_for(cond, what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


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
    if path == "/peer/mesh/join":
        return mm.peer_join_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("session") or "", body.get("handle") or "",
            body.get("role") or "", body.get("parent") or "",
        )
    if path == "/peer/mesh/leave":
        return mm.peer_leave_accept(
            body["mesh"], body["machine"], body["token"], body.get("handle") or ""
        )
    if path == "/peer/mesh/send":
        return mm.peer_send_accept(
            body["mesh"], body["machine"], body["token"], body.get("message") or {}
        )
    if path == "/peer/mesh/sync":
        return mm.peer_sync_accept(
            body["mesh"], body["machine"], body["token"],
            int(body.get("base") or 0), body.get("messages") or [],
            body.get("members") or [], body.get("policy"),
            body.get("nudges") or [],
            peers=body.get("peers"), epoch=body.get("epoch"),
            links=body.get("links"), edges=body.get("edges"),
            member_edges=body.get("member_edges"),
        )
    if path == "/peer/mesh/deliver":
        return mm.peer_deliver_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("message") or {},
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _wire(machines: dict) -> None:
    """Give every manager an identity and a direct transport to the others."""

    async def call(machine, path, body):
        return _dispatch_peer(machines[machine], path, body)

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


def _break_transport(mm: MeshManager) -> list:
    calls = []

    async def broken(machine, path, body):
        calls.append(path)
        raise PeerUnreachable("relay down")

    mm.peer_transport = broken
    mm.relay_connected = lambda: False
    return calls


async def _linked_pair(mgr, tmp_path, *, sessions=("sa", "sb")):
    """Primary pcA (alice@sa) linked with guest pcB (bob@sb), workers off."""
    mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
    mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
    _wire({"pcA": mm_a, "pcB": mm_b})
    for s in sessions:
        mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
    mm_a.create("m")
    await mm_a.join("m", sessions[0], handle="alice")
    # phase 6: the mirror is a side effect of the first member's join
    await mm_b.join(
        "m@pcA", sessions[1], handle="bob", code=mm_a.invite("m")["code"]
    )
    return mm_a, mm_b


# --------------------------------------------------------------------------- #
# A. link & roles
# --------------------------------------------------------------------------- #
def test_link_creates_mirror_with_snapshot(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        _wire({"pcA": mm_a, "pcB": mm_b})
        for s in ("sa", "sa2", "sb"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm_a.create("m")
        await mm_a.join("m", "sa", handle="alice")
        await mm_a.join("m", "sa2", handle="amy")
        await mm_a.send("m", "alice", "amy", "pre-link history")

        code = mm_a.invite("m")["code"]
        member = await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        assert member.handle == "bob"

        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        # roles: A owns, B mirrors
        assert mesh_a.primary == "" and mesh_b.primary == "pcA"
        # A2: snapshot — roster and full log arrived with the link
        # phase 7: the roster is fully absolute — even the authority's own
        # members carry its machine, so a handover cannot reassign them
        assert mesh_b.members["alice"].machine == "pcA"
        assert [m["body"] for m in mesh_b.messages] == ["pre-link history"]
        # asymmetric credential pair
        assert mesh_a.links["pcB"]["token_in"] == mesh_b.links["pcA"]["token_out"]
        assert mesh_a.links["pcB"]["token_out"] == mesh_b.links["pcA"]["token_in"]
        # the guest's cursor starts past the snapshot it was handed
        assert mesh_a.link_cursors["pcB"] == 1
        # A1: the mirror cannot mint invites, the primary can
        with pytest.raises(MeshError):
            mm_b.invite("m")
        # A4: the mirror refuses policy edits
        with pytest.raises(MeshError):
            mm_b.set_policy("m", {"heartbeat": {"enabled": True}})

        await mgr.shutdown_all()

    asyncio.run(run())


def test_join_address_refused_on_name_collision(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        _wire({"pcA": mm_a, "pcB": mm_b})
        mm_a.create("m")
        code = mm_a.invite("m")["code"]
        mm_b.create("m")  # a pre-existing local mesh of the same name
        with pytest.raises(MeshConflict):
            await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        # A3: never merged — B's mesh is still its own, unlinked
        assert mm_b.get("m").primary == ""
        await mgr.shutdown_all()

    asyncio.run(run())


def test_state_survives_reload_and_v1_state_is_dropped(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        sent = await mm_b.send("m", "bob", "alice", "over the hub")
        assert sent["queued"] is False
        await mm_a._flush_guest(mm_a.get("m"), "pcB")  # deterministic fanout

        # queue one send durably (D1 setup for the outbox reload check)
        _break_transport(mm_b)
        queued = await mm_b.send("m", "bob", "alice", "while offline")
        assert queued["queued"] is True

        # A5: both sides reload from disk
        mm_a2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_a2.load_all()
        mesh_a2 = mm_a2.get("m")
        assert mesh_a2.primary == ""
        assert "pcB" in mesh_a2.links
        assert mesh_a2.link_cursors["pcB"] >= 1
        assert mesh_a2.members["bob"].machine == "pcB"

        mm_b2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        mm_b2.load_all()
        mesh_b2 = mm_b2.get("m")
        assert mesh_b2.primary == "pcA"
        assert mesh_b2.links["pcA"]["token_out"] == mm_b.get("m").links["pcA"]["token_out"]
        assert [m["body"] for m in mesh_b2.messages] == ["over the hub"]
        assert [e["body"] for e in mesh_b2.outbox] == ["while offline"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_v1_federation_state_ignored_on_load(home, tmp_path):
    root = tmp_path / "mesh"
    d = root / "old"
    d.mkdir(parents=True)
    (d / "mesh.json").write_text(
        json.dumps(
            {
                "name": "old",
                "created_at": "2026-01-01T00:00:00+00:00",
                "members": {},
                "links": {"pcX": {"token_in": "a", "token_out": "b"}},
                "invites": {},
            }
        ),
        encoding="utf-8",
    )
    (d / "cursors.json").write_text(
        json.dumps({"members": {"w": 1}, "peers": {"pcX": 3}}), encoding="utf-8"
    )
    mm = MeshManager(_manager(), root=root)
    mm.load_all()
    mesh = mm.get("old")
    # A6: the v1 symmetric link and its peer cursor are gone; nothing crashes
    assert mesh.primary == "" and mesh.links == {} and mesh.peers == []
    assert mesh.cursors == {"w": 1}


# --------------------------------------------------------------------------- #
# B. membership
# --------------------------------------------------------------------------- #
def test_guest_join_is_authoritative_at_primary(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        # B1: the primary recorded bob as pcB's member; the mirror shows him
        assert mesh_a.members["bob"].machine == "pcB"
        assert mesh_b.members["bob"].machine == "pcB"

        # B1: central uniqueness — a second 'bob' from anywhere is refused
        mgr.create(SessionDef(name="sx", harness="py", cwd=str(tmp_path)))
        with pytest.raises(MeshError):
            await mm_b.join("m", "sx", handle="bob")
        with pytest.raises(MeshError):
            await mm_b.join("m", "sx", handle="alice")  # taken by the primary

        # B3: guest leave forwards to the primary
        left = await mm_b.leave("m", "bob")
        assert left.handle == "bob"
        assert "bob" not in mesh_a.members and "bob" not in mesh_b.members
        # a guest cannot remove somebody else's member
        with pytest.raises(MeshError):
            await mm_b.leave("m", "alice")

        # B3: a primary-side kick reaches the guest on the next sync
        await mm_b.join("m", "sx", handle="bob2")
        assert "bob2" in mesh_a.members
        await mm_a.leave("m", "bob2")
        await mm_a._flush_guest(mesh_a, "pcB")
        assert "bob2" not in mesh_b.members

        await mgr.shutdown_all()

    asyncio.run(run())


def test_guest_join_fails_fast_when_primary_unreachable(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mgr.create(SessionDef(name="sx", harness="py", cwd=str(tmp_path)))
        _break_transport(mm_b)
        with pytest.raises(MeshError):
            await mm_b.join("m", "sx", handle="carol")
        # B2: nothing was recorded anywhere
        assert "carol" not in mm_a.get("m").members
        assert "carol" not in mm_b.get("m").members
        await mgr.shutdown_all()

    asyncio.run(run())


def test_join_briefing_lands_in_guest_terminal(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        # B4: the joining guest member is briefed in its own terminal
        await _wait_screen(mgr.get("sb"), "join briefing")
        await _wait_screen(mgr.get("sb"), "you: bob")
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. messaging
# --------------------------------------------------------------------------- #
def test_guest_send_sequenced_by_primary_and_delivered(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mm_a.start()
        mm_b.start()
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        sent = await mm_b.send("m", "bob", "alice", "hello via hub", type="ack")
        # C2: the primary sequenced it and reported authoritative recipients
        assert sent["queued"] is False
        assert sent["recipients"] == ["alice"]
        assert sent["expects_reply"] is False
        assert mesh_a.messages[-1]["id"] == sent["id"]

        # the message lands in alice's terminal on the primary
        await _wait_screen(mgr.get("sa"), "hello via hub")

        # identical history on both daemons (the fanout worker syncs B)
        await _wait_for(
            lambda: [m["id"] for m in mesh_b.messages]
            == [m["id"] for m in mesh_a.messages],
            "mirror log to converge",
        )

        # C1 sanity: an owned-mesh send still carries v1 result fields
        local = await mm_a.send("m", "alice", "bob", "hi bob")
        assert local["queued"] is False
        assert local["remote"] == ["bob"]
        assert local["expects_reply"] is True
        await _wait_screen(mgr.get("sb"), "hi bob")

        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_guest_to_guest_via_hub_three_daemons(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        for s in ("sa", "sb", "sc"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm_a.create("m")
        await mm_a.join("m", "sa", handle="alice")
        await mm_b.join("m@pcA", "sb", handle="bob",
                        code=mm_a.invite("m")["code"])
        await mm_c.join("m@pcA", "sc", handle="carol",
                        code=mm_a.invite("m")["code"])

        # every daemon sees the full roster (via the hub, no B-C link)
        for mm in (mm_a, mm_b, mm_c):
            await mm_a._flush_guest(mm_a.get("m"), "pcB")
            await mm_a._flush_guest(mm_a.get("m"), "pcC")
            assert set(mm.get("m").members) == {"alice", "bob", "carol"}

        sent = await mm_b.send("m", "bob", "carol", "b to c")
        assert sent["recipients"] == ["carol"]
        await mm_a._flush_guest(mm_a.get("m"), "pcC")
        mesh_c = mm_c.get("m")
        # C3: carol's daemon has the message, pending for her
        assert [m["body"] for m in mesh_c.pending("carol")] == ["b to c"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_guest_local_dm_goes_through_primary(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mgr.create(SessionDef(name="sb2", harness="py", cwd=str(tmp_path)))
        await mm_b.join("m", "sb2", handle="bea")
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        # primary reachable: the DM is sequenced by A even though both live on B
        sent = await mm_b.send("m", "bob", "bea", "same-daemon dm")
        assert sent["queued"] is False
        assert mesh_a.messages[-1]["body"] == "same-daemon dm"
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_b.messages[-1]["id"] == sent["id"]
        assert [m["body"] for m in mesh_b.pending("bea")] == ["same-daemon dm"]

        # C4: primary down — the local DM waits in the outbox, nothing local
        _break_transport(mm_b)
        queued = await mm_b.send("m", "bob", "bea", "dm while down")
        assert queued["queued"] is True
        assert all(m["body"] != "dm while down" for m in mesh_b.messages)

        # reconnect: outbox drains through A; logs are identical again
        _wire({"pcA": mm_a, "pcB": mm_b})
        mesh_b.peer_status.clear()
        await mm_b._flush_upstream(mesh_b)
        await mm_a._flush_guest(mesh_a, "pcB")
        assert [m["id"] for m in mesh_b.messages] == [
            m["id"] for m in mesh_a.messages
        ]
        assert mesh_b.messages[-1]["body"] == "dm while down"
        assert mesh_b.outbox == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_sections_reply_to_and_slicing_across_hop(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        mgr.create(SessionDef(name="sb2", harness="py", cwd=str(tmp_path)))
        await mm_b.join("m", "sb2", handle="bea")

        sent = await mm_b.send(
            "m", "bob", ["alice", "bea"], "shared brief",
            sections={
                "alice": {"text": "alice does X", "type": "ask"},
                "bea": {"text": "bea just fyi", "type": "fyi"},
            },
            reply_to="msg-000000000000",
        )
        assert sent["queued"] is False and sent["batched"] is True

        # C5: the primary's log entry carries the batch fields intact
        got = mesh_a.messages[-1]
        assert got["shared"] == "shared brief"
        assert got["sections"]["alice"]["type"] == "ask"
        assert got["reply_to"] == "msg-000000000000"

        # slicing happens per delivering daemon: alice's block on A, bea's on B
        block_alice = format_delivery("m", "alice", [got])
        assert "alice does X" in block_alice and "bea just fyi" not in block_alice
        await mm_a._flush_guest(mesh_a, "pcB")
        got_b = mesh_b.messages[-1]
        block_bea = format_delivery("m", "bea", [got_b])
        assert "bea just fyi" in block_bea and "alice does X" not in block_bea
        assert "needs_reply: false" in block_bea  # her slice is fyi

        await mgr.shutdown_all()

    asyncio.run(run())


def test_dedupe_and_resync(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        sent = await mm_b.send("m", "bob", "alice", "once only")
        n = len(mesh_a.messages)
        # C6: an upstream retry with the same id is acknowledged, not re-run
        dup = mm_a.peer_send_accept(
            "m", "pcB", mesh_a.links["pcB"]["token_in"],
            {"id": sent["id"], "from": "bob", "to": "alice", "body": "once only"},
        )
        assert dup.get("duplicate") is True
        assert len(mesh_a.messages) == n

        # C7: a stale primary-side cursor replays; the guest asks for resync
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_b.messages[-1]["id"] == sent["id"]
        before = [m["id"] for m in mesh_b.messages]
        mesh_a.link_cursors["pcB"] = 0  # simulate a lost cursor
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_a.link_cursors["pcB"] == len(mesh_a.messages)
        assert [m["id"] for m in mesh_b.messages] == before  # no duplicates

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. failure
# --------------------------------------------------------------------------- #
def test_outbox_queues_durably_and_preserves_order(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        _break_transport(mm_b)
        q1 = await mm_b.send("m", "bob", "alice", "first")
        q2 = await mm_b.send("m", "bob", "alice", "second")
        assert q1["queued"] and q2["queued"]
        assert [e["body"] for e in mesh_b.outbox] == ["first", "second"]
        # D1: the outbox is on disk (survives a daemon restart)
        outbox_path = tmp_path / "meshB" / "m" / "outbox.jsonl"
        assert outbox_path.is_file()
        # D3: the mirror is still readable offline
        assert mm_b.history("m") is not None
        assert set(mesh_b.members) == {"alice", "bob"}

        # upstream flush backs off while down
        mesh_b.peer_status.clear()
        await mm_b._flush_upstream(mesh_b)
        assert mesh_b.peer_status["pcA"]["ok"] is False
        assert mesh_b.outbox != []

        # reconnect: drains in order; a new send goes behind the queue
        _wire({"pcA": mm_a, "pcB": mm_b})
        mesh_b.peer_status.clear()
        q3 = await mm_b.send("m", "bob", "alice", "third")
        assert q3["queued"] is True  # behind the queued backlog, not skipping it
        await mm_b._flush_upstream(mesh_b)
        assert mesh_b.outbox == []
        assert [m["body"] for m in mesh_a.messages][-3:] == [
            "first", "second", "third"
        ]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_primary_fanout_backoff_and_drain(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        calls = _break_transport(mm_a)
        mm_a.relay_connected = lambda: False
        sent = await mm_a.send("m", "alice", "bob", "for bob")
        # D2: the remote recipient is reported queued
        assert sent["queued_remote"] == ["bob"]
        await mm_a._flush_guest(mesh_a, "pcB")
        status = mesh_a.peer_status["pcB"]
        assert status["ok"] is False and status["backoff"] > 0
        n = len(calls)
        await mm_a._flush_guest(mesh_a, "pcB")  # inside backoff: no dial
        assert len(calls) == n

        _wire({"pcA": mm_a, "pcB": mm_b})
        mesh_a.peer_status["pcB"]["retry_at"] = 0.0
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_a.peer_status["pcB"]["ok"] is True
        assert [m["body"] for m in mesh_b.pending("bob")] == ["for bob"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# E. policy
# --------------------------------------------------------------------------- #
def test_guest_ack_piggybacks_activity(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a = mm_a.get("m")
        mm_a.set_policy("m", {"heartbeat": {"enabled": True}})
        mm_a.report_interval = 0.0  # report on every sync

        await _wait_for(
            lambda: mgr.get("sb").status() == "idle", "bob's session to go idle"
        )
        await mm_a._flush_guest(mesh_a, "pcB")
        # E2: the primary now knows bob's observed state
        act = mesh_a.remote_activity.get("bob")
        assert act is not None
        assert act["idle"] is True
        assert act["caught_up"] is True
        assert act["unanswered"] is False

        await mgr.shutdown_all()

    asyncio.run(run())


def test_remote_heartbeat_decided_by_primary_injected_by_guest(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        mm_a.set_policy("m", {"heartbeat": {"enabled": True, "interval": 1.0}})
        mm_a.report_interval = 0.0

        # bob owes an answer (his daemon's delivery bookkeeping says so)
        now = time.monotonic()
        mesh_b.activity["bob"] = {"anchor": now, "last_asked": now, "last_sent": 0.0}
        await _wait_for(
            lambda: mgr.get("sb").status() == "idle", "bob's session to go idle"
        )
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_a.remote_activity["bob"]["unanswered"] is True

        # E3: the primary's tick decides the nudge for the remote member…
        mesh_a.activity.setdefault("bob", {"anchor": 0.0})["hb_next"] = 0.0
        await mesh_policy.tick(mm_a, mesh_a)
        assert mesh_a.pending_nudges["pcB"][0]["kind"] == "heartbeat"
        assert mesh_a.pending_nudges["pcB"][0]["handle"] == "bob"

        # …and the guest daemon injects it into bob's terminal
        await mm_a._flush_guest(mesh_a, "pcB")
        # Wait on the nudge's LAST line, not its `kind:` header. The join
        # briefing is idle-gated and lands around now too, and the echo child
        # doubles every line, so the pair overflows a 30-row screen and the
        # header scrolls off; `note: heartbeat:` identifies the same block
        # just as precisely and survives.
        await _wait_screen(mgr.get("sb"), "note: heartbeat:")
        assert mesh_a.pending_nudges.get("pcB", []) == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_operator_nudge_survives_a_busy_remote_member(home, tmp_path):
    """A hand-fired nudge is not subject to the guest's idleness re-check.

    The engine's nudges may be dropped by a busy member — the tick that
    decided on one re-fires it a minute later, so nothing is lost. Nothing
    re-fires an operator's, and the local path never checks idleness at all,
    so without `force` the same button would mean two different things
    depending on which machine happens to host the member.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        session = mgr.get("sb")
        await _wait_screen(session, "READY")

        # the primary queues it for the daemon that owns bob's terminal
        result = await mm_a.nudge("m", "bob", "answer the schema question")
        assert result["queued"] is True
        queued = mesh_a.pending_nudges["pcB"][0]
        assert queued["handle"] == "bob" and queued["force"] is True

        # bob is mid-turn when it lands
        session.status = lambda: "busy"
        await mm_b._apply_nudge(mesh_b, {**queued, "force": False})
        assert "note: answer the schema question" not in "\n".join(session.capture())
        await mm_b._apply_nudge(mesh_b, queued)
        await _wait_screen(session, "note: answer the schema question")

        await mgr.shutdown_all()

    asyncio.run(run())


def test_stall_warning_reaches_remote_leader(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        _wire({"pcA": mm_a, "pcB": mm_b})
        for s in ("sw", "sl"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm_a.create("m")
        await mm_a.join("m", "sw", handle="worker_1")
        await mm_b.join("m@pcA", "sl", handle="leader",
                        code=mm_a.invite("m")["code"])
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        mm_a.set_policy("m", {"stall_warn": {"enabled": True, "warn_secs": 1.0}})

        await _wait_for(
            lambda: mgr.get("sw").status() == "idle", "worker session idle"
        )
        # make the worker look long-stalled
        mesh_a.activity["worker_1"] = {"anchor": time.monotonic() - 3600}
        await mesh_policy.tick(mm_a, mesh_a)
        # E4: an ordinary fyi from 'policy' targeted at the leader…
        stall = mesh_a.messages[-1]
        assert stall["from"] == "policy" and stall["type"] == "fyi"
        assert stall["to"] == ["leader"]
        # …which federates to the remote leader like any message
        await mm_a._flush_guest(mesh_a, "pcB")
        assert mesh_b.messages[-1]["body"].startswith("stall: worker_1")
        assert [m["body"] for m in mesh_b.pending("leader")] == [
            mesh_b.messages[-1]["body"]
        ]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_tick_is_noop_on_mirror(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_b = mm_b.get("m")
        # even with an (accidentally) enabled synced policy, the mirror's
        # tick must do nothing — the engine lives on the primary
        mesh_b.policy["heartbeat"]["enabled"] = True
        mesh_b.activity["bob"] = {
            "anchor": 0.0, "last_asked": 1.0, "last_sent": 0.0, "hb_next": 0.0,
        }
        before = _screen_text(mgr.get("sb"))
        await mesh_policy.tick(mm_b, mesh_b)
        await asyncio.sleep(0.3)
        assert "kind: heartbeat" not in _screen_text(mgr.get("sb"))
        assert _screen_text(mgr.get("sb")) == before

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #
def test_mesh_info_reports_roles_and_queues(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")

        # phase 7: "peers" is the whole rank list, ourselves included, with
        # rank 0 holding the authority
        info_a = mm_a.mesh_info(mesh_a)
        assert info_a["primary"] is None  # owned
        assert [p["machine"] for p in info_a["peers"]] == ["pcA", "pcB"]
        assert info_a["peers"][0]["role"] == "authority"
        assert info_a["peers"][0]["self"] is True
        assert info_a["peers"][1]["role"] == "peer"

        info_b = mm_b.mesh_info(mesh_b)
        assert info_b["primary"] == "pcA"
        assert [p["machine"] for p in info_b["peers"]] == ["pcA", "pcB"]
        assert info_b["peers"][0]["role"] == "authority"
        assert info_b["peers"][1]["self"] is True

        # outbox depth surfaces as the queue toward the authority
        _break_transport(mm_b)
        await mm_b.send("m", "bob", "alice", "stuck")
        info_b = mm_b.mesh_info(mm_b.get("m"))
        assert info_b["peers"][0]["queued"] == 1

        await mgr.shutdown_all()

    asyncio.run(run())


def test_authority_member_joined_after_federation_buckets_on_the_authority(
    home, tmp_path
):
    """A member enrolled on the authority AFTER the mesh federated belongs to
    the authority everywhere — including on a mirror's own view of it.

    ``_stamp_own_members`` runs when a mesh federates, at a handover and on
    migration; a join is none of those, so such a member used to keep the
    blank machine that means "the authority's own". Readers that resolved
    blank as *their own* name then drew it inside the reading daemon's
    cluster and left the authority's empty — the roster contradicting its own
    ``reachability``, which had it right all along.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        # the mesh is federated; NOW the authority enrols one of its own
        mgr.create(SessionDef(name="sa2", harness="py", cwd=str(tmp_path)))
        await mm_a.join("m", "sa2", handle="amy")
        await asyncio.sleep(0.3)  # roster fanout to the mirror

        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        # absolute from birth, on the authority and on the mirror's copy
        assert mesh_a.members["amy"].machine == "pcA"
        assert mesh_b.members["amy"].machine == "pcA"
        # ...and it is the authority's, whichever daemon is asked
        assert mm_a.is_local_member(mesh_a, mesh_a.members["amy"]) is True
        assert mm_b.is_local_member(mesh_b, mesh_b.members["amy"]) is False

        # the bucket the topology draws: 'amy' hangs off pcA on BOTH sides,
        # and the mirror never claims her as its own
        for mm, mesh in ((mm_a, mesh_a), (mm_b, mesh_b)):
            peers = {p["machine"]: p for p in mm.mesh_info(mesh)["peers"]}
            assert "amy" in peers["pcA"]["members"]
            assert "amy" not in peers["pcB"]["members"]
            assert peers["pcB"]["members"] == ["bob"]

        # the owed ledger names the same machine rather than the reader's
        assert {
            r["handle"]: r["machine"] for r in mm_b.owed_report(mesh_b)["members"]
        }["amy"] == "pcA"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_blank_machine_on_a_mirror_reads_as_the_authoritys(home, tmp_path):
    """A roster blank that predates the stamping rule still reads correctly.

    Meshes federated before that fix have members already on disk with a
    blank machine, and no reload re-stamps them (``_stamp_own_members`` is
    reached through migration, which such a mesh has already done). So the
    readers must resolve blank as the AUTHORITY's, not their own.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_b = mm_b.get("m")
        # the legacy shape: the authority's member, left blank
        mesh_b.members["alice"].machine = ""

        peers = {p["machine"]: p for p in mm_b.mesh_info(mesh_b)["peers"]}
        assert peers["pcA"]["members"] == ["alice"]
        assert peers["pcB"]["members"] == ["bob"]
        assert mm_b.is_local_member(mesh_b, mesh_b.members["alice"]) is False

        await mgr.shutdown_all()

    asyncio.run(run())


def test_unfederated_mesh_keeps_its_members_blank_and_local(home, tmp_path):
    """The never-federated case the blank convention exists for is untouched:
    no peer list, so no stamping, and the single cluster is still ours."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "solo")
        _wire({"pcA": mm})
        mgr.create(SessionDef(name="sa", harness="py", cwd=str(tmp_path)))
        mm.create("m")
        await mm.join("m", "sa", handle="alice")

        mesh = mm.get("m")
        assert mesh.peers == [] and mesh.members["alice"].machine == ""
        assert mm.is_local_member(mesh, mesh.members["alice"]) is True
        assert mm.mesh_info(mesh)["peers"] == []

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# F. self-identification ("which member am I?")
# --------------------------------------------------------------------------- #
def test_mesh_info_states_locality_per_member(home, tmp_path):
    """F1 the roster says whether a row is ours instead of implying it.

    ``mesh_info`` computed locality already — to pick pending/owed/parent —
    and dropped it on the way out, so every reader re-derived it from
    ``machine``. That is the one derivation ``is_local_member`` exists to
    stop anyone writing, and both callers who wrote it anyway got the mirror
    case backwards. ``owed_report`` has always said it outright.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)

        # each side owns exactly one row, and says so in the payload
        for mm, mine, theirs in ((mm_a, "alice", "bob"), (mm_b, "bob", "alice")):
            mesh = mm.get("m")
            rows = {r["handle"]: r for r in mm.mesh_info(mesh)["members"]}
            assert rows[mine]["local"] is True
            assert rows[theirs]["local"] is False
            # the same answer owed_report gives, from the same rule
            owed = {r["handle"]: r for r in mm.owed_report(mesh)["members"]}
            assert owed[mine]["local"] == rows[mine]["local"]
            assert owed[theirs]["local"] == rows[theirs]["local"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_mirrors_own_member_identifies_itself(home, tmp_path):
    """F2 a guest-daemon session resolves to its handle.

    The blank-machine test could never find it: on a mirror our own members
    are stamped with our machine, so nothing is blank and the caller was told
    it was not a member of a mesh it was demonstrably in.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_b = mm_b.get("m")

        # the shape that defeated the old predicate
        assert mesh_b.members["bob"].machine == "pcB"
        assert mm_b.member_for_session(mesh_b, "sb").handle == "bob"
        assert mm_b.mesh_info(mesh_b, session="sb")["you"] == "bob"

        # the authority's member is not ours to answer as
        assert mm_b.member_for_session(mesh_b, "sa") is None
        assert mm_b.mesh_info(mesh_b, session="sa")["you"] is None

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_stamped_authority_member_still_identifies_itself(home, tmp_path):
    """F3 stamping the authority's own rows does not unseat them.

    On the authority the blank test worked by accident, and only until a
    stamping pass ran — federation, a handover or a migration — after which
    the same session stopped recognising itself. Which sessions could run
    ``stance`` depended on who had joined before which event.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, _ = await _linked_pair(mgr, tmp_path)
        mesh_a = mm_a.get("m")

        mesh_a.members["alice"].machine = ""  # the pre-stamp shape
        assert mm_a.mesh_info(mesh_a, session="sa")["you"] == "alice"
        mm_a._stamp_own_members(mesh_a)  # federation / handover / migration
        assert mesh_a.members["alice"].machine == "pcA"
        assert mm_a.mesh_info(mesh_a, session="sa")["you"] == "alice"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_blank_row_is_never_answered_as_ours_on_a_mirror(home, tmp_path):
    """F4 a session name colliding with the authority's does not steal it.

    Session names are per-daemon and sequential, so two daemons of the same
    user collide by default rather than by accident. Resolving blank as
    "ours" handed such a caller the authority's handle — which ``leave``
    would then have aimed a DELETE at.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_b = mm_b.get("m")
        mesh_b.members["alice"].machine = ""  # legacy blank: the authority's
        # our own session bears the same name as the authority's member's
        mesh_b.members["bob"].session = "sa"

        assert mm_b.member_for_session(mesh_b, "sa").handle == "bob"
        assert mm_b.mesh_info(mesh_b, session="sa")["you"] == "bob"

        await mgr.shutdown_all()

    asyncio.run(run())


def test_you_is_absent_until_a_session_asks(home, tmp_path):
    """F5 'nobody asked' and 'you are nobody' stay distinguishable.

    The dashboard polls this document as no session at all; answering that
    with some member would be worse than answering with nothing.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path)
        mesh_b = mm_b.get("m")

        assert mm_b.mesh_info(mesh_b)["you"] is None
        assert mm_b.mesh_info(mesh_b, session="")["you"] is None
        assert mm_b.mesh_info(mesh_b, session="nosuch")["you"] is None
        assert mm_b.mesh_info(mesh_b, session="sb")["you"] == "bob"

        await mgr.shutdown_all()

    asyncio.run(run())
