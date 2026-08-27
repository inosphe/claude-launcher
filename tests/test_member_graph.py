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
   C5 the packaged reviewer rule wires a reviewer to the workers whichever
      of the two joined first — a rule is a predicate over a pair, so the
      wiring does not depend on the order a fleet comes up in

D. Authority
   D1 an agent may rewire an edge touching a session it spawned
   D2 an agent may not rewire an edge between two sessions it does not own
   D3 a human (no actor) may rewire anything

E. Lifecycle
   E1 edges naming a departed member are pruned, so a rejoining handle does
      not inherit its predecessor's isolation
   E2 the graph survives a reload from disk

G. Wiring that arrives after the members
   G1 `rewire` applies the rules in force to the members already enrolled —
      what a join cannot do for a rule that did not exist yet
   G2 it only ever opens: a pair somebody decided is untouched (so a cut
      stays cut), and a second run is a no-op
   G3 the HTTP surface the CLI drives
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

#: The same pair without the tree confinement — a single reviewer serving a
#: whole mesh, which is what the packaged rule ships as.
RULES_WORKER_REVIEWER_ANY = """
auto_link:
  rules:
    - between: [{tier: root}, {tier: root}]
    - between: [{role: worker}, {role: reviewer}]
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
        # The second fleet's root is a plain (reviewer) root, not a second
        # leader — the packaged leader is exclusive per mesh, and this test
        # is about the tree boundary, which any root provides.
        for name, parent in (
            ("lead", None), ("coder1", "lead"), ("qa1", "lead"),
            ("root2", None), ("coder2", "root2"),
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


def test_the_packaged_reviewer_rule_holds_whichever_end_joins_first(home, tmp_path):
    """C5: the packaged rule, on the packaged vocabulary — no upload at all.

    Both orders matter because both happen: a reviewer is usually added to a
    fleet already working, and workers keep being spawned after it. A rule is
    evaluated at BOTH joins, so neither order needs anyone to notice.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mm.create("team")
        # the worker is here first; the reviewer arrives into a running fleet
        for name, parent in (("lead", None), ("coder1", "lead"), ("qa1", None)):
            mgr.create(SessionDef(name=name, harness="py", parent=parent))
            await mm.join("team", name)
        mesh = mm.get("team")
        assert mesh.connected("coder1", "qa1") is True
        # ...and a worker spawned afterwards is wired to the reviewer too,
        # across the tree boundary: `within: any` on this rule
        mgr.create(SessionDef(name="coder2", harness="py", parent="lead"))
        await mm.join("team", "coder2")
        assert mesh.connected("coder2", "qa1") is True
        # the rule names a PAIR — two workers are still strangers
        assert mesh.connected("coder1", "coder2") is False
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


# --------------------------------------------------------------------------- #
# G. wiring that arrives after the members
# --------------------------------------------------------------------------- #
async def _fleet_wired_by_nothing(mm, mgr):
    """A mesh whose members joined while no rule named any of their pairs."""
    mm.create("team")
    await mm.set_roles("team", "auto_link: {rules: []}")
    for name, parent in (
        ("lead", None), ("coder1", "lead"), ("coder2", "lead"), ("qa1", None)
    ):
        mgr.create(SessionDef(name=name, harness="py", parent=parent))
        await mm.join("team", name)
    return mm.get("team")


def test_rewire_applies_a_later_rule_to_the_members_already_here(home, tmp_path):
    """G1: a join wires the member that is joining, so a rule that arrives
    afterwards has no join left to run in. Without this the only remedy is
    hand-wiring the fleet — the manual act the rules exist to remove."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _fleet_wired_by_nothing(mm, mgr)
        assert mesh.connected("coder1", "qa1") is False

        await mm.set_roles("team", RULES_WORKER_REVIEWER_ANY)
        # still nothing: uploading a document does not rewire anybody
        assert mesh.connected("coder1", "qa1") is False

        # every rule in force is applied, not just the one that changed --
        # `lead` and `qa1` are both roots, so the standing root rule that was
        # switched off at their join lands here too
        opened = await mm.rewire_members("team")
        assert opened == [
            {"a": "coder1", "b": "qa1"},
            {"a": "coder2", "b": "qa1"},
            {"a": "lead", "b": "qa1"},
        ]
        assert mesh.connected("coder1", "qa1") is True
        assert mesh.connected("coder2", "qa1") is True
        # ...and nothing was invented: no rule names two workers
        assert mesh.connected("coder1", "coder2") is False
        await mgr.shutdown_all()

    asyncio.run(run())


def test_rewire_only_opens_and_never_overrules_a_decision(home, tmp_path):
    """G2: the property that makes it safe to hand an operator. A pair
    somebody decided — a hand cut above all — is skipped whichever way it was
    decided, so no rule and no rerun can take reach back off a person."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _fleet_wired_by_nothing(mm, mgr)
        await mm.set_roles("team", RULES_WORKER_REVIEWER_ANY)
        # coder2 was deliberately kept away from the reviewer
        await mm.set_member_link("team", "coder2", "qa1", enabled=False)

        opened = await mm.rewire_members("team")
        assert opened == [{"a": "coder1", "b": "qa1"}, {"a": "lead", "b": "qa1"}]
        assert mesh.connected("coder2", "qa1") is False   # the cut stands
        assert mesh.connected("lead", "coder1") is True   # nothing was closed

        # ...and running it again says nothing and writes nothing
        before = dict(mesh.member_edges)
        assert await mm.rewire_members("team") == []
        assert mesh.member_edges == before
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_http_surface_rewires_a_mesh(home, tmp_path):
    """G3: the route `claunch mesh rewire` drives. POST, because it changes
    the graph — and with no `actor` in the body the caller is the operator,
    who owns the whole graph. This covers that the route runs, not who may
    call it: the token authenticates the daemon's door, not the session
    behind it, which is why G4 puts the confinement on the parameter."""
    _register_py_harness()
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            await client.post("/api/mesh", json={"name": "web"}, headers=auth)
            await client.put(
                "/api/mesh/web/roles",
                json={"yaml": "auto_link: {rules: []}"}, headers=auth,
            )
            for session, handle in (("s1", "coder1"), ("s2", "qa1")):
                mgr.create(SessionDef(name=session, harness="py", cwd=str(tmp_path)))
                await client.post(
                    "/api/mesh/web/members",
                    json={"session": session, "handle": handle}, headers=auth,
                )
            mesh = mm.get("web")
            assert mesh.connected("coder1", "qa1") is False

            await client.put(
                "/api/mesh/web/roles",
                json={"yaml": RULES_WORKER_REVIEWER_ANY}, headers=auth,
            )
            resp = await client.post("/api/mesh/web/rewire", json={}, headers=auth)
            assert resp.status == 200
            assert (await resp.json())["opened"] == [{"a": "coder1", "b": "qa1"}]
            assert mesh.connected("coder1", "qa1") is True
        finally:
            await client.close()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_http_surface_carries_actor_through(home, tmp_path):
    """G6: the route reads `actor` from the body and hands it on.

    G3 posts an empty body and G4 calls the manager directly, so between
    them nothing measured the line that connects the two -- the route could
    drop `actor` on the floor and both would still pass. s183 named that gap
    while re-reviewing the repair: the negative side stands (D3 runs over
    the route in G3), the positive side did not exist.

    Measured by difference rather than by inspection: the same request, once
    naming a caller that commands nothing and once naming nobody. If the
    parameter were dropped, the first call would open the edge too.
    """
    _register_py_harness()
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            await client.post("/api/mesh", json={"name": "web"}, headers=auth)
            await client.put(
                "/api/mesh/web/roles",
                json={"yaml": "auto_link: {rules: []}"}, headers=auth,
            )
            for session, handle in (("s1", "coder1"), ("s2", "qa1")):
                mgr.create(SessionDef(name=session, harness="py", cwd=str(tmp_path)))
                await client.post(
                    "/api/mesh/web/members",
                    json={"session": session, "handle": handle}, headers=auth,
                )
            await client.put(
                "/api/mesh/web/roles",
                json={"yaml": RULES_WORKER_REVIEWER_ANY}, headers=auth,
            )
            mesh = mm.get("web")

            # coder1 spawned nothing, so the pair is not its to hurry along
            resp = await client.post(
                "/api/mesh/web/rewire", json={"actor": "coder1"}, headers=auth
            )
            assert resp.status == 200
            assert (await resp.json())["opened"] == []
            assert mesh.connected("coder1", "qa1") is False

            # ...and the operator, over the same route, still gets it
            resp = await client.post("/api/mesh/web/rewire", json={}, headers=auth)
            assert (await resp.json())["opened"] == [{"a": "coder1", "b": "qa1"}]
            assert mesh.connected("coder1", "qa1") is True
        finally:
            await client.close()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_rewire_by_an_agent_reaches_only_its_own_subtree(home, tmp_path):
    """G4: the sweep is the same authority as an edit, not a way around it.

    D2 says an agent edits only the edges touching a session it spawned. A
    fleet-wide operation would be the one place that rule could be walked
    past, so `actor` puts every candidate edge through the same check --
    and a pair the caller does not command is *skipped*, not refused: a
    sweep names no pair, so there is nothing in it to reject, and one
    unowned pair must not abort the wiring the caller did ask for.

    `actor` is declared, not proven -- the same as on `set_member_link` --
    so this narrows an honest agent and is not what makes the operation
    safe. What makes it safe is the bound checked at the end: whoever calls,
    only edges a rule already names can open.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _fleet_wired_by_nothing(mm, mgr)
        await mm.set_roles("team", RULES_WORKER_REVIEWER_ANY)

        # coder1 spawned nothing, so it commands no session and owns no edge
        # -- not even the two it is an endpoint of. It gets silence, not an
        # error: nothing it was entitled to was withheld.
        assert await mm.rewire_members("team", actor="coder1") == []
        assert mesh.connected("coder1", "qa1") is False

        # lead spawned both coders, so the edges touching them are its own to
        # hurry along. `lead <-> qa1` is not: two roots, neither spawned by
        # lead, and `commands` does not include the actor itself (D2).
        opened = await mm.rewire_members("team", actor="lead")
        assert opened == [{"a": "coder1", "b": "qa1"}, {"a": "coder2", "b": "qa1"}]
        assert mesh.connected("lead", "qa1") is False

        # D3 still holds through this door: the human owns the whole graph,
        # and gets the pair the agent could not reach.
        assert await mm.rewire_members("team") == [{"a": "lead", "b": "qa1"}]
        assert mesh.connected("lead", "qa1") is True
        await mgr.shutdown_all()

    asyncio.run(run())


def test_rewire_opens_no_edge_a_rule_does_not_name_whoever_calls(home, tmp_path):
    """G5: the negative control for the sentence above -- the bound that
    actually carries the safety, measured on both doors rather than asserted
    in a comment. No rule names two workers, so no caller opens that pair:
    not the agent that spawned them both, not the operator.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        mesh = await _fleet_wired_by_nothing(mm, mgr)
        await mm.set_roles("team", RULES_WORKER_REVIEWER_ANY)

        await mm.rewire_members("team", actor="lead")   # owns both coders
        await mm.rewire_members("team")                 # owns everything
        assert mesh.connected("coder1", "coder2") is False
        await mgr.shutdown_all()

    asyncio.run(run())
