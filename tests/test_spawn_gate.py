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
* reject creates nothing; manual leaves the request pending with the clock
  paused (claunch-uzo40), the form's create links its session back and ends
  it, and approve / reject / withdraw still answer a held request;
  the deadline counts as approval;
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


def test_manual_holds_the_request_open_until_the_form_links_its_session(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    record = world.gate.manual(rid)
    # The bug (claunch-uzo40): pressing Manual used to settle the request, so
    # a form closed without creating anything left no way back.
    assert record["status"] == "pending" and record["hold"] == "manual"
    assert record["spec"] == SPEC and record["setup"] == "custom"
    assert world.created == [] and world.settled == []
    assert "stays open" in world.notes[-1][1] and "by hand (web)" in world.notes[-1][1]
    assert [r["id"] for r in world.gate.list() if r["status"] == "pending"] == [rid]

    linked = world.gate.link(rid, "hand-made")
    assert linked["status"] == "approved" and linked["setup"] == "custom"
    assert linked["created"] == "hand-made" and "hand-made" in world.notes[-1][1]
    assert "hold" not in linked and world.settled[-1]["id"] == rid
    with pytest.raises(spawn_gate.GateRefused):
        world.gate.link(rid, "again")


def test_manual_again_hands_the_same_spec_back(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    world.gate.manual(rid)
    told = len(world.notes)
    again = world.gate.manual(rid)
    assert again["status"] == "pending" and again["spec"] == SPEC
    assert len(world.notes) == told, "a second press does not tell the asker again"


def test_a_held_request_has_no_clock(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = _spawn(world)["id"]
        world.gate.manual(rid)
        held_deadline = world.gate.get(rid)["deadline"]
        await asyncio.sleep(0.3)
        return world, rid, held_deadline

    world, rid, held_deadline = asyncio.run(run())
    record = world.gate.get(rid)
    assert record["status"] == "pending" and record["hold"] == "manual"
    assert record["deadline"] == held_deadline, "the deadline is only where it stopped"
    assert world.created == [] and rid not in world.gate._timers


def test_extend_restarts_a_paused_clock(home, tmp_path):
    async def run():
        world = _World(tmp_path, timeout=0.05)
        rid = _spawn(world)["id"]
        world.gate.manual(rid)
        await asyncio.sleep(0.2)
        again = world.gate.extend(rid)
        armed = rid in world.gate._timers
        await world.gate.cancel()
        return again, armed

    again, armed = asyncio.run(run())
    assert "hold" not in again and again["status"] == "pending"
    assert armed is True, "the countdown is running again"
    assert datetime.fromisoformat(again["deadline"]) > datetime.now(timezone.utc)


def test_approve_after_manual_creates_as_asked(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    world.gate.manual(rid)
    record = asyncio.run(world.gate.approve(rid))
    assert record["status"] == "approved" and record["setup"] == "approved"
    assert record["created"] == "new1" and world.created[0][2] == rid
    assert "hold" not in record


def test_reject_and_withdraw_end_a_held_request(home, tmp_path):
    world = _World(tmp_path)
    one = _spawn(world)["id"]
    world.gate.manual(one)
    assert world.gate.reject(one)["status"] == "rejected"
    two = _spawn(world)["id"]
    world.gate.manual(two)
    assert world.gate.withdraw(two, session="op")["status"] == "withdrawn"
    assert world.created == []


def test_a_held_request_still_counts_against_the_asker(home, tmp_path):
    world = _World(tmp_path)
    for _ in range(spawn_gate.MAX_PENDING_PER_ASKER):
        world.gate.manual(_spawn(world)["id"])
    with pytest.raises(spawn_gate.GateRefused) as err:
        _spawn(world)
    assert err.value.status == 409


def test_link_refuses_a_request_nobody_took_over(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    with pytest.raises(spawn_gate.GateRefused):
        world.gate.link(rid, "hand-made")


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


def test_a_held_request_outlives_the_daemon_without_a_clock(home, tmp_path):
    world = _World(tmp_path)
    rid = _spawn(world)["id"]
    world.gate.manual(rid)
    held_deadline = world.gate.get(rid)["deadline"]
    again = _World(tmp_path)
    assert again.gate.get(rid)["hold"] == "manual"

    async def run():
        await again.gate.start()
        armed = rid in again.gate._timers
        await again.gate.cancel()
        return armed

    assert asyncio.run(run()) is False, "the paused clock is not re-armed"
    assert again.gate.get(rid)["deadline"] == held_deadline
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
            # the card's rows ride on the listing, resolved against the asker
            assert rows[0]["facts"]["profile"] == {"value": "p", "inherited": False}
            # the directory and its checkout are two rows (claunch-8x6v2):
            # here a root record, whose empty worktree is a new checkout
            assert rows[0]["facts"]["workspace"] == {"value": "/w/op",
                                                     "inherited": True}
            assert rows[0]["facts"]["worktree"] == {"value": "new (generated name)",
                                                    "inherited": False}
            got = await (await client.get(f"/api/spawn-requests/{one}", headers=AUTH)).json()
            assert got["request"]["facts"]["workflow"]["value"] == "improv-worker-v2"
            assert "facts" not in gate.get(one), "the stored record keeps the spec only"

            resp = await client.post(f"/api/spawn-requests/{one}/extend", headers=AUTH)
            assert resp.status == 200 and (await resp.json())["request"]["extensions"] == 1

            resp = await client.post(f"/api/spawn-requests/{one}/approve", headers=AUTH)
            body = await resp.json()
            assert body["ok"] is True and body["request"]["created"] == "fresh"
            assert made == ["op"]
            assert any(f"spawn-request {one}" in m for m in op.delivered)

            resp = await client.post(f"/api/spawn-requests/{two}/manual", headers=AUTH)
            held = (await resp.json())["request"]
            assert held["spec"] == SPEC
            # Manual is not an answer: the card stays, the clock is paused
            # (claunch-uzo40) and the routes still settle it.
            assert held["status"] == "pending" and held["hold"] == "manual"
            rows = (await (await client.get("/api/spawn-requests", headers=AUTH)).json())["requests"]
            assert two in {r["id"] for r in rows if r["status"] == "pending"}
            resp = await client.post(f"/api/spawn-requests/{two}/created",
                                     json={"session": "by-hand"}, headers=AUTH)
            linked = (await resp.json())["request"]
            assert linked["created"] == "by-hand"
            assert linked["status"] == "approved" and linked["setup"] == "custom"
            assert "hold" not in linked

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


# --- the card's facts rows (claunch-mgope) ----------------------------------


def test_card_facts_fill_a_childs_blanks_from_its_asker(home, tmp_path):
    from claude_launcher import workspaces

    (tmp_path / "repo").mkdir()
    workspaces.add(str(tmp_path / "repo"), "repo")
    asker = {"profile": "work", "cwd": str(tmp_path / "repo" / ".claude" / "worktrees" / "w1"),
             "project": "alpha"}
    facts = spawn_gate.card_facts({"role": "worker", "workflow": "improv-worker-v2",
                                   "worktree": "w1-kid"}, asker)
    assert list(facts) == list(spawn_gate.CARD_FIELDS)
    assert facts["project"] == {"value": "alpha", "inherited": True}
    assert facts["profile"] == {"value": "work", "inherited": True}
    # the workspace is the DIRECTORY the child would run in; the checkout it
    # asked for is its own row (claunch-8x6v2)
    assert facts["workspace"] == {"value": "repo/.claude/worktrees/w1",
                                  "inherited": True}
    assert facts["worktree"] == {"value": "w1-kid", "inherited": False}
    assert facts["role"] == {"value": "worker", "inherited": False}
    assert facts["workflow"] == {"value": "improv-worker-v2", "inherited": False}


def test_card_facts_keep_what_the_spec_names(home, tmp_path):
    from claude_launcher import projects, workspaces

    (tmp_path / "hq").mkdir()
    workspaces.add(str(tmp_path / "hq"), "hq")
    projects.add("beta", default_workspace="hq")
    asker = {"profile": "work", "cwd": "/elsewhere"}
    facts = spawn_gate.card_facts({"project": "beta", "profile": "p2", "workflow": "-"}, asker)
    assert facts["project"] == {"value": "beta", "inherited": False}
    assert facts["profile"] == {"value": "p2", "inherited": False}
    # a named project with no directory starts in its default workspace
    assert facts["workspace"] == {"value": "hq", "inherited": False}
    # a declined workflow and a missing role read as none
    assert facts["workflow"] == {"value": "", "inherited": False}
    assert facts["role"] == {"value": "", "inherited": False}
    named = spawn_gate.card_facts({"workspace": "hq"}, asker)["workspace"]
    assert named == {"value": "hq", "inherited": False}


def test_card_facts_show_the_workflow_a_child_inherits(home):
    # A child spec naming no workflow is given its asker's pair at create
    # time (onboard.inherit_workflow): the row says so instead of "none"
    # (claunch-1pfnx).
    asker = {"profile": "work", "cwd": "/w/op"}
    facts = spawn_gate.card_facts({"role": "worker"}, asker,
                                  paired_workflow="improv-worker-v2")
    assert facts["workflow"] == {"value": "improv-worker-v2", "inherited": True}
    # the spec's own answer wins, and "-" declines the pair
    named = spawn_gate.card_facts({"workflow": "other"}, asker,
                                  paired_workflow="improv-worker-v2")
    assert named["workflow"] == {"value": "other", "inherited": False}
    declined = spawn_gate.card_facts({"workflow": "-"}, asker,
                                     paired_workflow="improv-worker-v2")
    assert declined["workflow"] == {"value": "", "inherited": False}
    # no pair: still none
    assert spawn_gate.card_facts({}, asker)["workflow"] == {"value": "", "inherited": False}


def test_the_card_route_reads_the_pair_for_a_child_request(home, tmp_path, monkeypatch):
    from claude_launcher.daemon import onboard

    asked = []

    def paired(*, parent, parent_cwd, cwd):
        asked.append((parent, parent_cwd, cwd))
        return "improv-worker-v2"

    monkeypatch.setattr(onboard, "paired_child_workflow", paired)

    async def run():
        mgr = _manager(_FakeSession("op"))
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        gate = app["spawn_gate"]
        gate.on_settle = None
        client = await _client(app)
        try:
            kid = gate.submit_spawn(session="op", spec={"role": "worker", "task": "t"},
                                    reason="r", scope="child")["id"]
            named = gate.submit_spawn(session="op", spec={"role": "worker", "task": "t",
                                                          "workflow": "-"},
                                      reason="r", scope="child")["id"]
            rows = (await (await client.get("/api/spawn-requests", headers=AUTH)).json())["requests"]
            by_id = {r["id"]: r for r in rows}
            got = await (await client.get(f"/api/spawn-requests/{kid}", headers=AUTH)).json()
            return by_id, got, kid, named
        finally:
            await client.close()

    by_id, got, kid, named = asyncio.run(run())
    want = {"value": "improv-worker-v2", "inherited": True}
    assert by_id[kid]["facts"]["workflow"] == want
    assert got["request"]["facts"]["workflow"] == want
    # a declined pair is not looked up and reads as none
    assert by_id[named]["facts"]["workflow"] == {"value": "", "inherited": False}
    assert set(asked) == {("op", "/w/op", "/w/op")}


def test_card_facts_without_an_asker_show_the_spec_alone(home):
    facts = spawn_gate.card_facts({"cwd": "/not/registered"}, None)
    assert facts["project"] == {"value": "default", "inherited": False}
    assert facts["profile"] == {"value": "", "inherited": False}
    assert facts["workspace"] == {"value": "/not/registered", "inherited": False}


def test_card_facts_read_a_top_level_body(home, tmp_path):
    asker = {"profile": "work", "cwd": "/w/op", "project": "alpha"}
    # _root_spec's completion: profile, project and directory copied from the
    # asker, and worktree "" -- a new checkout with the generated name
    spec = {"profile": "work", "project": "alpha", "cwd": "/w/op", "worktree": "",
            "task": "t"}
    facts = spawn_gate.card_facts(spec, asker, top_level=True, asked={"task": "t"})
    assert facts["workspace"] == {"value": "/w/op", "inherited": True}
    # a top-level body's present-but-empty worktree is a new checkout; the
    # asker did not name one, so the daemon's own answer reads as inherited
    assert facts["worktree"] == {"value": "new (generated name)", "inherited": True}
    assert facts["profile"] == {"value": "work", "inherited": True}
    assert facts["project"] == {"value": "alpha", "inherited": True}
    # what the agent named stays its own
    named = spawn_gate.card_facts(spec, asker, top_level=True,
                                  asked={"task": "t", "project": "alpha",
                                         "worktree": ""})
    assert named["project"] == {"value": "alpha", "inherited": False}
    assert named["workspace"]["inherited"] is False
    assert named["worktree"] == {"value": "new (generated name)", "inherited": False}
    # a child body's empty worktree is no worktree of its own — it runs in
    # the asker's working copy (not a second directory)
    child = spawn_gate.card_facts({"worktree": ""}, asker)
    assert child["workspace"] == {"value": "/w/op", "inherited": True}
    assert child["worktree"] == {"value": "", "inherited": False}


def test_card_facts_read_the_checkout_a_child_was_given(home, tmp_path):
    from claude_launcher import workspaces

    (tmp_path / "repo").mkdir()
    workspaces.add(str(tmp_path / "repo"), "repo")
    asker = {"profile": "work", "cwd": str(tmp_path / "repo"), "project": "alpha"}
    # A named checkout with the branch it starts from: one row, each half of
    # the answer visible (claunch-8x6v2).
    facts = spawn_gate.card_facts({"workspace": "repo", "worktree": "kid-1",
                                   "branch_from": "master"}, asker)
    assert facts["workspace"] == {"value": "repo", "inherited": False}
    assert facts["worktree"] == {"value": "kid-1 from master", "inherited": False}
    # A reuse that is rebased: the other qualifier, never both from the
    # daemon's point of view, but the row still says which is which.
    reuse = spawn_gate.card_facts({"worktree": "old-1", "rebase_onto": "master"},
                                  asker)
    assert reuse["worktree"] == {"value": "old-1 rebased onto master",
                                 "inherited": False}
    # `true` is the generated name, which a child body can ask for before it
    # has a name to build one from.
    auto = spawn_gate.card_facts({"worktree": True}, asker)
    assert auto["worktree"] == {"value": "new (generated name)", "inherited": False}


def test_card_facts_resolve_a_harness_selector_and_a_shut_workspace_gate(home, tmp_path):
    from claude_launcher import projects, workspaces

    (tmp_path / "hq").mkdir()
    workspaces.add(str(tmp_path / "hq"), "hq")
    projects.add("beta", default_workspace="hq")
    asker = {"profile": "work:claude", "cwd": "/w/op"}
    facts = spawn_gate.card_facts({"profile": ":codex", "project": "beta"}, asker,
                                  project_home=False)
    assert facts["profile"] == {"value": "work:codex", "inherited": False}
    # spawn.allow_workspace off: the child stays in the asker's directory
    assert facts["workspace"] == {"value": "/w/op", "inherited": True}
