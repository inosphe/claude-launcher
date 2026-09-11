"""Subroles: a member holds its primary role AND further roles it answers for.

Scenario matrix (claunch-tnf8):

A. The record
   A1 `Member.subroles` rides to_dict/from_dict; a record with no key reads
      as the primary role alone
   A2 `roles` is the primary first, then the subroles; the primary is never
      repeated; names are lower-cased and deduplicated

B. The join
   B1 subroles resolve through the mesh vocabulary (alias -> canonical)
   B2 an unknown subrole refuses the join like an unknown primary
   B3 an exclusive role is exclusive however it is held: a live leader
      blocks `--subrole leader`, and a leader-by-subrole blocks a leader join
   B4 the packaged auto_link rule "every reviewer to every worker" wires a
      leader that holds `reviewer` as a subrole to the workers

C. Editing after the join
   C1 `set_subroles` adds, removes and replaces on a live member; the
      primary stays; keeping a subrole already held is not a second holder
   C2 the HTTP surface: POST members with subroles, PATCH subroles, the
      roster's `roles`/`subroles`, and `role` in the PATCH body is a 400

D. The lookups that key off a role read `roles`
   D1 roles_view lists a subrole holder under that role; an orphan subrole
      is reported
   D2 the policy engine's stall watchers and polled roles
   D3 a delegated decision `from: [{role: reviewer}]` asks a leader that
      holds reviewer as a subrole
   D4 `filter_roles`: a whitelist admits a driver holding the role as a
      subrole; a blacklist turns one away

E. Spawn / onboarding
   E1 a spawn with `subroles` seats the child with them; an unknown one is
      refused before anything is built
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher.cflow import engine, model, responders, state as state_mod
from claude_launcher.cflow.engine import CflowError
from claude_launcher.daemon import mesh_roles
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import Member, MeshConflict, MeshError, MeshManager

from test_cflow import (  # noqa: F401 — flow_dir is a fixture
    ASK_TWO_GROUPS, WORKER_ONLY, _driving_session, _member, _roster, _write,
    flow_dir,
)
from test_mesh_graph import _register_py_harness
from test_spawn_api import BEARER, _serve


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


# --------------------------------------------------------------------------- #
# A. the record
# --------------------------------------------------------------------------- #
def test_subroles_ride_the_record_and_an_old_record_reads_as_primary_alone():
    m = Member("lead1", "s1", role="leader", subroles=["Reviewer", "leader", "worker", "reviewer"])
    # A2: lower-cased, deduplicated, the primary never repeated.
    assert m.subroles == ["reviewer", "worker"]
    assert m.roles == ["leader", "reviewer", "worker"]
    assert m.holds("reviewer") and m.holds("LEADER") and not m.holds("pm")
    assert m.role_label() == "leader+reviewer+worker"

    # A1: the round trip keeps them...
    doc = m.to_dict()
    assert doc["subroles"] == ["reviewer", "worker"]
    assert Member.from_dict(doc).roles == m.roles
    # ...and a record from a daemon that predates the key is the primary alone.
    doc.pop("subroles")
    old = Member.from_dict(doc)
    assert old.subroles == [] and old.roles == ["leader"]
    # A hand-typed comma string is accepted where a list is expected.
    assert Member.from_dict({**doc, "subroles": "worker, pm"}).subroles == ["worker", "pm"]


# --------------------------------------------------------------------------- #
# B. the join
# --------------------------------------------------------------------------- #
def test_a_join_resolves_subroles_and_refuses_an_unknown_one(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")

        # B1: the alias `qa` stores `reviewer`; `lead` (an alias of the
        # primary) is dropped rather than stored twice.
        member = await mm.join("m", "s1", handle="lead1", subroles=["qa", "lead"])
        assert member.role == "leader"
        assert member.subroles == ["reviewer"]
        assert mesh.members["lead1"].roles == ["leader", "reviewer"]

        # B2: an unknown subrole refuses the join, naming the vocabulary.
        with pytest.raises(MeshError, match="unknown subrole 'ghost'"):
            await mm.join("m", "s2", handle="worker2", subroles=["ghost"])
        assert "worker2" not in mesh.members

        await mgr.shutdown_all()

    asyncio.run(run())


def test_an_exclusive_role_is_exclusive_however_it_is_held(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")

        # B3, first direction: a live leader blocks `leader` as a subrole.
        await mm.join("m", "s1", handle="lead1")
        with pytest.raises(MeshConflict, match="cannot be taken as a subrole"):
            await mm.join("m", "s2", handle="worker2", subroles=["leader"])
        assert "worker2" not in mesh.members
        await mm.leave("m", "lead1")

        # B3, second direction: a worker holding `leader` as a subrole is
        # the live holder, so a leader join is refused and that worker named.
        await mm.join("m", "s2", handle="worker2", subroles=["leader"])
        assert mesh.members["worker2"].roles == ["worker", "leader"]
        with pytest.raises(MeshConflict) as exc:
            await mm.join("m", "s3", handle="lead3")
        assert "worker2" in str(exc.value)
        # The preflight question session creation asks answers the same.
        assert mm.exclusive_holder(mesh, "lead3", "").handle == "worker2"
        assert mm.exclusive_holder(mesh, "x", "worker", ["leader"]).handle == "worker2"
        assert mm.exclusive_holder(mesh, "x", "worker", ["reviewer"]) is None

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_packaged_reviewer_rule_wires_a_leader_holding_reviewer_as_a_subrole(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2", "s3"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")

        # Only the reviewer<->worker rule, so the packaged root<->root rule
        # cannot wire these three roots for a reason that is not under test.
        await mm.set_roles("m", """
            auto_link:
              rules:
                - between: [{role: reviewer}, {role: worker}]
        """)
        # Control: a plain leader is not wired to a worker by that rule.
        await mm.join("m", "s2", handle="worker2")
        await mm.join("m", "s1", handle="lead1")
        assert not mesh.connected("lead1", "worker2")
        await mm.leave("m", "lead1")
        # B4: the reviewer rule reaches the leader THROUGH its subrole, and
        # it holds whichever end joined first.
        await mm.join("m", "s1", handle="lead1", subroles=["reviewer"])
        await mm.join("m", "s3", handle="worker3")
        assert mesh.connected("lead1", "worker2")
        assert mesh.connected("lead1", "worker3")

        # The rule itself, at the unit: a pattern naming a role matches a
        # member holding it in any position.
        pat = mesh_roles.LinkPattern(role="reviewer")
        assert pat.matches(mesh_roles.LinkFacts(
            role="leader", tier=0, root="x", roles=("leader", "reviewer")
        ))
        assert not pat.matches(mesh_roles.LinkFacts(role="leader", tier=0, root="x"))

        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C. editing after the join
# --------------------------------------------------------------------------- #
def test_set_subroles_edits_a_live_member_and_leaves_the_primary_alone(
    home, tmp_path
):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")
        await mm.join("m", "s1", handle="lead1")
        await mm.join("m", "s2", handle="worker2")

        # C1: add, then remove, then replace — the primary never moves.
        m = mm.set_subroles("m", "lead1", add=["qa"])
        assert (m.role, m.subroles) == ("leader", ["reviewer"])
        m = mm.set_subroles("m", "lead1", add=["worker", "reviewer"])
        assert m.subroles == ["reviewer", "worker"]  # keeping one is not a second holder
        m = mm.set_subroles("m", "lead1", remove=["qa"])
        assert m.subroles == ["worker"]
        m = mm.set_subroles("m", "lead1", replace=["pm"])
        assert m.subroles == ["pm"]
        m = mm.set_subroles("m", "lead1", replace=[])
        assert m.subroles == []
        # The edit is persisted with the roster.
        assert mesh.members["lead1"].subroles == []

        # An exclusive role held live elsewhere is refused here too, and a
        # refused edit leaves the member as it was.
        mm.set_subroles("m", "lead1", add=["reviewer"])
        with pytest.raises(MeshConflict):
            mm.set_subroles("m", "worker2", add=["leader"])
        assert mesh.members["worker2"].subroles == []
        with pytest.raises(MeshError, match="unknown subrole"):
            mm.set_subroles("m", "worker2", add=["ghost"])
        with pytest.raises(MeshError, match="no member"):
            mm.set_subroles("m", "nobody", add=["reviewer"])

        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_http_surface_carries_subroles(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            await client.post("/api/mesh", json={"name": "web"}, headers=BEARER)
            for s in ("l1", "w1"):
                mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
            # C2: the join body takes a list...
            resp = await client.post(
                "/api/mesh/web/members",
                json={"session": "l1", "handle": "lead1", "subroles": ["qa"]},
                headers=BEARER,
            )
            assert resp.status == 201
            body = await resp.json()
            assert body["role"] == "leader" and body["subroles"] == ["reviewer"]
            # ...or one comma-separated string from a hand-typed call.
            resp = await client.post(
                "/api/mesh/web/members",
                json={"session": "w1", "handle": "w1", "subroles": "pm, analyst"},
                headers=BEARER,
            )
            assert (await resp.json())["subroles"] == ["pm", "analyst"]

            # The roster publishes both the primary and the whole set.
            info = await (await client.get("/api/mesh/web", headers=BEARER)).json()
            rows = {m["handle"]: m for m in info["members"]}
            assert rows["lead1"]["roles"] == ["leader", "reviewer"]
            assert rows["lead1"]["subroles"] == ["reviewer"]
            assert rows["w1"]["role"] == "free-role"  # "w1" infers no role

            # PATCH edits; the primary is refused with a pointer to the fields.
            resp = await client.patch(
                "/api/mesh/web/members/w1/subroles",
                json={"remove": ["pm"], "add": ["reviewer"]}, headers=BEARER,
            )
            assert resp.status == 200
            assert (await resp.json())["subroles"] == ["analyst", "reviewer"]
            resp = await client.patch(
                "/api/mesh/web/members/w1/subroles",
                json={"set": []}, headers=BEARER,
            )
            assert (await resp.json())["subroles"] == []
            resp = await client.patch(
                "/api/mesh/web/members/w1/subroles",
                json={"role": "leader"}, headers=BEARER,
            )
            assert resp.status == 400
            assert "settled at join" in (await resp.json())["error"]
            resp = await client.patch(
                "/api/mesh/web/members/w1/subroles",
                json={"add": ["ghost"]}, headers=BEARER,
            )
            assert resp.status == 400

            # D1: the vocabulary view counts a subrole holder under that role.
            doc = await (await client.get("/api/mesh/web/roles", headers=BEARER)).json()
            by_name = {r["name"]: r for r in doc["roles"]}
            assert by_name["reviewer"]["members"] == ["lead1"]
            assert by_name["leader"]["members"] == ["lead1"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# D. the lookups
# --------------------------------------------------------------------------- #
def test_roles_view_reports_an_orphan_subrole(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
        mm.create("m")
        await mm.join("m", "s1", handle="lead1", subroles=["reviewer"])
        # The vocabulary drops reviewer after the join; the member keeps it
        # (uploads are not retroactive) and the view says so.
        await mm.set_roles("m", "roles: {reviewer: null}\n")
        view = mm.roles_view("m")
        assert view["orphans"] == ["reviewer"]
        assert "reviewer" not in {r["name"] for r in view["roles"]}
        await mgr.shutdown_all()

    asyncio.run(run())


def test_the_policy_engine_reads_every_role_a_member_holds(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, settle=0.05, root=tmp_path / "mesh")
        for s in ("s1", "s2"):
            mgr.create(SessionDef(name=s, harness="py", cwd=str(tmp_path)))
        mm.create("m")
        mesh = mm.get("m")
        # A worker that took `leader` as a subrole is a stall watcher; a
        # leader that took `worker` is polled for tasks, in the worker's words.
        await mm.join("m", "s1", handle="w1", subroles=["leader"])
        await mm.leave("m", "w1")
        await mm.join("m", "s1", handle="lead1", subroles=["worker"])
        await mm.join("m", "s2", handle="worker2")

        # D2: the derivations the tick makes, on the roster it reads.
        watching = set(mesh.roleset.stall_watchers())
        assert sorted(
            h for h, m in mesh.members.items() if watching.intersection(m.roles)
        ) == ["lead1"]
        tp = mesh.policy["task_poll"]
        polled = {
            h: next((r for r in m.roles if r in tp["roles"]), "")
            for h, m in mesh.members.items()
        }
        assert polled == {"lead1": "worker", "worker2": "worker"}

        await mgr.shutdown_all()

    asyncio.run(run())


def _team(monkeypatch, driver_roles=("worker",)):
    """A roster read for the cflow engine: dev1 drives, lead1 is above it and
    holds reviewer as a SUBROLE, wired to dev1."""
    leader = _member("lead1", "lead1", "leader")
    leader["subroles"] = ["reviewer"]
    leader["roles"] = ["leader", "reviewer"]
    driver = _member("dev1", "driver", driver_roles[0], parent="lead1")
    driver["roles"] = list(driver_roles)
    _roster(
        monkeypatch,
        {
            "name": "team",
            "members": [driver, leader],
            "member_links": [{"a": "dev1", "b": "lead1", "enabled": True}],
        },
    )
    monkeypatch.setattr(responders, "deliver", lambda ask, **kw: None)


def test_a_delegated_decision_finds_a_reviewer_held_as_a_subrole(
    flow_dir, monkeypatch
):
    _driving_session(monkeypatch)
    _team(monkeypatch)
    _write(flow_dir, "two", ASK_TWO_GROUPS)
    engine.start("two")
    payload = engine.status()
    # D3: the FIRST group (reviewer) matched — the leader was not reached as
    # a leader in the second group, it answered as the reviewer it also is.
    assert [e["handle"] for e in payload["ask"]["asked"]] == ["lead1"]
    assert payload["ask"]["asked"][0]["roles"] == ["leader", "reviewer"]
    engine.answer(payload["ask"]["id"], "approve", "read it", by_session="lead1")
    assert engine.next_step()["status"] == "step"


def test_a_pool_reads_a_roster_that_publishes_no_roles_list(monkeypatch):
    # A daemon that predates subroles publishes `role` alone; the pool
    # reads that as the one role held.
    _roster(monkeypatch, {
        "name": "team",
        "members": [
            _member("dev1", "driver", "worker"),
            _member("rev1", "rev1", "reviewer"),
        ],
        "member_links": [{"a": "dev1", "b": "rev1", "enabled": True}],
    })
    pool = responders.pool(session="driver")
    assert pool.me_roles == ("worker",)
    assert pool.members["rev1"].held == ("reviewer",)
    assert [m.handle for m in pool.eligible(model.Candidate(role="reviewer"))] == ["rev1"]


def test_filter_roles_is_held_against_every_role_the_driver_holds(
    flow_dir, monkeypatch
):
    # D4: dev1 is a leader that took `worker` as a subrole.
    _driving_session(monkeypatch)
    _team(monkeypatch, driver_roles=("leader", "worker"))
    _write(flow_dir, "worker-only", WORKER_ONLY)
    payload = engine.start("worker-only")
    assert "role_filter" not in payload  # admitted through the subrole
    assert engine.status()["status"] != "idle"


def test_a_blacklist_turns_away_a_role_held_as_a_subrole(flow_dir, monkeypatch):
    _driving_session(monkeypatch)
    _team(monkeypatch, driver_roles=("leader", "worker"))
    _write(flow_dir, "no-workers", WORKER_ONLY.replace("whitelist", "blacklist"))
    with pytest.raises(CflowError, match="may not drive"):
        engine.start("no-workers")
    assert engine.status()["status"] == "idle"


def test_role_filter_allows_any_reads_the_whole_set():
    white = model.RoleFilter(type=model.FILTER_WHITELIST, roles=("worker",))
    black = model.RoleFilter(type=model.FILTER_BLACKLIST, roles=("worker",))
    assert white.allows_any(("leader", "worker"))
    assert not white.allows_any(("leader", "reviewer"))
    assert not white.allows_any(())  # no role held = not a listed one
    assert not black.allows_any(("leader", "worker"))
    assert black.allows_any(("leader",))
    assert black.allows_any(())


# --------------------------------------------------------------------------- #
# E. spawn / onboarding
# --------------------------------------------------------------------------- #
def test_a_spawn_seats_the_child_with_its_subroles(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mm.create("team")
            mgr.create(SessionDef(name="lead", harness="py", cwd=str(tmp_path)))
            await mm.join("team", "lead", handle="lead")

            # E1, the refusal first: nothing is built for an unknown subrole.
            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "kid0", "mesh": "team", "handle": "w0",
                      "role": "worker", "subroles": ["ghost"]},
                headers=BEARER,
            )
            assert resp.status == 400
            assert "unknown subrole 'ghost'" in (await resp.json())["error"]
            assert "w0" not in mm.get("team").members

            resp = await client.post(
                "/api/sessions/lead/children",
                json={"name": "kid1", "mesh": "team", "handle": "w1",
                      "role": "worker", "subroles": ["qa"]},
                headers=BEARER,
            )
            assert resp.status == 201, await resp.text()
            body = await resp.json()
            assert body["mesh"]["role"] == "worker"
            assert body["mesh"]["subroles"] == ["reviewer"]
            assert mm.get("team").members["w1"].roles == ["worker", "reviewer"]
            # The spawn's `subroles` never enters the session definition.
            assert "subroles" not in body["session"]

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
