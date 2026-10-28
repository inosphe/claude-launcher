"""An independent child is wired to the leader its landing goes to (claunch-u7nu3).

The default ``auto_link`` joins a spawned child to its parent and nothing
else, so a ``relation: independent`` worker under another worker could not
message the leader that takes its landing request (landing-nudge, settle
asks). The spawn's join now connects it to the one leader
:meth:`.landing_route.Router.independent` names -- never a leader of another
repository.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher.daemon import landing_route, onboard
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshError


class _Mesh:
    def __init__(self, members, edges=()):
        self.members = {h: SimpleNamespace(handle=h, session=s, roles=r)
                        for h, (s, r) in members.items()}
        self.edges = {frozenset(e) for e in edges}

    def connected(self, a, b):
        return frozenset((a, b)) in self.edges


class _MeshMgr:
    def __init__(self, mesh, refuse=False):
        self.mesh = mesh
        self.refuse = refuse
        self.links = []

    def get(self, name):
        return self.mesh

    def member_for_session(self, mesh, session):
        return next((m for m in mesh.members.values() if m.session == session), None)

    async def set_member_link(self, name, a, b, *, enabled, actor="", by=""):
        if self.refuse:
            raise MeshError("no such member")
        self.links.append((name, a, b, enabled))
        self.mesh.edges.add(frozenset((a, b)))


def _plan(relation="independent", version=2):
    return onboard.Plan(mesh="claunch", parent="p", workflow="improv-worker-v2",
                        workflow_version=version, cflow_inputs={"relation": relation})


def _run(plan, mm, monkeypatch, target):
    monkeypatch.setattr(onboard, "_landing_target", lambda *a: target)
    return asyncio.run(onboard._landing_leader(plan, "w", "w-h", cwd="/w",
                                               mesh_mgr=mm, manager=object()))


def test_only_an_independent_v2_child_asks_for_its_landing_leader():
    assert _plan().independent
    assert not _plan("stacked").independent
    assert not _plan(version=1).independent
    assert not onboard.Plan(workflow_version=2).independent


def test_the_child_is_connected_to_the_leader_the_router_names(monkeypatch):
    mm = _MeshMgr(_Mesh({"w-h": ("w", ["worker"]), "lead-h": ("s29", ["leader"])}))
    out = _run(_plan(), mm, monkeypatch, ("s29", ""))
    assert out == {"connected": True, "session": "s29", "handle": "lead-h"}
    assert mm.links == [("claunch", "w-h", "lead-h", True)]


def test_an_existing_edge_is_not_made_twice(monkeypatch):
    mm = _MeshMgr(_Mesh({"w-h": ("w", ["worker"]), "lead-h": ("s29", ["leader"])},
                        edges=[("w-h", "lead-h")]))
    assert _run(_plan(), mm, monkeypatch, ("s29", ""))["connected"]
    assert mm.links == []


def test_no_leader_for_this_repository_wires_nobody_and_says_why(monkeypatch):
    why = ("mesh leader: leader 's29' takes landings for another repository "
           "(/repo/claunch), not the worker's (/repo/gds6)")
    mm = _MeshMgr(_Mesh({"w-h": ("w", ["worker"]), "lead-h": ("s29", ["leader"])}))
    out = _run(_plan(), mm, monkeypatch, (None, why))
    assert out == {"connected": False, "why": why}
    assert mm.links == []


def test_a_refused_link_is_reported_and_the_spawn_goes_on(monkeypatch):
    mm = _MeshMgr(_Mesh({"w-h": ("w", ["worker"]), "lead-h": ("s29", ["leader"])}),
                  refuse=True)
    out = _run(_plan(), mm, monkeypatch, ("s29", ""))
    assert out["connected"] is False and out["why"] == "no such member"


def test_the_router_reads_a_parent_the_session_record_does_not_carry_yet(monkeypatch):
    """At the join the child's record may not name its parent; the stated one
    makes the leader beside that parent a sibling of the chain."""
    sessions = {
        "w": SimpleNamespace(exited=False, sdef=SessionDef(name="w", cwd="/w")),
        "p": SimpleNamespace(exited=False, sdef=SessionDef(name="p", cwd="/p")),
        "lead": SimpleNamespace(exited=False, sdef=SessionDef(name="lead", cwd="/l")),
    }
    manager = SimpleNamespace(get=lambda n: sessions[n])
    members = {h: SimpleNamespace(session=h, roles=r) for h, r in
               {"w": ["worker"], "p": ["worker"], "lead": ["leader"]}.items()}
    mesh = SimpleNamespace(
        members=members,
        meshes_for_session=lambda s: [{"mesh": "claunch"}],
        get=lambda n: mesh,
        is_local_member=lambda m, mem: True,
    )
    holders = lambda name, repo: (  # noqa: E731
        [landing_route.Holder("/l", None, True)] if name == "lead" else [])
    monkeypatch.setattr(landing_route.cflow_landing, "common_dir", lambda c: "/repo")
    # The record names a parent that is no session: "lead" is then neither
    # a sibling nor an ancestor. The stated parent "p" (a root) makes it one.
    sessions["w"].sdef = SessionDef(name="w", cwd="/w", parent="ghost")
    router = landing_route.Router(manager, mesh, holders)
    assert router.independent("/w", "w").holder is None
    router = landing_route.Router(manager, mesh, holders)
    found = router.independent("/w", "w", parent="p")
    assert found.owner == "lead", found.why


class _JoinMgr(_MeshMgr):
    """Adds the join-side calls ``onboard._join`` makes."""

    def defer_briefing(self, name):
        import contextlib
        return contextlib.nullcontext()

    async def join(self, mesh, name, **kw):
        member = SimpleNamespace(handle="w-h", session=name, role="worker",
                                 subroles=(), roles=["worker"])
        self.mesh.members["w-h"] = member
        return member

    def briefing_block(self, mesh, member):
        return "brief"


def _mesh_neighbours(mesh):
    mesh.neighbours = lambda h: sorted(
        next(iter(e - {h})) for e in mesh.edges if h in e)
    return mesh


def test_the_join_wires_an_independent_child_and_lists_the_leader(monkeypatch):
    mm = _JoinMgr(_mesh_neighbours(_Mesh({"lead-h": ("s29", ["leader"])})))
    monkeypatch.setattr(onboard, "_landing_target", lambda *a: ("s29", ""))
    out = asyncio.run(onboard._join(_plan(), "w", mesh_mgr=mm, cwd="/w", manager=object()))
    assert out["landing_leader"]["connected"] is True
    assert out["connected_to"] == ["lead-h"]


def test_the_join_leaves_a_stacked_child_to_its_parent(monkeypatch):
    mm = _JoinMgr(_mesh_neighbours(_Mesh({"lead-h": ("s29", ["leader"])})))
    monkeypatch.setattr(onboard, "_landing_target",
                        lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
    out = asyncio.run(onboard._join(_plan("stacked"), "w", mesh_mgr=mm, cwd="/w",
                                    manager=object()))
    assert "landing_leader" not in out and mm.links == []


def test_a_checkout_with_no_git_wires_nobody(tmp_path):
    owner, why = onboard._landing_target(object(), object(), str(tmp_path), "w", "p")
    assert owner is None and "no git readable" in why
