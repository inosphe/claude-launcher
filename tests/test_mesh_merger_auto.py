"""The ``merger`` subrole the daemon grants on its own (claunch-nie6o).

A worker's landing request goes only to a session holding ``merger``
(``landing_route.Router.merger_role``). A session that drives a run holding a
landing queue is given it by the trigger clock rather than waiting for a
person; the grant is shown (the member's notice, the roster record, the CLI)
and a person can take it back, after which it is not granted again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher.daemon import cflow2_triggers, landing_route
from claude_launcher.daemon.cflow_clock import TriggerClock
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import Member, MeshManager

from test_mesh_graph import _register_py_harness

REASON = "it drives a run that holds a landing queue (v1 stack at X)"


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def test_the_record_carries_the_daemons_grants_and_a_persons_removals():
    m = Member("w1", "s1", role="worker", subroles=["merger"],
               auto_subroles={"merger": REASON, "ghost": "x"}, declined=["Reviewer"])
    # A reason is kept only for a subrole the member holds.
    assert m.auto_subroles == {"merger": REASON}
    assert m.declined == ["reviewer"]
    back = Member.from_dict(m.to_dict())
    assert (back.auto_subroles, back.declined) == ({"merger": REASON}, ["reviewer"])
    # A record from a daemon that predates the keys has neither.
    doc = m.to_dict()
    doc.pop("auto_subroles")
    doc.pop("declined")
    old = Member.from_dict(doc)
    assert (old.auto_subroles, old.declined) == ({}, [])


def test_a_grant_is_announced_and_a_persons_removal_sticks(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        await mm.join("m", "s1", handle="lead1")
        await mm.join("m", "s2", handle="worker2")
        mesh = mm.get("m")
        w = mesh.members["worker2"]

        assert mm.grant_auto_subrole("s2", "merger", REASON) == [("m", "worker2")]
        assert w.subroles == ["merger"]
        assert w.auto_subroles == {"merger": REASON}
        # The notice names the reason and how a person takes it back.
        block = mm.role_change_block(mesh, w, "worker", [])
        assert "granted by the daemon: merger" in block
        assert REASON in block
        assert "claunch mesh subroles m worker2 --remove merger" in block
        # A leader already holds it: nothing to grant.
        assert mm.grant_auto_subrole("s1", "merger", REASON) == []
        # Held already: nothing again.
        assert mm.grant_auto_subrole("s2", "merger", REASON) == []

        # A person removes it: it stays removed.
        mm.set_subroles("m", "worker2", remove=["merger"])
        assert (w.subroles, w.auto_subroles, w.declined) == ([], {}, ["merger"])
        assert mm.grant_auto_subrole("s2", "merger", REASON) == []
        assert w.subroles == []
        # The removal is on the persisted record and the session's view.
        row = next(r for r in mm.meshes_for_session("s2") if r["mesh"] == "m")
        assert row["declined"] == ["merger"] and row["auto_subroles"] == {}

        # A person adding it back clears the removal, and it is theirs now.
        mm.set_subroles("m", "worker2", add=["merger"])
        assert (w.subroles, w.auto_subroles, w.declined) == (["merger"], {}, [])

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_clock_grants_merger_to_a_session_driving_a_landing_queue(
    home, tmp_path, monkeypatch
):
    _register_py_harness()
    queue_cwd = str(tmp_path / "repo")

    def fake_holders(manager, cwds):
        def holders(session, repo):
            if session == "s2":
                return [landing_route.Holder(queue_cwd, "stack", False)]
            return []
        return holders

    monkeypatch.setattr(cflow2_triggers, "landing_holders", fake_holders)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        await mm.join("m", "s1", handle="lead1")
        await mm.join("m", "s2", handle="worker2")
        await mm.join("m", "s3", handle="worker3")
        clock = TriggerClock(mgr, mm)

        granted = clock.grant_mergers(now=100.0)
        assert granted == [("s2", "m", "worker2")]
        members = mm.get("m").members
        assert members["worker2"].subroles == ["merger"]
        assert "stack" in members["worker2"].auto_subroles["merger"]
        assert queue_cwd in members["worker2"].auto_subroles["merger"]
        assert members["worker3"].subroles == []
        # At most once a minute.
        mm.set_subroles("m", "worker2", remove=["merger"])
        assert clock.grant_mergers(now=130.0) == []
        # And a person's removal is not undone on the next pass.
        assert clock.grant_mergers(now=200.0) == []
        assert members["worker2"].subroles == []

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_clock_without_a_mesh_grants_nothing():
    assert TriggerClock(SimpleNamespace(list=lambda: []), None).grant_mergers(now=0.0) == []


def test_merger_role_reads_only_this_daemons_member_of_a_session_name():
    # claunch-nie6o item 2: a mirrored member on another machine can carry
    # the same session name; its roles do not speak for ours.
    remote = SimpleNamespace(session="p1", roles=["worker", "merger"], machine="far")
    local = SimpleNamespace(session="p1", roles=["worker"], machine="")
    mesh_obj = SimpleNamespace(members={"a": remote, "b": local})

    class Mesh:
        def meshes_for_session(self, scope):
            return [{"mesh": "m"}]

        def get(self, name):
            return mesh_obj

        def is_local_member(self, mesh, m):
            return not m.machine

    router = landing_route.Router(None, Mesh(), lambda s, r: [])
    assert "holds no 'merger' role" in router.merger_role("w1", "p1")
    local.roles = ["worker", "merger"]
    assert router.merger_role("w1", "p1") == ""
