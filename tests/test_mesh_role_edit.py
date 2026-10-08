"""Editing a live member's roles: the primary as well as the subroles.

Scenario matrix (claunch-a6azt):

A. The edit
   A1 `set_member_roles` replaces the primary; the new primary leaves the
      subroles and the old one is kept only when named among them
   A2 an exclusive role is refused while somebody ELSE holds it live, and the
      member keeps an exclusive role it already holds; a refused or unknown
      edit leaves the member exactly as it was
   A3 `set_subroles` is the same edit with the primary left alone

B. What the member is told
   B1 a primary change queues a notice carrying the new stance and its text id
   B2 a subrole-only change queues a notice without the primary's stance
      (an added subrole's own stance rides it — claunch-bwukr)
   B3 an edit that changes nothing queues nothing

C. The lookups follow
   C1 the session reminder's Role entry names the new primary and the
      subroles, and its key moves when only the subroles do
   C2 auto_link edges the new roles earn are opened; a pair somebody
      disconnected stays disconnected

D. The HTTP surface
   D1 PATCH /roles takes `role` with `set`/`add`/`remove`; an empty body and
      a conflict are refused
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import digests
from claude_launcher.daemon import session_reminder
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshConflict, MeshError, MeshManager

from test_mesh_graph import _register_py_harness
from test_spawn_api import BEARER, _serve


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _capture(mgr, name):
    """Record what the daemon queues for ``name``'s terminal."""
    sent = []
    session = mgr.get(name)
    session.queue_delivery = lambda text: sent.append(text) or True
    return sent


async def _setup(tmp_path, names=("s1", "s2")):
    _register_py_harness()
    mgr = _manager()
    mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
    for s in names:
        mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
    mm.create("m")
    return mgr, mm, mm.get("m")


# --------------------------------------------------------------------------- #
# A. the edit
# --------------------------------------------------------------------------- #
def test_the_primary_moves_and_the_subroles_are_settled_around_it(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="worker1", subroles=["reviewer"])

        # A1: the new primary leaves the subroles; the old one is dropped.
        m = mm.set_member_roles("m", "worker1", role="reviewer")
        assert (m.role, m.subroles) == ("reviewer", [])
        # ...and kept when named, with an alias resolved like a join's.
        # A leader is given `merger` (claunch-lhgfx).
        m = mm.set_member_roles("m", "worker1", role="mod", add=["reviewer"])
        assert (m.role, m.subroles) == ("leader", ["reviewer", "merger"])
        assert mesh.members["worker1"].roles == ["leader", "reviewer", "merger"]
        # A3: set_subroles leaves the primary where it is.
        m = mm.set_subroles("m", "worker1", replace=["worker"])
        assert (m.role, m.subroles) == ("leader", ["worker", "merger"])
        # Persisted with the roster: a fresh manager reads the same record.
        again = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        again.load_all()
        assert again.get("m").members["worker1"].roles == ["leader", "worker", "merger"]

        await mgr.shutdown_all()

    asyncio.run(run())


def test_an_exclusive_primary_is_refused_while_somebody_else_holds_it(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="lead1")
        await mm.join("m", "s2", handle="worker2", subroles=["pm"])

        # A2: a live leader blocks a second one...
        with pytest.raises(MeshConflict, match="lead1"):
            mm.set_member_roles("m", "worker2", role="leader")
        with pytest.raises(MeshError, match="unknown role"):
            mm.set_member_roles("m", "worker2", role="ghost")
        with pytest.raises(MeshError, match="unknown subrole"):
            mm.set_member_roles("m", "worker2", role="reviewer", add=["ghost"])
        # ...and every refusal leaves the member as it was.
        assert mesh.members["worker2"].roles == ["worker", "pm"]
        # The holder itself keeps it through an edit.
        m = mm.set_member_roles("m", "lead1", role="leader", add=["reviewer"])
        assert m.roles == ["leader", "merger", "reviewer"]
        # Once it lets go, the role is free to move (merger is not exclusive
        # and stays with whoever was given it).
        m = mm.set_member_roles("m", "lead1", role="worker")
        assert m.roles == ["worker", "merger", "reviewer"]
        m = mm.set_member_roles("m", "worker2", role="leader")
        assert m.roles == ["leader", "pm", "merger"]

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# B. what the member is told
# --------------------------------------------------------------------------- #
def test_the_member_is_typed_a_notice_with_the_new_stance(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker")
        sent = _capture(mgr, "s1")

        # B1: the primary moved, so the new stance rides with its id.
        mm.set_member_roles("m", "w1", role="leader")
        assert len(sent) == 1
        notice = sent[0]
        assert "role change -- machine-generated" in notice
        assert "roles: worker -> leader" in notice
        assert "primary role: worker -> leader" in notice
        stance = mesh.roleset.get("leader").stance.strip()
        assert stance and stance[:60] in notice
        assert f"[text id: {digests.text_digest(stance)}]" in notice

        # B2: subroles only — the primary's stance is not repeated, the
        # change is spelt out, and the added subrole's stance rides with its
        # id (claunch-bwukr; tests/test_subrole_stance.py).
        mm.set_member_roles("m", "w1", add=["reviewer"])
        assert len(sent) == 2
        assert "subroles added: reviewer" in sent[1]
        assert "stance (leader)" not in sent[1]
        assert "stance (subrole reviewer)" in sent[1]
        mm.set_subroles("m", "w1", remove=["reviewer"])
        assert "subroles removed: reviewer" in sent[2]

        # B3: nothing changed, nothing typed.
        mm.set_member_roles("m", "w1", role="leader", replace=[])
        assert len(sent) == 3

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. the lookups follow
# --------------------------------------------------------------------------- #
def test_the_role_reminder_follows_the_edit(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path)
        await mm.join("m", "s1", handle="w1", role="worker")
        _capture(mgr, "s1")

        before = session_reminder.role_entries("s1", mgr, mm)
        assert [e["name"] for e in before] == ["worker"]

        mm.set_member_roles("m", "w1", role="leader")
        after = session_reminder.role_entries("s1", mgr, mm)
        assert [e["name"] for e in after] == ["leader"]
        assert after[0]["digest"] == digests.text_digest(
            mesh.roleset.get("leader").stance.strip()
        )
        assert session_reminder.role_key(after) != session_reminder.role_key(before)

        # C1: a subrole-only edit is a new position too, and the section says it.
        mm.set_member_roles("m", "w1", add=["reviewer"])
        subbed = session_reminder.role_entries("s1", mgr, mm)
        assert subbed[0]["subroles"] == ["merger", "reviewer"]
        assert session_reminder.role_key(subbed) != session_reminder.role_key(after)
        lines = session_reminder.role_section(subbed, cflow_guidance=False)
        assert any(line.startswith("subroles: merger, reviewer") for line in lines)

        await mgr.shutdown_all()

    asyncio.run(run())


def test_auto_link_edges_follow_the_new_roles(home, tmp_path):
    async def run():
        mgr, mm, mesh = await _setup(tmp_path, names=("s1", "s2", "s3"))
        await mm.set_roles("m", """
            auto_link:
              rules:
                - between: [{role: reviewer}, {role: worker}]
        """)
        await mm.join("m", "s2", handle="worker2")
        await mm.join("m", "s3", handle="worker3")
        await mm.join("m", "s1", handle="lead1")
        assert not mesh.connected("lead1", "worker2")
        # Somebody closed this pair on purpose.
        await mm.set_member_link("m", "lead1", "worker3", enabled=False)
        sent = _capture(mgr, "s1")

        # C2: holding reviewer now earns the worker edge...
        mm.set_member_roles("m", "lead1", add=["reviewer"])
        assert mesh.connected("lead1", "worker2")
        # ...but does not reopen a pair that was closed by hand.
        assert not mesh.connected("lead1", "worker3")
        assert "connected: worker2" in sent[0]
        # Dropping the role keeps the edge: nothing is cut mid-conversation.
        mm.set_member_roles("m", "lead1", replace=[])
        assert mesh.connected("lead1", "worker2")

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. the HTTP surface
# --------------------------------------------------------------------------- #
def test_the_roles_endpoint_edits_the_primary_and_the_subroles(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            await client.post("/api/mesh", json={"name": "web"}, headers=BEARER)
            for s in ("l1", "w1"):
                mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
            await client.post(
                "/api/mesh/web/members",
                json={"session": "l1", "handle": "lead1"}, headers=BEARER,
            )
            await client.post(
                "/api/mesh/web/members",
                json={"session": "w1", "handle": "w1", "role": "worker"},
                headers=BEARER,
            )
            _capture(mgr, "w1")

            resp = await client.patch(
                "/api/mesh/web/members/w1/roles",
                json={"role": "reviewer", "set": ["pm", "worker"]},
                headers=BEARER,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["role"] == "reviewer"
            assert body["roles"] == ["reviewer", "pm", "worker"]

            resp = await client.patch(
                "/api/mesh/web/members/w1/roles",
                json={"remove": ["pm"]}, headers=BEARER,
            )
            assert (await resp.json())["roles"] == ["reviewer", "worker"]

            resp = await client.patch(
                "/api/mesh/web/members/w1/roles", json={}, headers=BEARER,
            )
            assert resp.status == 400
            resp = await client.patch(
                "/api/mesh/web/members/w1/roles",
                json={"role": "leader"}, headers=BEARER,
            )
            assert resp.status == 409
            info = await (await client.get("/api/mesh/web", headers=BEARER)).json()
            rows = {m["handle"]: m for m in info["members"]}
            assert rows["w1"]["roles"] == ["reviewer", "worker"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
