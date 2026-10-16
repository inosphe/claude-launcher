"""An agent's kill of another session waits for a person; a reviewer the
workflow is done with is ended by the daemon and only announced
(claunch-5aysr).

The user's spec: every agent kill of another session goes through the
notification-channel permission gate, waiting with no deadline; reviewers
are the exception -- the workflow (improv-worker-v2) ends them without
asking and the person is told. A quick-fork is the person's and an agent
does not end it at all (the s712-qf1 case: a worker's settle-check ended the
fork the person was about to merge).

What these pin:

* the gate: filing ends nothing; approve ends the target recorded as the
  asking agent's kill; reject keeps it; one open request per target; a
  quick-fork is refused; the asker hears every settlement in its terminal;
* the routes: an agent's kill answers 202 pending, a person's kill (no actor
  header) and a workflow's (``cflow``) end at once, a self-kill is not a
  request, the bulk kill refuses an agent, the children listing marks a
  quick-fork;
* the settle reap: a ``settle`` record this run wrote (and not one the
  inherited log carried over) ends the driver's own reviewers only, keeps one
  an ask is still addressed to, and tells the notification channel.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher.daemon import cflow_clock, kill_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import Session

AUTH = {"Authorization": "Bearer sekrit"}


class _FakeSession:
    note_ender = Session.note_ender

    def __init__(self, name: str, *, parent: str = None, exited: bool = False,
                 quick_fork_of: str = None):
        self.sdef = SessionDef(name=name, harness="py", cwd=f"/w/{name}",
                               parent=parent, quick_fork_of=quick_fork_of)
        self.created_at = "2020-01-01T00:00:00+00:00"
        self.exited = exited
        self.exit_code = 0 if exited else None
        self.ended_by = None
        self.pid = None
        self.last_output_at = None
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = None
        self.archived_at = None
        self.held_at = None
        self.paused_at = None
        self.delivered = []
        self.killed = False

    def status(self, threshold=None):
        return "exited" if self.exited else "idle"

    def info(self):
        return {"name": self.sdef.name, "status": self.status(),
                "ended_by": self.ended_by}

    def delivery_held(self):
        return False

    def queue_delivery(self, message):
        self.delivered.append(message)
        return True

    def kill(self, *, force: bool = False):
        self.killed = True
        self.exited = True

    async def shutdown(self):
        self.exited = True


def _manager(*sessions) -> SessionManager:
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    for i, session in enumerate(sessions):
        session.created_at = "2020-01-01T00:00:%02d+00:00" % i
        mgr._sessions[session.sdef.name] = session
    return mgr


def _gate(mgr, tmp_path, notes=None, ended=None):
    notes = notes if notes is not None else []
    ended = ended if ended is not None else []

    def notify(name, message):
        notes.append((name, message))
        return True

    async def end(target, by, force):
        ended.append((target, by, force))
        mgr._sessions[target].ended_by = by
        mgr._sessions[target].exited = True
        return {"name": target}

    return kill_gate.KillGate(manager=mgr, path=tmp_path / kill_gate.FILE,
                              notify=notify, end=end)


# --------------------------------------------------------------------------- #
# who is gated
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "actor, gated",
    [
        ({"kind": "agent", "name": "lead"}, True),
        ({"kind": "agent", "name": "rev"}, False),      # its own exit
        ({"kind": "user", "via": "web"}, False),
        ({"kind": "cflow", "name": "lead"}, False),
        ({"kind": "daemon"}, False),
        (None, False),
    ],
)
def test_only_an_agent_ending_another_session_is_gated(actor, gated):
    assert kill_gate.is_gated(actor, "rev") is gated


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_filing_ends_nothing_and_has_no_deadline(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", parent="lead"))
    ended = []
    gate = _gate(mgr, tmp_path, ended=ended)

    record = gate.submit(session="lead", target="rev", reason="done with it")
    assert record["status"] == "pending"
    assert "deadline" not in record
    assert record["target_parent"] == "lead"
    assert ended == [] and mgr._sessions["rev"].exited is False
    # still pending when read back by a new daemon
    again = kill_gate.KillGate(manager=mgr, path=tmp_path / kill_gate.FILE)
    assert again.get(record["id"])["status"] == "pending"


def test_approve_ends_the_target_as_the_agents_kill(home, tmp_path):
    lead = _FakeSession("lead")
    mgr = _manager(lead, _FakeSession("rev"))
    notes, ended = [], []
    gate = _gate(mgr, tmp_path, notes, ended)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    row = asyncio.run(gate.approve(rid, decided_by="web"))
    assert row["status"] == "approved"
    assert ended == [("rev", {"kind": "agent", "name": "lead",
                              "reason": "kill-approved"}, False)]
    assert mgr._sessions["rev"].ended_by["name"] == "lead"
    [(to, text)] = notes
    assert to == "lead" and f"[claunch kill-request {rid}] approved" in text
    assert asyncio.run(gate.approve(rid)) is None     # second press: nothing


def test_reject_keeps_the_target_and_tells_the_asker(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev"))
    notes, ended = [], []
    gate = _gate(mgr, tmp_path, notes, ended)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    assert gate.reject(rid)["status"] == "rejected"
    assert ended == [] and mgr._sessions["rev"].exited is False
    assert "rejected" in notes[0][1]


def test_a_failed_ending_is_reported_as_failed(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev"))
    notes = []

    async def end(target, by, force):
        raise RuntimeError("pty is wedged")

    gate = kill_gate.KillGate(manager=mgr, path=tmp_path / kill_gate.FILE,
                              notify=lambda n, m: notes.append(m) or True, end=end)
    rid = gate.submit(session="lead", target="rev")["id"]
    row = asyncio.run(gate.approve(rid))
    assert row["status"] == "failed" and "pty is wedged" in row["error"]
    assert "failed" in notes[0]


def test_one_open_request_per_target(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("other"), _FakeSession("rev"))
    gate = _gate(mgr, tmp_path)
    first = gate.submit(session="lead", target="rev", reason="a")
    second = gate.submit(session="other", target="rev", reason="b")
    assert second["id"] == first["id"] and second["existing"] is True
    assert len(gate.list()) == 1


@pytest.mark.parametrize(
    "target, status, match",
    [
        ("", 400, "target"),
        ("lead", 400, "itself"),
        ("ghost", 404, "no session named"),
        ("lead-qf1", 403, "quick-fork"),
    ],
)
def test_refusals(home, tmp_path, target, status, match):
    mgr = _manager(_FakeSession("lead"),
                   _FakeSession("lead-qf1", parent="lead", quick_fork_of="lead"))
    gate = _gate(mgr, tmp_path)
    with pytest.raises(kill_gate.GateRefused, match=match) as info:
        gate.submit(session="lead", target=target)
    assert info.value.status == status
    assert gate.list() == []


def test_a_target_that_exited_otherwise_closes_the_request(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev"))
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev")["id"]
    mgr._sessions["rev"].exited = True
    assert gate.get(rid)["status"] == "moot"
    assert "closed" in notes[0][1]


def test_only_the_asker_withdraws(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev"))
    gate = _gate(mgr, tmp_path)
    rid = gate.submit(session="lead", target="rev")["id"]
    with pytest.raises(kill_gate.GateRefused):
        gate.withdraw(rid, session="someone")
    assert gate.withdraw(rid, session="lead")["status"] == "withdrawn"


def test_note_auto_files_a_settled_notice(home, tmp_path):
    mgr = _manager(_FakeSession("lead"))
    gate = _gate(mgr, tmp_path)
    row = gate.note_auto("lead", "lead-rev", "settle-reap")
    assert row["status"] == "reaped" and row["decided_by"] == "cflow"
    assert row["status"] not in kill_gate.OPEN


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _app(mgr):
    app = build_app(mgr, "sekrit", started_at=time.monotonic())

    async def no_winddown(session, manager, by=None):
        return False

    app["beads"].begin_winddown = no_winddown
    return app


def test_the_kill_routes_gate_an_agent_only(home):
    async def run():
        lead = _FakeSession("lead")
        rev = _FakeSession("rev", parent="lead")
        kid = _FakeSession("kid", parent="lead")
        qf = _FakeSession("lead-qf1", parent="lead", quick_fork_of="lead")
        mine = _FakeSession("mine")
        mgr = _manager(lead, rev, kid, qf, mine)
        app = _app(mgr)
        client = await _client(app)
        agent = {**AUTH, "X-Claunch-Actor": "agent:lead"}
        try:
            # an agent's kill: pending, nothing ended
            resp = await client.post("/api/sessions/rev/kill",
                                     json={"reason": "review done"}, headers=agent)
            assert resp.status == 202, await resp.text()
            body = await resp.json()
            assert body["pending"] is True and rev.killed is False
            rid = body["kill_request"]

            # the same through the parent's child route: the same request
            resp = await client.post("/api/sessions/lead/children/rev/kill",
                                     json={}, headers=agent)
            assert resp.status == 202, await resp.text()
            again = await resp.json()
            assert again["kill_request"] == rid and again["existing"] is True

            # a quick-fork is refused, on both routes
            for path in ("/api/sessions/lead-qf1/kill",
                         "/api/sessions/lead/children/lead-qf1/kill"):
                resp = await client.post(path, json={}, headers=agent)
                assert resp.status == 403, path
            assert qf.killed is False

            # the children listing marks the quick-fork
            resp = await client.get("/api/sessions/lead/children", headers=agent)
            rows = {r["name"]: r for r in (await resp.json())["children"]}
            assert rows["lead-qf1"]["quick_fork_of"] == "lead"
            assert "quick_fork_of" not in rows["kid"]

            # the bulk kill refuses an agent outright
            resp = await client.post("/api/sessions/kill", json={}, headers=agent)
            assert resp.status == 403
            assert kid.killed is False

            # a session ending itself is not a request
            resp = await client.post("/api/sessions/mine/kill", json={},
                                     headers={**AUTH, "X-Claunch-Actor": "agent:mine"})
            assert resp.status == 200 and mine.killed is True

            # a workflow's kill and a person's end at once
            resp = await client.post("/api/sessions/kid/kill", json={},
                                     headers={**AUTH, "X-Claunch-Actor": "cflow:lead"})
            assert resp.status == 200 and kid.killed is True
            assert kid.ended_by["kind"] == "cflow"

            # approve: the target ends, recorded as the agent's kill
            resp = await client.post(f"/api/kill-requests/{rid}/approve", headers=AUTH)
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["request"]["status"] == "approved"
            assert rev.killed is True
            assert rev.ended_by == {"kind": "agent", "name": "lead",
                                    "reason": "kill-approved"}
            assert any(f"kill-request {rid}" in m for m in lead.delivered)
            resp = await client.post(f"/api/kill-requests/{rid}/reject", headers=AUTH)
            assert resp.status == 409
            assert (await client.get("/api/kill-requests/nope",
                                     headers=AUTH)).status == 404
        finally:
            await client.close()

    asyncio.run(run())


def test_a_persons_kill_is_not_gated(home):
    async def run():
        rev = _FakeSession("rev")
        mgr = _manager(rev)
        client = await _client(_app(mgr))
        try:
            resp = await client.post("/api/sessions/rev/kill", headers=AUTH)
            assert resp.status == 200 and rev.killed is True
            rows = (await (await client.get("/api/kill-requests",
                                            headers=AUTH)).json())["requests"]
            assert rows == []
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the settle reap
# --------------------------------------------------------------------------- #
def _at(delta_s: float) -> str:
    base = datetime(2026, 10, 16, 6, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=delta_s)).isoformat()


def _state(*items) -> dict:
    return {"started": _at(0),
            "collections": {"signals": {"items": list(items)}}}


def test_settle_counts_only_this_runs_record():
    assert cflow_clock.settle_signalled(_state({"name": "settle", "at": _at(5)}))
    # the inherited log carries the previous run's settle: not this run's
    assert not cflow_clock.settle_signalled(
        _state({"name": "settle", "at": _at(-60)}))
    assert not cflow_clock.settle_signalled(_state({"name": "cut", "at": _at(5)}))
    assert not cflow_clock.settle_signalled(_state({"name": "settle"}))
    assert not cflow_clock.settle_signalled({"collections": {}})


class _TreeManager:
    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, name):
        return self._sessions[name]

    def children(self, name):
        return [n for n, s in self._sessions.items() if s.sdef.parent == name]

    def list(self):
        return list(self._sessions.values())

    def persist(self):
        pass


class _Mesh:
    def __init__(self, roles):
        self._roles = roles

    def meshes_for_session(self, name):
        return [{"mesh": "m", "role": self._roles.get(name, "worker")}]


def _drain(clock):
    async def go():
        clock._drain_ends()

    asyncio.run(go())


def _settle_tree(monkeypatch, *, asked=()):
    sessions = {
        "w1": _FakeSession("w1"),
        "w1-rev": _FakeSession("w1-rev", parent="w1"),
        "w1-rev2": _FakeSession("w1-rev2", parent="w1"),
        "w1-kid": _FakeSession("w1-kid", parent="w1"),
        "w1-qf1": _FakeSession("w1-qf1", parent="w1", quick_fork_of="w1"),
        "w1-kid-rev": _FakeSession("w1-kid-rev", parent="w1-kid"),
    }
    mesh = _Mesh({"w1-rev": "reviewer", "w1-rev2": "reviewer",
                  "w1-kid-rev": "reviewer"})
    from claude_launcher.cflow2 import asks

    monkeypatch.setattr(asks, "pending_for",
                        lambda name: [{"ask": "a"}] if name in asked else [])
    clock = cflow_clock.RunEventClock(_TreeManager(sessions), mesh)
    told = []
    clock.on_reviewer_ended = lambda d, r, why: told.append((d, r, why))
    return clock, sessions, told


def test_settle_reap_ends_the_drivers_reviewers_only(home, monkeypatch):
    clock, sessions, told = _settle_tree(monkeypatch)
    clock._queue_settle_reap("w1", "run-1")
    _drain(clock)
    ended = sorted(n for n, s in sessions.items() if s.exited)
    assert ended == ["w1-rev", "w1-rev2"]
    assert sessions["w1-rev"].ended_by == {
        "kind": "cflow", "name": "w1", "reason": "settle-reap"}
    # a worker child, a quick-fork and a grandchild reviewer stay
    for name in ("w1", "w1-kid", "w1-qf1", "w1-kid-rev"):
        assert sessions[name].exited is False, name
    assert sorted(told) == [("w1", "w1-rev", "settle-reap"),
                            ("w1", "w1-rev2", "settle-reap")]


def test_settle_reap_keeps_a_reviewer_an_ask_is_addressed_to(home, monkeypatch):
    clock, sessions, told = _settle_tree(monkeypatch, asked=("w1-rev",))
    clock._queue_settle_reap("w1", "run-1")
    _drain(clock)
    assert sessions["w1-rev"].exited is False
    assert sessions["w1-rev2"].exited is True
    assert told == [("w1", "w1-rev2", "settle-reap")]


def test_settle_reap_queues_nothing_while_every_reviewer_is_asked(home, monkeypatch):
    clock, sessions, told = _settle_tree(monkeypatch, asked=("w1-rev", "w1-rev2"))
    clock._queue_settle_reap("w1", "run-1")
    assert clock._settle_pending == []


def test_settle_reap_queues_nothing_without_a_reviewer(home, monkeypatch):
    clock, sessions, told = _settle_tree(monkeypatch)
    for name in ("w1-rev", "w1-rev2"):
        sessions[name].exited = True
    clock._queue_settle_reap("w1", "run-1")
    assert clock._settle_pending == []
