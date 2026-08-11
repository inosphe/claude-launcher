"""The member graph: who inside a mesh may message whom.

One layer up from ``test_mesh_graph.py``, whose edges are between *daemons*
and only move traffic around. An edge here is an ACL: members are never
routed, so a cut pair simply cannot speak.

Scenario matrix:

A. Default
   A1 a mesh nobody has rewired is the complete graph it always was
   A2 nothing new is written to mesh.json until an edge is actually cut

B. Cutting
   B1 a send to a disconnected member is refused, and the refusal names who
      the sender CAN reach
   B2 '*' narrows silently to the sender's neighbours
   B3 a cut is duplex — it holds whichever end sends
   B4 a fully isolated member's broadcast says so, not "no other members"

C. Spawn seeding
   C1 isolate() leaves a new member connected to the peers named, no others
   C2 later joiners are connected by default (isolation is a snapshot)

D. Authority
   D1 an agent may rewire an edge touching a session it spawned
   D2 an agent may not rewire an edge between two sessions it does not own
   D3 a human (no actor) may rewire anything

E. Lifecycle
   E1 edges naming a departed member are pruned, so a rejoining handle does
      not inherit its predecessor's isolation
   E2 the graph survives a reload from disk
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from claude_launcher import store
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshError, MeshManager

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


async def _team(mm: MeshManager, mgr: SessionManager, *, names=("lead", "w1", "w2")):
    """A mesh of real sessions, the first one parent to all the others."""
    mm.create("team")
    for name in names:
        parent = None if name == names[0] else names[0]
        mgr.create(SessionDef(name=name, harness="py", parent=parent))
        await mm.join("team", name)
    return mm.get("team")


# --------------------------------------------------------------------------- #
# A. default
# --------------------------------------------------------------------------- #
def test_an_untouched_mesh_is_the_complete_graph(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mesh = await _team(MeshManager(mgr, root=tmp_path / "mesh"), mgr)
        # A1
        assert mesh.connected("w1", "w2") is True
        assert mesh.neighbours("w1") == ["lead", "w2"]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_nothing_is_persisted_until_an_edge_is_cut(home, tmp_path):
    """A2: a mesh that never touches this writes exactly the file it always
    did, so an older daemon reading it sees no new key."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        await _team(mm, mgr)
        doc = json.loads(
            (mm._mesh_dir("team") / "mesh.json").read_text(encoding="utf-8")
        )
        assert "member_edges" not in doc
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        doc = json.loads(
            (mm._mesh_dir("team") / "mesh.json").read_text(encoding="utf-8")
        )
        assert doc["member_edges"] == {"w1|w2": False}
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. cutting
# --------------------------------------------------------------------------- #
def test_a_send_across_a_cut_is_refused_and_names_the_alternative(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        await _team(mm, mgr)
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        # B1
        with pytest.raises(MeshError) as exc:
            await mm.send("team", "w1", "w2", "hello")
        assert "no connection to w2" in str(exc.value)
        assert "lead" in str(exc.value)  # who it CAN reach
        await mgr.shutdown_all()

    asyncio.run(run())


def test_broadcast_narrows_to_the_senders_neighbours(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        # B2: no error — a broadcast means "everyone I can reach"
        result = await mm.send("team", "w1", "*", "morning")
        assert result["recipients"] == ["lead"]
        assert mesh.pending("w2") == []
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_cut_holds_from_either_end(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        await _team(mm, mgr)
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        # B3: the edge was named (w1, w2); the reverse must be just as dead
        with pytest.raises(MeshError):
            await mm.send("team", "w2", "w1", "and back")
        await mgr.shutdown_all()

    asyncio.run(run())


def test_an_isolated_members_broadcast_explains_itself(home, tmp_path):
    """B4: 'no other members to deliver to' would be a lie with two peers in
    the roster — and sends the agent looking for the wrong problem."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        await _team(mm, mgr)
        await mm.isolate_member("team", "w1", keep=())
        with pytest.raises(MeshError) as exc:
            await mm.send("team", "w1", "*", "anyone?")
        assert "not connected to any" in str(exc.value)
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. spawn seeding
# --------------------------------------------------------------------------- #
def test_isolate_keeps_exactly_the_named_peers(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr, names=("lead", "w1", "w2", "qa"))
        # C1
        cut = await mm.isolate_member("team", "qa", keep={"lead"})
        assert sorted(cut) == ["w1", "w2"]
        assert mesh.neighbours("qa") == ["lead"]
        assert mesh.neighbours("w1") == ["lead", "w2"]  # untouched pair
        await mgr.shutdown_all()

    asyncio.run(run())


def test_isolation_is_a_snapshot_not_a_standing_rule(home, tmp_path):
    """C2: a member who joins later is connected by default. Documented, and
    the reason the parent — not the daemon — decides who reaches its child."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        await mm.isolate_member("team", "w1", keep={"lead"})
        mgr.create(SessionDef(name="late", harness="py"))
        await mm.join("team", "late")
        assert mesh.connected("w1", "late") is True
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. authority
# --------------------------------------------------------------------------- #
def test_a_parent_may_wire_up_its_own_children(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        # D1: lead spawned both, so it owns both ends
        await mm.set_member_link("team", "w1", "w2", enabled=False, actor="lead")
        assert mesh.connected("w1", "w2") is False
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_sibling_may_not_rewire_its_peers(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        # D2: w1 spawned nothing — it owns no edge, not even one it is on
        with pytest.raises(MeshError) as exc:
            await mm.set_member_link("team", "lead", "w2", enabled=False, actor="w1")
        assert "may not rewire" in str(exc.value)
        assert mesh.connected("lead", "w2") is True
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_human_owns_the_whole_graph(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        # D3: no actor = the CLI or the dashboard, which is not restricted
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        assert mesh.connected("w1", "w2") is False
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# E. lifecycle
# --------------------------------------------------------------------------- #
def test_leaving_prunes_the_edges_that_named_you(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr)
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        await mm.leave("team", "w2")
        # E1: a different session reusing the handle starts clean
        mgr.create(SessionDef(name="w2b", harness="py", parent="lead"))
        await mm.join("team", "w2b", handle="w2")
        assert mesh.connected("w1", "w2") is True
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_graph_survives_a_reload(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "mesh"
        mm = MeshManager(mgr, root=root)
        await _team(mm, mgr)
        await mm.set_member_link("team", "w1", "w2", enabled=False)
        # E2
        reloaded = MeshManager(mgr, root=root)
        reloaded.load_all()
        assert reloaded.get("team").connected("w1", "w2") is False
        await mgr.shutdown_all()

    asyncio.run(run())
