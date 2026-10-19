"""An agent's request for a session, gated on a person (claunch-iw5i8.15).

The user's decisions (thread th-d9e343, 2026-10-18):

1. a request for a new session waits 5 minutes; no answer counts as approval;
2. a request to resume an exited session waits 5 minutes; no answer means it
   is NOT resumed and a new session is asked for instead;
5. the card's answers are approve / manual / reject, with the time left and
   an extension.

What these pin:

* filing creates nothing; approve creates through the create path and tells
  the asker the new name; a failed create settles ``failed``;
* reject creates nothing; manual settles the request, and the form's create
  links its session back; the deadline counts as approval;
* extend moves the deadline out and keeps the request pending, as often as
  pressed;
* resume: approve respawns; "spawn instead" and the deadline file the
  fallback as a spawn request and leave the target exited; reject does
  neither; a target back by other means closes it as moot;
* pending requests survive a daemon restart and are re-armed with a grace;
* the HTTP routes carry all of it.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher.daemon import spawn_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}
SPEC = {"profile": "p", "project": "alpha", "task": "do the thing",
        "workflow": "improv-worker-v2", "worktree": ""}


class _FakeSession:
    def __init__(self, name: str, *, exited: bool = False):
        self.sdef = SessionDef(name=name, harness="py", cwd=f"/w/{name}")
        self.created_at = "2020-01-01T00:00:00+00:00"
        self.exited = exited
        self.exit_code = 0 if exited else None
        self.pid = None
        self.last_output_at = None
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = "2026-10-18T00:00:00+00:00" if exited else None
        self.archived_at = None
        self.held_at = None
        self.delivered = []

    def status(self):
        return "exited" if self.exited else "idle"

    def delivery_held(self):
        return False

    def queue_delivery(self, message):
        self.delivered.append(message)
        return True

    async def shutdown(self):
        self.exited = True


def _manager(*sessions) -> SessionManager:
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    for i, session in enumerate(sessions):
        session.created_at = "2020-01-01T00:00:%02d+00:00" % i
        mgr._sessions[session.sdef.name] = session
    return mgr


class _World:
    def __init__(self, tmp_path, *, timeout=spawn_gate.GATE_TIMEOUT, fail="", path=True):
        self.mgr = _manager(_FakeSession("op"), _FakeSession("old", exited=True))
        self.created = []
        self.respawned = []
        self.notes = []
        self.settled = []
        self.fail = fail

        async def create(spec, asker, record):
            self.created.append((spec, asker, record["id"]))
            if self.fail:
                return 400, {"error": self.fail}
            return 201, {"name": f"new{len(self.created)}"}

        async def respawn_async(name):
            self.respawned.append(name)
            self.mgr._sessions[name].exited = False

        self.mgr.respawn_async = respawn_async
        self.path = tmp_path / spawn_gate.FILE if path else None
        self.gate = spawn_gate.SpawnGate(
            manager=self.mgr, path=self.path, create=create, timeout=timeout,
            notify=lambda name, message: self.notes.append((name, message)) or True,
            on_settle=self.settled.append)


def _spawn(world, **kw):
    return world.gate.submit_spawn(session="op", spec=dict(SPEC), reason="no one does it",
                                   basis={"operator": "abc"}, **kw)


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
def test_filing_creates_nothing_and_sets_a_five_minute_deadline(home, tmp_path):
    world = _World(tmp_path)
    record = _spawn(world)
    assert record["status"] == "pending" and record["kind"] == "spawn"
    asked = datetime.fromisoformat(record["requested_at"])
    assert datetime.fromisoformat(record["deadline"]) - asked == timedelta(seconds=300)
    assert world.created == [] and world.notes == []


@pytest.mark.parametrize("spec,reason", [({}, "r"), (SPEC, ""), (SPEC, "   ")])
def test_a_request_needs_a_spec_and_a_reason(home, tmp_path, spec, reason):
    world = _World(tmp_path)
    with pytest.raises(spawn_gate.GateRefused) as err:
        world.gate.submit_spawn(session="op", spec=spec, reason=reason)
    assert err.value.status == 400


def test_approve_creates_and_tells_the_asker(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    record = asyncio.run(world.gate.approve(rid, decided_by="web"))
    assert record["status"] == "approved" and record["created"] == "new1"
    assert world.created == [(SPEC, "op", rid)]
    assert world.notes and world.notes[0][0] == "op" and "new1" in world.notes[0][1]
    assert [r["id"] for r in world.settled] == [rid]
    with pytest.raises(spawn_gate.GateRefused):
        asyncio.run(world.gate.approve(rid))   # nothing pending any more
    assert len(world.created) == 1


def test_a_failed_create_is_reported_as_failed(home, tmp_path):
    world = _World(tmp_path, fail="a session needs profile")
    rid = _spawn(world)["id"]
    record = asyncio.run(world.gate.approve(rid))
    assert record["status"] == "failed" and record["error"] == "a session needs profile"
    assert "create failed" in world.notes[0][1]


def test_reject_creates_nothing(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    assert world.gate.reject(rid)["status"] == "rejected"
    assert world.created == [] and "rejected" in world.notes[0][1]


def test_manual_settles_and_the_form_links_its_session(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    record = world.gate.manual(rid)
    assert record["status"] == "manual" and record["spec"] == SPEC
    assert world.created == []
    assert "by hand" in world.notes[-1][1]
    linked = world.gate.link(rid, "hand-made")
    assert linked["created"] == "hand-made" and "hand-made" in world.notes[-1][1]
    with pytest.raises(spawn_gate.GateRefused):
        world.gate.link(rid, "again")


def test_the_deadline_counts_as_approval(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = _spawn(world)["id"]
        await asyncio.sleep(0.3)
        return world, rid

    world, rid = asyncio.run(run())
    record = world.gate.get(rid)
    assert record["status"] == "approved" and record["decided_by"] == "timeout"
    assert record["created"] == "new1"


def test_extend_moves_the_deadline_and_keeps_it_pending(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.1)
        record = _spawn(world)
        first = datetime.fromisoformat(record["deadline"])
        once = world.gate.extend(record["id"])
        twice = world.gate.extend(record["id"])
        await asyncio.sleep(0.3)
        return world, record["id"], first, once, twice

    world, rid, first, once, twice = asyncio.run(run())
    assert twice["extensions"] == 2
    assert datetime.fromisoformat(twice["deadline"]) - first >= timedelta(seconds=599)
    assert world.gate.get(rid)["status"] == "pending"     # the old timer did not fire
    assert world.created == []


def test_only_the_asker_withdraws(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    with pytest.raises(spawn_gate.GateRefused) as err:
        world.gate.withdraw(rid, session="someone")
    assert err.value.status == 403
    assert world.gate.withdraw(rid, session="op")["status"] == "withdrawn"


def test_one_asker_holds_a_bounded_number(home, tmp_path):
    world = _World(tmp_path)
    for _ in range(spawn_gate.MAX_PENDING_PER_ASKER):
        _spawn(world)
    with pytest.raises(spawn_gate.GateRefused) as err:
        _spawn(world)
    assert err.value.status == 409


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #
def _resume(world, fallback=SPEC):
    return world.gate.submit_resume(session="op", target="old", reason="it did this work",
                                    fallback=dict(fallback) if fallback else None,
                                    basis={"operator": "abc"})


def test_resume_approve_respawns(home, tmp_path):
    world = _World(tmp_path)
    rid = _resume(world)["id"]
    record = asyncio.run(world.gate.approve(rid))
    assert record["status"] == "approved" and world.respawned == ["old"]
    assert world.created == []


def test_resume_refuses_a_running_or_unknown_target_and_a_second_ask(home, tmp_path):
    world = _World(tmp_path)
    for target, status in (("op", 409), ("nope", 400)):
        with pytest.raises(spawn_gate.GateRefused) as err:
            world.gate.submit_resume(session="op", target=target, reason="r", fallback=SPEC)
        assert err.value.status == status
    _resume(world)
    with pytest.raises(spawn_gate.GateRefused) as err:
        _resume(world)
    assert err.value.status == 409


def test_resume_spawn_instead_files_the_fallback(home, tmp_path):
    world = _World(tmp_path)
    rid = _resume(world)["id"]
    record = world.gate.spawn_instead(rid)
    assert record["status"] == "spawned" and world.respawned == []
    follow = world.gate.get(record["followup"])
    assert follow["kind"] == "spawn" and follow["status"] == "pending"
    assert follow["spec"] == SPEC and follow["origin"] == rid
    assert follow["basis"] == {"operator": "abc"}


def test_resume_deadline_does_not_resume_and_asks_for_a_new_session(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = _resume(world)["id"]
        await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)     # the follow-up spawn has its own deadline
        mid = world.gate.get(rid)
        return world, mid, world.gate.get(mid["followup"])

    world, resume, follow = asyncio.run(run())
    assert resume["status"] == "expired" and resume["decided_by"] == "timeout"
    assert world.respawned == [] and world.mgr._sessions["old"].exited
    assert follow["kind"] == "spawn" and follow["origin"] == resume["id"]
    assert follow["status"] == "approved" and follow["created"] == "new1"


def test_resume_reject_does_neither(home, tmp_path):
    world = _World(tmp_path)
    rid = _resume(world)["id"]
    assert world.gate.reject(rid)["status"] == "rejected"
    assert world.respawned == [] and world.created == []
    assert len(world.gate.list()) == 1


def test_resume_target_back_by_other_means_is_moot(home, tmp_path):
    world = _World(tmp_path)
    rid = _resume(world)["id"]
    world.mgr._sessions["old"].exited = False
    assert world.gate.get(rid)["status"] == "moot"
    assert "running again" in world.notes[-1][1]


def test_manual_is_for_spawn_requests_only(home, tmp_path):
    world = _World(tmp_path)
    rid = _resume(world)["id"]
    with pytest.raises(spawn_gate.GateRefused):
        world.gate.manual(rid)


# --------------------------------------------------------------------------- #
# a daemon restart
# --------------------------------------------------------------------------- #
def test_a_pending_request_outlives_the_daemon_and_gets_a_grace(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    # The deadline passed while the daemon was down.
    world.gate.records[rid]["deadline"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    world.gate._save()
    again = _World(tmp_path)
    assert again.gate.get(rid)["status"] == "pending"

    async def run():
        await again.gate.start()
        armed = rid in again.gate._timers
        await again.gate.cancel()
        return armed

    assert asyncio.run(run()) is True
    deadline = datetime.fromisoformat(again.gate.get(rid)["deadline"])
    assert deadline - datetime.now(timezone.utc) > timedelta(seconds=spawn_gate.BOOT_GRACE - 5)
    assert again.created == []


# --------------------------------------------------------------------------- #
# the HTTP surface
# --------------------------------------------------------------------------- #
async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_the_routes_list_extend_and_settle(home, tmp_path):
    async def run():
        op = _FakeSession("op")
        mgr = _manager(op, _FakeSession("old", exited=True))
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        gate = app["spawn_gate"]
        made = []

        async def create(spec, asker, record):
            made.append(asker)
            return 201, {"name": "fresh"}

        gate._create = create
        gate.on_settle = None
        client = await _client(app)
        try:
            assert (await client.get("/api/spawn-requests")).status == 401
            one = gate.submit_spawn(session="op", spec=dict(SPEC), reason="r")["id"]
            two = gate.submit_spawn(session="op", spec=dict(SPEC), reason="r")["id"]

            rows = (await (await client.get("/api/spawn-requests", headers=AUTH)).json())["requests"]
            assert {r["id"] for r in rows} == {one, two}

            resp = await client.post(f"/api/spawn-requests/{one}/extend", headers=AUTH)
            assert resp.status == 200 and (await resp.json())["request"]["extensions"] == 1

            resp = await client.post(f"/api/spawn-requests/{one}/approve", headers=AUTH)
            body = await resp.json()
            assert body["ok"] is True and body["request"]["created"] == "fresh"
            assert made == ["op"]
            assert any(f"spawn-request {one}" in m for m in op.delivered)

            resp = await client.post(f"/api/spawn-requests/{two}/manual", headers=AUTH)
            assert (await resp.json())["request"]["spec"] == SPEC
            resp = await client.post(f"/api/spawn-requests/{two}/created",
                                     json={"session": "by-hand"}, headers=AUTH)
            assert (await resp.json())["request"]["created"] == "by-hand"

            assert (await client.post(f"/api/spawn-requests/{one}/reject",
                                      headers=AUTH)).status == 409
            # an agent cannot answer, not even the asker; it may withdraw its own
            three = gate.submit_spawn(session="op", spec=dict(SPEC), reason="r")["id"]
            agent = {**AUTH, "X-Claunch-Actor": "agent:op"}
            for verb in ("approve", "extend", "manual", "reject"):
                assert (await client.post(f"/api/spawn-requests/{three}/{verb}",
                                          headers=agent)).status == 403
            resp = await client.post(f"/api/spawn-requests/{three}/withdraw",
                                     json={"session": "op"}, headers=agent)
            assert (await resp.json())["request"]["status"] == "withdrawn"
            assert (await client.get("/api/spawn-requests/nope", headers=AUTH)).status == 404
        finally:
            await gate.cancel()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_gate_request_carries_the_asker_as_creator(home):
    from claude_launcher.daemon import actors
    from claude_launcher.daemon.api import _GateRequest

    request = _GateRequest({"manager": None}, "op")
    assert actors.from_request(request) == actors.make(actors.AGENT, "op")


# --------------------------------------------------------------------------- #
# claunch-iw5i8.16: child and root requests (th-d9e343 extended, th-715f44,
# th-17c48c, 2026-10-19)
#
# * an agent's child spawn waits on the card too; unanswered, it is created
#   as asked, set up by the agent;
# * a root (top-level) ask, unanswered, makes no top-level session: the same
#   session is created as the asker's child instead (an operator's ask is a
#   root ask);
# * ``setup`` tells the agent's own setup (``agent``) from the person's
#   approval (``approved``) and the person's form (``custom``);
# * a reviewer child is not gated.
# --------------------------------------------------------------------------- #
CHILD = {"task": "review the branch", "project": "alpha", "workflow": "improv-worker-v2"}


def test_a_root_ask_unanswered_is_created_as_the_askers_child(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = _spawn(world, child_spec=dict(CHILD))["id"]
        await asyncio.sleep(0.3)
        return world, rid

    world, rid = asyncio.run(run())
    record = world.gate.get(rid)
    assert record["scope"] == "root" and record["status"] == "approved"
    assert record["decided_by"] == "timeout"
    assert record["created_as"] == "child" and record["setup"] == "agent"
    assert world.created == [(CHILD, "op", rid)]   # the child spec, not the root one
    assert "no top-level session" in world.notes[-1][1]


def test_a_root_ask_without_a_child_spec_falls_back_on_its_own_spec(home, tmp_path):
    world = _World(tmp_path)
    record = _spawn(world)
    assert record["scope"] == "root" and record["child_spec"] == SPEC


def test_a_root_ask_approved_is_top_level(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world, child_spec=dict(CHILD))["id"]
    record = asyncio.run(world.gate.approve(rid))
    assert record["created_as"] == "root" and record["setup"] == "approved"
    assert world.created == [(SPEC, "op", rid)]
    assert "top-level" in world.notes[-1][1]


def test_a_child_ask_unanswered_is_created_as_asked(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = world.gate.submit_spawn(session="op", spec=dict(CHILD), reason="r",
                                      scope="child")["id"]
        await asyncio.sleep(0.3)
        return world, rid

    world, rid = asyncio.run(run())
    record = world.gate.get(rid)
    assert "child_spec" not in record
    assert record["status"] == "approved" and record["decided_by"] == "timeout"
    assert record["created_as"] == "child" and record["setup"] == "agent"
    assert world.created == [(CHILD, "op", rid)]
    assert "created as your child" in world.notes[-1][1]


def test_a_child_ask_approved_or_taken_by_hand(home, tmp_path):
    world = _World(tmp_path)
    one = world.gate.submit_spawn(session="op", spec=dict(CHILD), reason="r", scope="child")["id"]
    record = asyncio.run(world.gate.approve(one))
    assert record["created_as"] == "child" and record["setup"] == "approved"
    two = world.gate.submit_spawn(session="op", spec=dict(CHILD), reason="r", scope="child")["id"]
    record = world.gate.manual(two)
    assert record["setup"] == "custom" and "spawn child form" in world.notes[-1][1]


@pytest.mark.parametrize("kw", [{"scope": "sideways"}, {"child_spec": "x"}])
def test_a_spawn_ask_refuses_a_bad_scope_or_child_spec(home, tmp_path, kw):
    with pytest.raises(spawn_gate.GateRefused) as err:
        _spawn(_World(tmp_path), **kw)
    assert err.value.status == 400


def test_a_resume_fallback_carries_its_child_spec(home, tmp_path):
    world = _World(tmp_path)
    rid = world.gate.submit_resume(session="op", target="old", reason="r",
                                   fallback=dict(SPEC), fallback_child=dict(CHILD))["id"]
    record = world.gate.spawn_instead(rid)
    follow = world.gate.get(record["followup"])
    assert follow["scope"] == "root" and follow["child_spec"] == CHILD


def test_the_operator_child_spec_drops_profile_and_directory(home):
    from claude_launcher.daemon.operators_v2 import OperatorsV2

    spec = {**SPEC, "cwd": "/w/alpha", "name": "w1", "issue": "x-1"}
    assert OperatorsV2.child_spec(spec) == {
        "name": "w1", "project": "alpha", "task": "do the thing",
        "workflow": "improv-worker-v2", "issue": "x-1"}


def test_an_agents_spawn_files_a_request_and_a_reviewer_does_not(home, tmp_path, monkeypatch):
    from claude_launcher.daemon import api

    made = []

    async def spawn_child(request, parent, body, *, created_by):
        made.append((parent, dict(body), created_by))
        return 201, {"name": "kid", "session": {"name": "kid"}, "parent": parent}

    monkeypatch.setattr(api, "_spawn_child", spawn_child)

    async def run():
        op = _FakeSession("op")
        op.sdef = SessionDef(name="op", harness="py", cwd="/w/op", profile="p")
        mgr = _manager(op)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        gate = app["spawn_gate"]
        gate.path = tmp_path / spawn_gate.FILE
        client = await _client(app)
        agent = {**AUTH, "X-Claunch-Actor": "agent:op"}
        try:
            # an agent's child spawn: a pending request, nothing made
            resp = await client.post("/api/sessions/op/children", headers=agent,
                                     json={"task": "fix it\nin detail", "name": "kid"})
            body = await resp.json()
            assert resp.status == 202 and body["pending"] is True
            record = body["request"]
            assert record["scope"] == "child" and record["session"] == "op"
            assert record["spec"] == {"task": "fix it\nin detail", "name": "kid"}
            assert record["reason"] == "fix it" and made == []

            # a root ask: the top-level spec from the asker, the body as the child
            resp = await client.post("/api/sessions/op/children", headers=agent,
                                     json={"task": "new line of work", "root": True,
                                           "reason": "nobody does it"})
            record = (await resp.json())["request"]
            assert resp.status == 202 and record["scope"] == "root"
            assert record["reason"] == "nobody does it"
            assert record["spec"] == {"task": "new line of work", "profile": "p",
                                      "cwd": "/w/op", "worktree": ""}
            assert record["child_spec"] == {"task": "new line of work"}

            # a reviewer child is not gated
            resp = await client.post("/api/sessions/op/children", headers=agent,
                                     json={"role": "reviewer", "handle": "op-rev"})
            assert resp.status == 201 and made[0][0] == "op"
            assert made[0][2] == {"kind": "agent", "name": "op"}

            # a person's spawn is not gated; a person cannot ask for 'root' here
            resp = await client.post("/api/sessions/op/children", headers=AUTH,
                                     json={"task": "t"})
            assert resp.status == 201 and len(made) == 2
            resp = await client.post("/api/sessions/op/children", headers=AUTH,
                                     json={"task": "t", "root": True})
            assert resp.status == 400
            assert len([r for r in gate.list() if r["status"] == "pending"]) == 2
        finally:
            await gate.cancel()
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_gate_creates_a_child_or_a_root_with_its_setup(home, monkeypatch):
    from claude_launcher.daemon import api

    calls = []

    async def spawn_child(request, parent, body, *, created_by):
        calls.append(("child", parent, body, created_by))
        return 201, {"name": "kid"}

    async def create_session(request, body):
        calls.append(("root", body, request.gate_actor))
        return 201, {"name": "top"}

    monkeypatch.setattr(api, "_spawn_child", spawn_child)
    monkeypatch.setattr(api, "_create_session", create_session)
    asyncio.run(api._spawn_gate_create({}, dict(CHILD), "op",
                                       {"created_as": "child", "setup": "agent"}))
    asyncio.run(api._spawn_gate_create({}, dict(SPEC), "op",
                                       {"created_as": "root", "setup": "approved"}))
    assert calls[0] == ("child", "op", CHILD, {"kind": "agent", "name": "op",
                                               "via": "spawn-gate", "reason": "setup:agent"})
    assert calls[1] == ("root", SPEC, {"kind": "agent", "name": "op",
                                       "via": "spawn-gate", "reason": "setup:approved"})
