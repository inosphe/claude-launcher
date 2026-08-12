"""The member graph: who inside a mesh may message whom.

One layer up from ``test_mesh_graph.py``, whose edges are between *daemons*
and only move traffic around. An edge here is an ACL: members are never
routed, so a cut pair simply cannot speak.

Scenario matrix:

A. What a join wires
   A1 roots reach each other — the packaged rule, and the graph every mesh
      had before there was a rule
   A2 a spawned child reaches its parent and nobody else
   A3 the join records what it opened, and records only that: isolating a
      child costs one edge, not a cut against every member present
   A4 a mesh written before any of this stays the complete graph it was —
      nothing migrates

B. Cutting
   B1 a send to a disconnected member is refused, and the refusal names who
      the sender CAN reach
   B2 '*' narrows silently to the sender's neighbours
   B3 a cut is duplex — it holds whichever end sends
   B4 a fully isolated member's broadcast says so, not "no other members"

C. Rules and standing isolation
   C1 isolate() leaves a member connected to the peers named, no others
   C2 a wired member stays isolated from members who join LATER — the
      standing rule the snapshot never was
   C3 a role rule connects across the tree; `within: tree` keeps it home
   C4 no rule ever withholds the parent edge, at either end of it — a parent
      that rejoins after its children is wired back to them

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

#: A mesh that wants its producers to reach its auditors, but only inside one
#: fleet. `coder` is an alias of `worker` and `qa` of `reviewer`, so the two
#: handles below self-select into the pair this names.
RULES_WORKER_REVIEWER = """
auto_link:
  rules:
    - between: [{tier: root}, {tier: root}]
    - between: [{role: worker}, {role: reviewer}]
      within: tree
"""


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


async def _settled(mgr, timeout: float = 20.0):
    """Wait until every session has booted and gone idle (delivery is
    idle-gated, so a test that sends immediately would just queue)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if all(s.status() == "idle" for s in mgr.list()):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("sessions never settled")


async def _drained(mesh, handle: str, timeout: float = 20.0):
    """Wait until `handle` has actually been delivered its mail -- `owed` only
    counts DELIVERED messages, so asserting before this races the worker."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if mesh.pending(handle) == []:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{handle!r} still has pending mail")


def _unanswered(mesh, handle: str) -> bool:
    """The heartbeat's own verdict, computed exactly as mesh_policy does."""
    st = mesh.activity.get(handle) or {}
    return st.get("last_asked", 0.0) > 0 and st.get("last_sent", 0.0) < st["last_asked"]

# --------------------------------------------------------------------------- #
# A. default
# --------------------------------------------------------------------------- #
def test_sessions_a_human_started_all_reach_each_other(home, tmp_path):
    """A1: nobody spawned them, so they are all tier 0 and the packaged rule
    connects them — the complete graph a mesh of peers has always been."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("a", "b", "c"):
            mgr.create(SessionDef(name=name, harness="py"))
            await mm.join("team", name)
        mesh = mm.get("team")
        assert mesh.neighbours("a") == ["b", "c"]
        assert mesh.neighbours("c") == ["a", "b"]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_spawned_child_reaches_its_parent_and_nobody_else(home, tmp_path):
    """A2: the whole point. A child arriving into a room full of members is
    connected to the one that asked for it, and to none of the others —
    including the siblings it will never have been introduced to."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr, names=("lead", "w1", "w2"))
        assert mesh.neighbours("w1") == ["lead"]
        assert mesh.neighbours("w2") == ["lead"]
        assert mesh.connected("w1", "w2") is False
        # ...and a grandchild goes no further either: one edge, upwards.
        mgr.create(SessionDef(name="helper", harness="py", parent="w1"))
        await mm.join("team", "helper")
        assert mesh.neighbours("helper") == ["w1"]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_join_records_what_it_opened_and_only_that(home, tmp_path):
    """A3: isolating a child costs ONE edge — the parent's. The old seeding
    wrote a cut against every member that happened to be in the room, which
    is the n-per-spawn the wiring exists to stop."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        await _team(mm, mgr, names=("lead", "w1", "w2"))
        doc = json.loads(
            (mm._mesh_dir("team") / "mesh.json").read_text(encoding="utf-8")
        )
        assert doc["member_edges"] == {"lead|w1": True, "lead|w2": True}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_mesh_from_before_the_wiring_stays_complete(home, tmp_path):
    """A4: `wired` is per member exactly so this holds. A roster written by a
    daemon that never heard of it carries no flag, reads as unwired, and its
    members go on reaching each other with no migration run against them."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        root = tmp_path / "mesh"
        mm = MeshManager(mgr, root=root)
        await _team(mm, mgr, names=("lead", "w1", "w2"))
        # Rewind the file to what an older daemon would have written: members
        # with no `wired` key, and no edge table at all.
        path = mm._mesh_dir("team") / "mesh.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("member_edges", None)
        for entry in doc["members"].values():
            entry.pop("wired", None)
        path.write_text(json.dumps(doc), encoding="utf-8")

        reloaded = MeshManager(mgr, root=root)
        reloaded.load_all()
        mesh = reloaded.get("team")
        assert mesh.connected("w1", "w2") is True
        assert mesh.neighbours("w1") == ["lead", "w2"]
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
# C. rules and standing isolation
# --------------------------------------------------------------------------- #
def test_isolate_keeps_exactly_the_named_peers(home, tmp_path):
    """C1: still the operator's blunt instrument, now on a graph where most
    of the cuts it would make are already the default."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        for name in ("a", "b", "c", "qa"):
            mgr.create(SessionDef(name=name, harness="py"))
            await mm.join("team", name)
        mesh = mm.get("team")
        cut = await mm.isolate_member("team", "qa", keep={"a"})
        assert sorted(cut) == ["b", "c"]
        assert mesh.neighbours("qa") == ["a"]
        assert mesh.neighbours("b") == ["a", "c"]  # untouched pair
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_wired_member_stays_isolated_from_later_joiners(home, tmp_path):
    """C2: the reversal. Isolation used to be a snapshot — cuts against the
    members present, and a standing invitation to everyone who arrived after
    — so a child quietly gained a peer every time the fleet grew. A wired
    member has the edges its join recorded and no others, whenever the other
    end showed up."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr, names=("lead", "w1"))
        mgr.create(SessionDef(name="late", harness="py"))
        await mm.join("team", "late")
        # `late` is a root, so the packaged rule connects it to the other
        # root — and to the child of that root, not at all.
        assert mesh.connected("lead", "late") is True
        assert mesh.connected("w1", "late") is False
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_role_rule_connects_across_the_tree(home, tmp_path):
    """C3: what the tree cannot say. 'A worker should reach a reviewer' is a
    relation the spawn forest does not have, and is the reason the rules are
    a document rather than a constant."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        await mm.set_roles("team", RULES_WORKER_REVIEWER)
        for name, parent in (
            ("lead", None), ("coder1", "lead"), ("qa1", "lead"),
            ("lead2", None), ("coder2", "lead2"),
        ):
            mgr.create(SessionDef(name=name, harness="py", parent=parent))
            await mm.join("team", name)
        mesh = mm.get("team")
        # coder1 is a worker, qa1 a reviewer (by handle), both under `lead`
        assert mesh.connected("coder1", "qa1") is True
        # ...and `within: tree` is what keeps the other fleet's reviewer out
        assert mesh.connected("coder2", "qa1") is False
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_parent_arriving_after_its_child_still_gets_the_edge(home, tmp_path):
    """C4, other end. A member can join at either end of a spawn edge — a
    lead that left and rejoined would otherwise come back unable to reach the
    workers still running for it, since leaving took its edges with it."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _team(mm, mgr, names=("lead", "w1", "w2"))
        await mm.leave("team", "lead")
        assert not [k for k in mesh.member_edges if "lead" in k.split("|")]
        await mm.join("team", "lead")
        assert mesh.neighbours("lead") == ["w1", "w2"]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_no_rule_can_withhold_the_parent_edge(home, tmp_path):
    """C4: a child that cannot reach its parent cannot report, and the reply
    command its briefing hands it fails. So the parent edge is the join's
    doing — an empty rule set removes every other edge and not that one."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        await mm.set_roles("team", "auto_link: {rules: []}")
        for name, parent in (("lead", None), ("w1", "lead"), ("other", None)):
            mgr.create(SessionDef(name=name, harness="py", parent=parent))
            await mm.join("team", name)
        mesh = mm.get("team")
        assert mesh.neighbours("w1") == ["lead"]
        # with no rules at all, even two roots are strangers
        assert mesh.connected("lead", "other") is False
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
        # The lead wires its two workers together, as a lead may.
        await mm.set_member_link("team", "w1", "w2", enabled=True)
        await mm.leave("team", "w2")
        assert not [k for k in mesh.member_edges if "w2" in k.split("|")]
        # E1: a different session reusing the handle starts clean — it is
        # wired by its own join, not by what its predecessor was granted.
        mgr.create(SessionDef(name="w2b", harness="py", parent="lead"))
        await mm.join("team", "w2b", handle="w2")
        assert mesh.neighbours("w2") == ["lead"]
        assert mesh.connected("w1", "w2") is False
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


# --------------------------------------------------------------------------- #
# F. the graph and the debt ledger agree
# --------------------------------------------------------------------------- #
def test_cutting_an_edge_settles_the_debt_it_carried(home, tmp_path):
    """F1: `owed` is recomputed through the graph, the heartbeat's
    `last_asked` is a stamp that would just sit there. Left alone the two
    disagree -- and the nudge would be worse than noise, because a member
    that obeyed it would have its reply refused by the same cut."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh", settle=0.05)
        mm.start()
        mesh = await _team(mm, mgr, names=("lead", "w1"))
        await _settled(mgr)

        await mm.send("team", "lead", "w1", "please answer", type="ask")
        await _drained(mesh, "w1")
        assert len(mesh.owed("w1")) == 1
        assert _unanswered(mesh, "w1") is True

        await mm.set_member_link("team", "lead", "w1", enabled=False)
        assert mesh.owed("w1") == []
        assert _unanswered(mesh, "w1") is False

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_a_cut_elsewhere_leaves_a_real_debt_being_chased(home, tmp_path):
    """F2: settling is per member and only when nothing is left -- a member
    that still owes someone reachable must still be nudged."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh", settle=0.05)
        mm.start()
        mesh = await _team(mm, mgr, names=("lead", "w1", "w2"))
        await _settled(mgr)
        # Siblings are strangers until the lead introduces them.
        await mm.set_member_link("team", "w2", "w1", enabled=True)

        await mm.send("team", "lead", "w1", "answer me", type="ask")
        await mm.send("team", "w2", "w1", "and me", type="ask")
        await _drained(mesh, "w1")
        assert len(mesh.owed("w1")) == 2

        await mm.set_member_link("team", "w2", "w1", enabled=False)
        assert len(mesh.owed("w1")) == 1        # lead's question stands
        assert _unanswered(mesh, "w1") is True  # ...so the chase continues

        await mm.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())
