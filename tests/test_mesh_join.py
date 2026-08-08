"""Membership-first joining (phase 6): scenario-derived unit tests.

Scenario matrix, derived from docs/mesh-design.md "Membership-first joining":

J. Establishment — join is the only verb
   J1 `join dev@pca --code` (pre-approval ticket): ONE call creates the
      mirror + the member + the briefing; tickets are single-use
   J2 a codeless join pends on the primary; nothing is created on the
      guest yet (only a durable outgoing record)
   J3 approve → the grant calls back to the guest: mirror + member appear,
      the outgoing record clears, the request leaves the queue
   J4 deny → the guest's outgoing record clears; nothing is created
   J5 approve while the guest is unreachable → the grant retries and
      lands once the transport returns
   J6 an address that collides with an existing local mesh is refused
   J7 later members from an already-linked machine join with no ceremony
   J8 a machine whose mirror was lost re-requests and is auto-granted
      (already trusted), reclaiming its existing member
   J9 the outgoing record survives a daemon reload; the grant still lands

I. Invite tickets
   I1 tickets expire (TTL)
   I2 tickets can be listed and revoked

G. Guest lifecycle
   G1 `revoke` removes the guest and its members and drops the mirror
   G2 deleting the mesh on the primary drops guests' mirrors

S. Authority hardening
   S1 the primary cannot send AS a guest member without `external`
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import (
    Member,
    MeshConflict,
    MeshError,
    MeshManager,
    PeerUnreachable,
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
            body.get("role") or "",
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
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _wire(machines: dict) -> None:
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


async def _primary_with_alice(mgr, tmp_path):
    """Primary pcA owning mesh 'm' with alice@sa and some history."""
    mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
    mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
    _wire({"pcA": mm_a, "pcB": mm_b})
    for s in ("sa", "sb"):
        mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
    mm_a.create("m")
    await mm_a.join("m", "sa", handle="alice")
    return mm_a, mm_b


# --------------------------------------------------------------------------- #
# J1 + J7: ticket join is one call; later members need no ceremony
# --------------------------------------------------------------------------- #
def test_coded_join_creates_mirror_member_and_briefing(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mgr.create(SessionDef(name="sa2", harness="py", cwd=str(tmp_path)))
        await mm_a.join("m", "sa2", handle="amy")
        await mm_a.send("m", "alice", "amy", "pre-join history")
        code = mm_a.invite("m")["code"]

        member = await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        assert isinstance(member, Member) and member.handle == "bob"

        # J1: one call produced the whole establishment
        mesh_a, mesh_b = mm_a.get("m"), mm_b.get("m")
        assert mesh_b.primary == "pcA"
        assert set(mesh_b.members) == {"alice", "amy", "bob"}
        assert [m["body"] for m in mesh_b.messages] == ["pre-join history"]
        assert mesh_a.members["bob"].machine == "pcB"
        assert mesh_a.guests["pcB"]["token_in"] == mesh_b.link["token_out"]
        assert mesh_a.guests["pcB"]["token_out"] == mesh_b.link["token_in"]
        assert mm_a.request_list("m") == []  # nothing pended
        # the joining member is briefed in its own terminal
        await _wait_screen(mgr.get("sb"), "join briefing")

        # tickets are single-use
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        mgr.create(SessionDef(name="sc", harness="py", cwd=str(tmp_path)))
        with pytest.raises(MeshError):
            await mm_c.join("m@pcA", "sc", handle="carol", code=code)

        # J7: a second member from the linked machine — plain join, by name
        # or by address, no code, no approval
        mgr.create(SessionDef(name="sb2", harness="py", cwd=str(tmp_path)))
        bee = await mm_b.join("m@pcA", "sb2", handle="bee")
        assert isinstance(bee, Member)
        assert mesh_a.members["bee"].machine == "pcB"

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# J2/J3/J4: codeless joins pend for approval
# --------------------------------------------------------------------------- #
def test_codeless_join_pends_then_approve_and_deny(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)

        res = await mm_b.join("m@pcA", "sb", handle="bob")
        # J2: pended — no mirror yet, a durable outgoing record instead
        assert isinstance(res, dict) and res["pending"] is True
        rid = res["request_id"]
        assert [x.name for x in mm_b.list()] == []
        outgoing = mm_b.outgoing_list()
        assert len(outgoing) == 1 and outgoing[0]["handle"] == "bob"

        reqs = mm_a.request_list("m")
        assert len(reqs) == 1
        assert reqs[0]["machine"] == "pcB" and reqs[0]["handle"] == "bob"
        assert reqs[0]["id"] == rid

        # J3: approval delivers the grant to the guest
        await mm_a.approve_request("m", rid)
        mesh_b = mm_b.get("m")
        assert mesh_b.primary == "pcA"
        assert mesh_b.members["bob"].machine == "pcB"
        assert mm_a.get("m").members["bob"].machine == "pcB"
        assert mm_a.request_list("m") == []
        assert mm_b.outgoing_list() == []
        await _wait_screen(mgr.get("sb"), "join briefing")

        # J4: a denied request evaporates on both sides
        mm_c = MeshManager(mgr, settle=0.05, root=tmp_path / "meshC")
        _wire({"pcA": mm_a, "pcB": mm_b, "pcC": mm_c})
        mgr.create(SessionDef(name="sc", harness="py", cwd=str(tmp_path)))
        res_c = await mm_c.join("m@pcA", "sc", handle="carol")
        await mm_a.deny_request("m", res_c["request_id"])
        assert mm_a.request_list("m") == []
        assert mm_c.outgoing_list() == []
        assert [x.name for x in mm_c.list()] == []
        assert "carol" not in mm_a.get("m").members

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# J5: grant survives an unreachable guest
# --------------------------------------------------------------------------- #
def test_grant_retries_until_guest_reachable(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        res = await mm_b.join("m@pcA", "sb", handle="bob")
        rid = res["request_id"]

        _break_transport(mm_a)  # the approval callback cannot reach pcB
        await mm_a.approve_request("m", rid)
        mesh_a = mm_a.get("m")
        # membership is decided; only the delivery of the grant is pending
        assert mesh_a.members["bob"].machine == "pcB"
        assert rid in mesh_a.pending_grants
        assert [x.name for x in mm_b.list()] == []  # guest still has nothing

        # transport returns; the worker-side retry lands the grant
        _wire({"pcA": mm_a, "pcB": mm_b})
        mesh_a.peer_status.clear()
        await mm_a._flush_grants(mesh_a)
        assert mesh_a.pending_grants == {}
        assert mm_b.get("m").members["bob"].machine == "pcB"
        assert mm_b.outgoing_list() == []

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# J6: address collisions are refused
# --------------------------------------------------------------------------- #
def test_join_address_collisions(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        mgr.create(SessionDef(name="sx", harness="py", cwd=str(tmp_path)))

        # a local mesh of the same name blocks an establishment join
        mm_b.create("m")
        with pytest.raises(MeshConflict):
            await mm_b.join("m@pcA", "sb", handle="bob")
        mm_b.delete("m")

        # linked to pcA, then addressed to a different primary → conflict
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        with pytest.raises(MeshConflict):
            await mm_b.join("m@pcX", "sx", handle="bee")

        # joining the primary's own address locally still works
        member = await mm_a.join("m@pcA", "sx", handle="amy")
        assert isinstance(member, Member) and member.machine == ""

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# J8: a trusted machine that lost its mirror is auto-granted + reclaims
# --------------------------------------------------------------------------- #
def test_relink_auto_grant_reclaims_member(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        old_token = mm_a.get("m").guests["pcB"]["token_in"]

        # the guest loses its mirror (local mishap)
        mm_b.delete("m")
        assert [x.name for x in mm_b.list()] == []

        # re-request without a code: the machine is already trusted →
        # auto-grant with fresh credentials, reclaiming the same member
        member = await mm_b.join("m@pcA", "sb", handle="bob")
        assert isinstance(member, Member) and member.handle == "bob"
        assert mm_b.get("m").primary == "pcA"
        assert mm_a.request_list("m") == []
        assert mm_a.get("m").guests["pcB"]["token_in"] != old_token
        # no duplicate member appeared
        assert sum(
            1 for x in mm_a.get("m").members.values() if x.machine == "pcB"
        ) == 1

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# J9: the outgoing request survives a guest daemon reload
# --------------------------------------------------------------------------- #
def test_outgoing_request_survives_reload(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        res = await mm_b.join("m@pcA", "sb", handle="bob")
        rid = res["request_id"]

        mm_b2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
        mm_b2.load_all()
        assert [o["request_id"] for o in mm_b2.outgoing_list()] == [rid]

        # the grant lands on the reloaded daemon
        _wire({"pcA": mm_a, "pcB": mm_b2})
        await mm_a.approve_request("m", rid)
        assert mm_b2.get("m").members["bob"].machine == "pcB"
        assert mm_b2.outgoing_list() == []

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# I1/I2: invite tickets — TTL, list, revoke
# --------------------------------------------------------------------------- #
def test_invite_tickets_expire_list_revoke(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)

        code = mm_a.invite("m")["code"]
        tickets = mm_a.invite_list("m")
        assert len(tickets) == 1
        assert tickets[0]["prefix"] and tickets[0]["created_at"]

        # I1: an expired ticket is refused (and cleaned up)
        mm_a.invite_ttl = 0.0
        with pytest.raises(MeshError):
            await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        assert mm_a.invite_list("m") == []
        mm_a.invite_ttl = 86400.0

        # I2: revocation by prefix
        code2 = mm_a.invite("m")["code"]
        prefix = mm_a.invite_list("m")[0]["prefix"]
        mm_a.invite_revoke("m", prefix)
        assert mm_a.invite_list("m") == []
        with pytest.raises(MeshError):
            await mm_b.join("m@pcA", "sb", handle="bob", code=code2)

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# G1/G2: guest lifecycle — revoke and delete propagate
# --------------------------------------------------------------------------- #
def test_revoke_guest_removes_members_and_mirror(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)

        await mm_a.revoke_guest("m", "pcB")
        mesh_a = mm_a.get("m")
        assert mesh_a.guests == {}
        assert "bob" not in mesh_a.members
        # G1: the guest was told; its mirror is gone
        assert [x.name for x in mm_b.list()] == []
        # a revoked machine cannot resume with its old credentials
        with pytest.raises(MeshError):
            mm_a.peer_send_accept(
                "m", "pcB", "whatever",
                {"id": "msg-x", "from": "bob", "to": "alice", "body": "hi"},
            )

        await mgr.shutdown_all()

    asyncio.run(run())


def test_delete_on_primary_drops_guest_mirrors(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)

        mm_a.delete("m")
        # G2: the unlink notification is best-effort/async — wait for it
        await _wait_for(
            lambda: [x.name for x in mm_b.list()] == [],
            "the guest mirror to be dropped",
        )

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# S1: the primary cannot impersonate a guest member
# --------------------------------------------------------------------------- #
def test_primary_cannot_send_as_guest_member(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)

        with pytest.raises(MeshError):
            await mm_a.send("m", "bob", "alice", "not really bob")
        # the honest paths still work
        ok = await mm_a.send("m", "operator", "alice", "hi", external=True)
        assert ok["from"] == "operator"
        ok2 = await mm_b.send("m", "bob", "alice", "genuinely bob")
        assert ok2["from"] == "bob" and ok2["queued"] is False

        await mgr.shutdown_all()

    asyncio.run(run())
