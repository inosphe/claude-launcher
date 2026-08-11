"""Member lineage: who spawned whom, expressed in handles.

The session tree (``SessionDef.parent``) is the only source. These tests are
about carrying it to the layer that draws it — ``mesh_info``, and the sync
that lets a daemon see a tree rooted on somebody else's machine.

Scenario matrix, derived from docs/mesh-design.md "Hierarchy in the diagram":

A. Local
   A1 a spawned member reports its parent's handle; a root reports None
   A2 a lineage step through a session that never joined collapses to the
      nearest *enrolled* ancestor, rather than breaking the chain
   A3 a parent session that no longer exists reads as a root
   A4 a parent that left the mesh reads as a root, so no edge is left
      pointing at somebody who is gone

B. Federation
   B1 a guest's sync ack carries the lineage of the members it hosts, and
      the authority stores it
   B2 the authority relays it on, so a guest sees another guest's tree
   B3 lineage leaves with the member, so a rejoining handle does not
      inherit its predecessor's parent
"""

from __future__ import annotations

import asyncio
import sys

from claude_launcher import store
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager, PeerUnreachable

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
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)


def _parents(mm: MeshManager, name: str = "m") -> dict:
    """The lineage as the dashboard reads it."""
    return {
        m["handle"]: m["parent"] for m in mm.mesh_info(mm.get(name))["members"]
    }


# --------------------------------------------------------------------------- #
# in-process wiring: the /peer/* HTTP layer, minus the HTTP. Mirrors what
# api.py forwards, lineage included — a dispatcher that quietly dropped it
# would make the relay tests below pass for the wrong reason.
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
            peers=body.get("peers"), epoch=body.get("epoch"),
            links=body.get("links"), edges=body.get("edges"),
            member_edges=body.get("member_edges"),
            roles=body.get("roles"), lineage=body.get("lineage"),
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _wire(machines: dict) -> None:
    async def call(machine, path, body):
        return _dispatch_peer(machines[machine], path, body)

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


async def _team(mm: MeshManager, mgr: SessionManager, tree):
    """A mesh of real sessions. ``tree`` is (session, parent, handle|None) —
    a None handle means the session is created but never enrolled."""
    mm.create("m")
    for session, parent, handle in tree:
        mgr.create(SessionDef(name=session, harness="py", parent=parent))
        if handle:
            await mm.join("m", session, handle=handle)
    return mm.get("m")


# --------------------------------------------------------------------------- #
# A. local
# --------------------------------------------------------------------------- #
def test_a_spawned_member_reports_its_parent(home, tmp_path):
    """A1."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        await _team(mm, mgr, [
            ("s-lead", None, "lead"),
            ("s-api", "s-lead", "w-api"),
            ("s-db", "s-api", "w-db"),
        ])
        assert _parents(mm) == {"lead": None, "w-api": "lead", "w-db": "w-api"}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_lineage_collapses_through_a_session_that_never_joined(home, tmp_path):
    """A2: the tree is of sessions, the roster is of members, and the two do
    not have to line up. A worker whose lead never enrolled hangs off whoever
    above it did — breaking the chain instead would scatter a team into
    unrelated roots."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        await _team(mm, mgr, [
            ("s-lead", None, "lead"),
            ("s-mid", "s-lead", None),      # exists, never joined the mesh
            ("s-worker", "s-mid", "worker"),
        ])
        assert _parents(mm) == {"lead": None, "worker": "lead"}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_parent_session_that_is_gone_reads_as_a_root(home, tmp_path):
    """A3: the same rule the session tree uses — a dangling parent makes its
    child a root, rather than an edge pointing at nothing."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mm.create("m")
        mgr.create(SessionDef(name="s-orphan", harness="py", parent="s-ghost"))
        await mm.join("m", "s-orphan", handle="orphan")
        assert _parents(mm) == {"orphan": None}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_parent_that_left_the_mesh_reads_as_a_root(home, tmp_path):
    """A4."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        await _team(mm, mgr, [
            ("s-lead", None, "lead"),
            ("s-api", "s-lead", "w-api"),
        ])
        assert _parents(mm)["w-api"] == "lead"
        await mm.leave("m", "lead")
        # the session still exists and is still the parent; the MEMBER is gone
        assert mgr.get("s-api").sdef.parent == "s-lead"
        assert _parents(mm) == {"w-api": None}
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. federation
# --------------------------------------------------------------------------- #
async def _trio(mgr, tmp_path):
    """pcA (authority, alice) + pcB (bob, and a worker bob spawned) + pcC."""
    mms = {
        name: MeshManager(mgr, settle=0.05, root=tmp_path / ("mesh" + name))
        for name in ("pcA", "pcB", "pcC")
    }
    _wire(mms)
    for name, parent in (("sa", None), ("sb", None), ("sb2", "sb"), ("sc", None)):
        mgr.create(SessionDef(name=name, harness="py", parent=parent))
    mms["pcA"].create("m")
    await mms["pcA"].join("m", "sa", handle="alice")
    for machine, session, handle in (
        ("pcB", "sb", "bob"), ("pcB", "sb2", "bob-w"), ("pcC", "sc", "carol")
    ):
        await mms[machine].join(
            "m@pcA", session, handle=handle,
            code=mms["pcA"].invite("m")["code"],
        )
    return mms


def test_a_guest_reports_the_lineage_of_the_members_it_hosts(home, tmp_path):
    """B1: only the daemon running a session can see its parent, so the
    authority has to be told."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mesh_a = mms["pcA"].get("m")
        await mms["pcA"]._flush_guest(mesh_a, "pcB")

        assert mesh_a.remote_lineage.get("bob-w") == "bob"
        assert _parents(mms["pcA"])["bob-w"] == "bob"
        # alice is the authority's own member: derived, not reported
        assert _parents(mms["pcA"])["alice"] is None
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_authority_relays_lineage_to_the_other_guests(home, tmp_path):
    """B2: without the relay, pcC would draw pcB's two agents as a flat pile —
    the diagram would only be right on the machine that happens to host them.

    Note what this does NOT do: send a message, or change the roster after A
    learns the lineage. Lineage arrives a hop behind the join it describes, so
    if learning it did not itself make the other guests due for a sync, the
    answer would sit on A until unrelated traffic gave it a lift.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mesh_a = mms["pcA"].get("m")
        await mms["pcA"]._flush_guest(mesh_a, "pcB")  # A learns it…
        await mms["pcA"]._flush_guest(mesh_a, "pcC")  # …and passes it on

        assert _parents(mms["pcC"])["bob-w"] == "bob"
        # and pcC still derives its own from the session tree, not the wire
        assert _parents(mms["pcC"])["carol"] is None
        await mgr.shutdown_all()

    asyncio.run(run())


def test_lineage_leaves_with_the_member(home, tmp_path):
    """B3: handles are reusable, so a stale parent would be inherited by
    whoever wears the handle next — the same argument that prunes the member
    graph on leave."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mesh_a = mms["pcA"].get("m")
        await mms["pcA"]._flush_guest(mesh_a, "pcB")
        assert mesh_a.remote_lineage.get("bob-w") == "bob"

        await mms["pcB"].leave("m", "bob-w")
        assert "bob-w" not in mesh_a.remote_lineage
        assert "bob-w" not in _parents(mms["pcA"])
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_departed_parent_does_not_leave_a_dangling_remote_edge(home, tmp_path):
    """B3, the other half: the child outlives the parent, and reads as a root
    rather than pointing at a handle no longer in the roster."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mms = await _trio(mgr, tmp_path)
        mesh_a = mms["pcA"].get("m")
        await mms["pcA"]._flush_guest(mesh_a, "pcB")
        assert _parents(mms["pcA"])["bob-w"] == "bob"

        await mms["pcB"].leave("m", "bob")
        assert _parents(mms["pcA"])["bob-w"] is None
        await mgr.shutdown_all()

    asyncio.run(run())
