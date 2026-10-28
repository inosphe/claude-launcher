"""tests/test_routing_rosters.py

The landing side and the question side of one worker read DIFFERENT mesh
rosters (claunch-eyz0s.1). This file pins that difference, and pins the one
rule the two sides do share: a member mirrored from another machine is never
a candidate, however its session name reads.

Both sides are driven here off ONE real ``MeshManager`` -- the same roster
document the daemon serves at ``GET /api/mesh`` -- so a change that makes the
two readers agree about who is in the mesh, or that lets a mirrored namesake
through on either side, fails here rather than in a night's routing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from claude_launcher import daemon_client
from claude_launcher.cflow import model as v1
from claude_launcher.cflow import responders
from claude_launcher.daemon import landing_route
from claude_launcher.daemon.mesh import Member, MeshManager

#: The evidence shape ``pool`` reads its diagnosis from: a daemon that is up.
SERVING = {
    "state": "serving", "pid": None, "started_at": None, "probes": 0,
    "successes": 0, "budget": 0.0, "base_url": None, "lock_free": True,
}


class _Sessions:
    """The slice of ``SessionManager`` a mesh join and a roster read touch.

    A join also types a roster note into the other members' terminals
    (``MeshManager._tell``), so a session here has to accept a delivery. It
    records them instead, which is what lets a test say nothing was typed
    into anybody.
    """

    def __init__(self, names) -> None:
        self.names = set(names)
        self.delivered = []

    def get(self, name):
        if name not in self.names:
            raise KeyError(name)
        sessions = self

        async def deliver(block):
            sessions.delivered.append((name, block))
            return True

        async def capture(n=200):
            return []

        return SimpleNamespace(
            exited=False, status=lambda: "idle", keyboard_busy=lambda: False,
            deliver=deliver, capture=capture,
            sdef=SimpleNamespace(parent=None, profile=None),
        )

    def ancestors(self, name):
        return []

    def direct_kin(self, a, b):
        return False


def _client(mm, names):
    """``GET /api/mesh`` answered with the real ``mesh_info`` of these meshes."""

    class _Client:
        def get(self, path, **kw):
            if path == "/api/mesh":
                return {"meshes": [mm.mesh_info(mm.get(n), state="all") for n in names]}
            return {"sessions": []}

        def post(self, path, body, **kw):
            return {}

        def patch(self, path, body=None, **kw):
            return {"enabled": True}

    return _Client()


def _serve(monkeypatch, mm, names, *, brief=False):
    """Point the question side at a roster holding only ``names``.

    ``brief=False`` is what keeps this a test of the readers: a join schedules
    a briefing into the member's terminal, and there is nobody to type into.
    """
    client = _client(mm, names)
    monkeypatch.setattr(responders.daemon_client, "connect", lambda: client)
    monkeypatch.setattr(responders.daemon_client, "connect_with_diagnosis",
                        lambda **kw: (client, dict(SERVING)))
    return client


async def _mesh(tmp_path, sessions, joins=()):
    """A real ``MeshManager`` with ``joins`` = ``[(mesh, session, handle, role)]``."""
    mm = MeshManager(_Sessions(sessions), settle=0.05, root=tmp_path / "mesh")
    mm.machine = ""
    for name in sorted({j[0] for j in joins}):
        mm.create(name)
    for mesh, session, handle, role in joins:
        await mm.join(mesh, session, handle=handle, role=role)
    return mm


def _holders(*taking):
    """A ``holders`` closure naming which sessions drive a landing queue."""
    def holders(name, repo):
        return [landing_route.Holder("/l", None, True)] if name in taking else []
    return holders


def _leaders(pool, scope=v1.SCOPE_ANY):
    found, why = pool.match(v1.Candidate(role="leader", scope=scope))
    return [r.handle for r in found], why


# --------------------------------------------------------------------------- #
# the difference this issue is about
# --------------------------------------------------------------------------- #
def test_the_landing_side_reads_a_mesh_the_question_side_cannot_see(
    tmp_path, monkeypatch
):
    """One worker in meshes A and B. The landing request goes to the leader of
    B; the question -- with the run pinned to A -- can only reach the leader of
    A. Neither answer is wrong: they are answering from different rosters."""
    monkeypatch.setattr(landing_route.cflow_landing, "common_dir", lambda c: "/repo")

    async def run():
        mm = await _mesh(tmp_path, {"w1", "q-leader", "l-leader"}, [
            ("A", "w1", "w1", "worker"), ("A", "q-leader", "q-leader", "leader"),
            ("B", "w1", "w1", "worker"), ("B", "l-leader", "l-leader", "leader"),
        ])
        assert [r["mesh"] for r in mm.meshes_for_session("w1")] == ["A", "B"]

        # landing: only B's leader drives a queue, and the landing side sees B
        found = landing_route.Router(mm.manager, mm, _holders("l-leader")) \
            .independent("/w", "w1")
        assert found.owner == "l-leader", found.why

        # question: the run is pinned to A, so B's leader is not even a member
        _serve(monkeypatch, mm, ["A"])
        pool = responders.pool(session="w1", mesh="A")
        assert pool.mesh == "A"
        assert sorted(pool.members) == ["q-leader"]
        assert _leaders(pool) == (["q-leader"], None)

        # ... and pinned to B it is the other leader, not both
        _serve(monkeypatch, mm, ["B"])
        assert _leaders(responders.pool(session="w1", mesh="B"))[0] == ["l-leader"]

    asyncio.run(run())


def test_a_second_mesh_makes_the_unpinned_question_ambiguous(tmp_path, monkeypatch):
    """The same worker, with no mesh named for the run: the question side
    refuses to choose, which is how joining one more mesh breaks asking."""
    async def run():
        mm = await _mesh(tmp_path, {"w1", "q-leader", "l-leader"}, [
            ("A", "w1", "w1", "worker"), ("A", "q-leader", "q-leader", "leader"),
            ("B", "w1", "w1", "worker"), ("B", "l-leader", "l-leader", "leader"),
        ])
        _serve(monkeypatch, mm, ["A", "B"])
        pool = responders.pool(session="w1")
        assert pool.ambiguous == ("A", "B")
        assert "several meshes (A, B)" in pool.problem
        # a setting, not a fact: the message names the command that settles it
        assert "claunch cflow2 mesh" in pool.problem
        assert not pool.unreadable
        # naming one is enough, and it does not route to the other's leader
        assert _leaders(responders.pool(session="w1", mesh="A"))[0] == ["q-leader"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the rule the two sides share: a mirrored namesake is nobody
# --------------------------------------------------------------------------- #
def test_the_question_side_refuses_a_leader_mirrored_from_another_machine(
    tmp_path, monkeypatch
):
    """A handle on a mirrored mesh can carry a session name from another
    machine. It matches the role and is wired, and is still not a candidate:
    answering writes this run's state, which lives on this machine."""

    async def run():
        mm = await _mesh(tmp_path, {"w1", "far"}, [("N", "w1", "w1", "worker")])
        mm.get("N").members["far-h"] = Member(
            "far-h", "far", machine="pcB", role="leader")
        await mm.set_member_link("N", "w1", "far-h", enabled=True)
        _serve(monkeypatch, mm, ["N"])
        pool = responders.pool(session="w1", mesh="N")
        assert pool.me == "w1"
        assert pool.members["far-h"].local is False
        assert "far-h" in pool.reachable         # wired, so not the miss
        asks, why = _leaders(pool)
        assert asks == []
        assert "far-h matched but cannot answer from here" in why
        assert "another daemon" in why

    asyncio.run(run())


def test_the_landing_side_refuses_a_leader_mirrored_from_another_machine(
    tmp_path, monkeypatch
):
    """The same member read by the landing side: refused, and the reason says
    it is another machine rather than that it has no queue."""
    monkeypatch.setattr(landing_route.cflow_landing, "common_dir", lambda c: "/repo")

    async def run():
        mm = await _mesh(tmp_path, {"w1", "far"}, [("N", "w1", "w1", "worker")])
        mm.get("N").members["far-h"] = Member(
            "far-h", "far", machine="pcB", role="leader")
        found = landing_route.Router(
            mm.manager, mm, _holders("far")).independent("/w", "w1")
        assert found.holder is None
        assert "leader 'far' is on another machine" in found.why
        # the namesake is not what failed -- it never reached the queue question
        assert "declares a landing queue" not in found.why.split("another machine")[0]

    asyncio.run(run())


def test_a_mirrored_namesake_is_not_the_asking_session_either(tmp_path, monkeypatch):
    """The other direction: a mesh whose only row carrying our session name is
    another machine's. The question side must not adopt that row as itself --
    reading it as "me" would route a question as somebody else's agent."""
    async def run():
        mm = await _mesh(tmp_path, {"w1"}, [])
        mm.create("N")
        mm.get("N").members["theirs"] = Member(
            "theirs", "w1", machine="pcB", role="leader")
        assert mm.meshes_for_session("w1") == []
        _serve(monkeypatch, mm, ["N"])
        pool = responders.pool(session="w1", mesh="N")
        assert pool.me == ""
        assert "not a member of mesh 'N'" in pool.problem

    asyncio.run(run())


def test_a_mirrored_namesake_does_not_put_the_session_in_that_mesh(tmp_path):
    """``meshes_for_session`` decides membership by locality, not by name. The
    landing side trusts it, so this is what keeps a namesake out of every
    landing candidate list before the role check ever runs."""

    async def run():
        mm = await _mesh(tmp_path, {"w1", "far"}, [("A", "w1", "w1", "worker")])
        # a mesh the worker has no row in except another machine's row that
        # happens to carry the same session name
        mm.create("Away")
        mm.get("Away").members["w1@pcB"] = Member("w1@pcB", "w1", machine="pcB")
        assert [r["mesh"] for r in mm.meshes_for_session("w1")] == ["A"]
        assert "Away" not in {r["mesh"] for r in mm.meshes_for_session("w1")}

    asyncio.run(run())
