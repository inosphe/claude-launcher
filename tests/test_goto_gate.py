"""A leader's request to move a CHILD session's run, gated on a person.

``request_goto`` lets a run's own driver ask for an off-graph move. This is
the other half (claunch-ny72): a leader that has *verified* where a
descendant's run actually stands — the afternoon six sessions sat permanently
blocked behind a defective gate — can file the same kind of request against
the child's run, and the person's answer (or the five-minute silence that
counts as one) is what moves it.

What these pin:

* authority runs down the tree only — the same ``require_commands`` kill and
  reparent stand on; a peer or a parent as target is refused before anything
  is filed;
* filing HOLDS the child run (``waiting_goto``) and moves nothing, so an
  approval landing later moves the run the person was actually looking at;
* approve/deny/timeout settle through the engine's ordinary grant path, so
  the child run's journal keeps request and move attached (``granted``,
  ``asked_by``), and a timeout approval is recorded as one (``by=timeout``);
* the asker — and only the asker — can withdraw through the agent door.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from claude_launcher.cflow import engine, mcp, state as state_mod
from claude_launcher.cflow.engine import CflowError
from claude_launcher.daemon import goto_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
    next: three
  three:
    instructions: do three
"""


class _FakeSession:
    """Enough of a session for the tree walks, the nudge, the leader notice,
    and the manager's shutdown persist: the def, the creation stamp, the
    record fields persist() reads, and a deliver that records."""

    def __init__(self, name: str, parent=None, cwd: str = "", made: int = 0):
        self.sdef = SessionDef(name=name, harness="py", parent=parent, cwd=cwd)
        self.created_at = "2020-01-01T00:00:%02d+00:00" % made
        self.exited = False
        self.exit_code = None
        self.pid = None
        self.last_output_at = None
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = None
        self.archived_at = None
        self.delivered = []

    def status(self):
        return "idle"

    def delivery_held(self):
        return False

    async def deliver(self, message):
        self.delivered.append(message)
        return True

    def queue_delivery(self, message):
        self.delivered.append(message)
        return True

    async def shutdown(self):
        self.exited = True


def _manager(*sessions) -> SessionManager:
    """lead -> (w1, w2), each ``(name, parent, cwd)``."""
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    for i, (name, parent, cwd) in enumerate(sessions):
        mgr._sessions[name] = _FakeSession(name, parent, cwd, i)
    return mgr


def _child_run(tmp_path, name="w1"):
    """A real run owned by session ``name``, advanced to step 'two'."""
    proj = tmp_path / f"proj-{name}"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    cwd = str(proj)
    engine.start("linear", cwd=cwd, scope=name)
    engine.report("did one", cwd=cwd, scope=name)
    payload = engine.next_step(cwd=cwd, scope=name)
    assert payload["step_id"] == "two"
    return proj


def _journal(proj, scope):
    path = state_mod.journal_path(str(proj), scope)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _events(proj, scope, name):
    return [e for e in _journal(proj, scope) if e.get("event") == name]


async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --------------------------------------------------------------------------- #
# filing: authority, the hold, the record
# --------------------------------------------------------------------------- #
def test_submit_files_and_holds_the_child_run(home, tmp_path):
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            resp = await client.post(
                "/api/cflow/goto-requests",
                json={
                    "session": "lead",
                    "target_session": "w1",
                    "step": "three",
                    "reason": "gate defect; branch landed, verified by git",
                },
                headers=AUTH,
            )
            assert resp.status == 200
            rec = (await resp.json())["request"]
            assert rec["status"] == "pending"
            assert rec["session"] == "lead"
            assert rec["target_session"] == "w1"
            assert rec["step"] == "three"
            assert rec["from"] == "two"
            assert rec["requested_at"] < rec["deadline"]

            # The hold is the engine's own: the child driver's next poll
            # answers waiting_goto, and the note names who asked rather than
            # telling the driver "you asked".
            held = engine.status(str(proj), scope="w1")
            assert held["status"] == "waiting_goto"
            assert held["goto_request"]["by"] == "lead"
            assert held["goto_request"]["via"] == "leader"
            assert "lead" in held["note"]
            assert engine.status(str(proj), scope="w1")["step_id"] == "two"

            [event] = _events(proj, "w1", "goto_requested")
            assert event["by"] == "lead"
            assert event["via"] == "leader"
            assert event["step"] == "three"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_submit_refuses_a_peer_a_parent_and_oneself(home, tmp_path):
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(
            ("lead", None, ""), ("w1", "lead", str(proj)), ("w2", "lead", "")
        )
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            for session, target in (
                ("w2", "w1"),      # sideways: a peer's run
                ("w1", "lead"),    # upward: the parent's run
                ("w1", "w1"),      # one's own run: plain request_goto's job
                ("ghost", "w1"),   # a requester that does not exist
            ):
                resp = await client.post(
                    "/api/cflow/goto-requests",
                    json={
                        "session": session,
                        "target_session": target,
                        "step": "three",
                        "reason": "should never be filed",
                    },
                    headers=AUTH,
                )
                assert resp.status == 400, (session, target, resp.status)
            # Nothing was filed on the run by any of those.
            assert engine.status(str(proj), scope="w1")["status"] == "step"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_submit_refuses_a_target_with_no_run(home, tmp_path):
    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", ""))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            resp = await client.post(
                "/api/cflow/goto-requests",
                json={
                    "session": "lead",
                    "target_session": "w1",
                    "step": "three",
                    "reason": "no run exists",
                },
                headers=AUTH,
            )
            assert resp.status == 400
            assert "no cflow run" in (await resp.json())["error"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_one_pending_request_per_run(home, tmp_path):
    proj1 = _child_run(tmp_path, "w1")
    proj2 = _child_run(tmp_path, "w2")

    async def run():
        mgr = _manager(
            ("lead", None, ""),
            ("w1", "lead", str(proj1)),
            ("w2", "lead", str(proj2)),
        )
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            ask = {"session": "lead", "step": "three", "reason": "verified"}
            assert (
                await client.post(
                    "/api/cflow/goto-requests",
                    json={**ask, "target_session": "w1"},
                    headers=AUTH,
                )
            ).status == 200
            # A second ask on the SAME run queues nothing.
            resp = await client.post(
                "/api/cflow/goto-requests",
                json={**ask, "target_session": "w1"},
                headers=AUTH,
            )
            assert resp.status == 409
            # A different run is a different question and goes through.
            assert (
                await client.post(
                    "/api/cflow/goto-requests",
                    json={**ask, "target_session": "w2"},
                    headers=AUTH,
                )
            ).status == 200
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the three settlements
# --------------------------------------------------------------------------- #
def test_approve_moves_the_run_with_the_request_attached(home, tmp_path):
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            rec = (
                await (
                    await client.post(
                        "/api/cflow/goto-requests",
                        json={
                            "session": "lead",
                            "target_session": "w1",
                            "step": "three",
                            "reason": "verified",
                        },
                        headers=AUTH,
                    )
                ).json()
            )["request"]
            resp = await client.post(
                f"/api/cflow/goto-requests/{rec['id']}/approve", headers=AUTH
            )
            assert resp.status == 200
            settled = (await resp.json())["request"]
            assert settled["status"] == "approved"
            assert settled["decided_by"] == "web"

            # The run actually moved, and the journal ties the move to the
            # request and its asker — not a bare override.
            await asyncio.sleep(0.1)  # the nudge/notice tasks land
            assert engine.status(str(proj), scope="w1")["step_id"] == "three"
            [approved] = _events(proj, "w1", "goto_approved")
            assert approved["by"] == "web"
            assert approved["request"] == rec["request"]
            [forced] = _events(proj, "w1", "state_forced")
            assert forced["to"] == "three"
            assert forced["granted"] == rec["request"]
            assert forced["asked_by"] == "lead"

            # Both ends were told: the child driver nudged, the leader
            # delivered the outcome.
            child = mgr._sessions["w1"]
            leader = mgr._sessions["lead"]
            assert any("forced to 'three'" in m for m in child.delivered)
            assert any("approved (web)" in m for m in leader.delivered)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_deny_moves_nothing_and_hands_the_refusal_to_the_driver(home, tmp_path):
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            rec = (
                await (
                    await client.post(
                        "/api/cflow/goto-requests",
                        json={
                            "session": "lead",
                            "target_session": "w1",
                            "step": "three",
                            "reason": "verified",
                        },
                        headers=AUTH,
                    )
                ).json()
            )["request"]
            resp = await client.post(
                f"/api/cflow/goto-requests/{rec['id']}/deny",
                json={"reason": "the branch is not what you read"},
                headers=AUTH,
            )
            assert resp.status == 200
            assert (await resp.json())["request"]["status"] == "denied"

            await asyncio.sleep(0.1)
            state = engine.status(str(proj), scope="w1")
            assert state["step_id"] == "two"
            assert state["goto_request"]["decision"] == "denied"
            [denied] = _events(proj, "w1", "goto_denied")
            assert denied["by"] == "web"
            assert denied["reason"] == "the branch is not what you read"
            assert any("denied (web)" in m for m in mgr._sessions["lead"].delivered)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_an_unanswered_request_counts_as_approval(home, tmp_path):
    """The user's spec — 5분 무응답이면 승인으로 센다 — with the gate's own
    clock shortened: the timer fires, the move is applied, and the journal
    records the approval as the deadline's, not a person's."""
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=0.05)
        gate = app["goto_gate"]
        rec = gate.submit(
            session="lead",
            target_session="w1",
            step="three",
            reason="verified",
        )
        assert rec["status"] == "pending"
        try:
            await asyncio.sleep(0.3)
            settled = gate.get(rec["id"])
            assert settled["status"] == "approved"
            assert settled["decided_by"] == "timeout"
            assert engine.status(str(proj), scope="w1")["step_id"] == "three"
            [approved] = _events(proj, "w1", "goto_approved")
            assert approved["by"] == "timeout"
            await asyncio.sleep(0.1)
            assert any("approved (timeout)" in m for m in mgr._sessions["lead"].delivered)
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_only_the_asker_withdraws(home, tmp_path):
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(
            ("lead", None, ""), ("w1", "lead", str(proj)), ("w2", "lead", "")
        )
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            rec = (
                await (
                    await client.post(
                        "/api/cflow/goto-requests",
                        json={
                            "session": "lead",
                            "target_session": "w1",
                            "step": "three",
                            "reason": "verified",
                        },
                        headers=AUTH,
                    )
                ).json()
            )["request"]
            # Somebody else — even another session of the same subtree — may
            # not take the question back.
            resp = await client.post(
                f"/api/cflow/goto-requests/{rec['id']}/withdraw",
                json={"session": "w2"},
                headers=AUTH,
            )
            assert resp.status == 400
            assert engine.status(str(proj), scope="w1")["status"] == "waiting_goto"

            resp = await client.post(
                f"/api/cflow/goto-requests/{rec['id']}/withdraw",
                json={"session": "lead"},
                headers=AUTH,
            )
            assert resp.status == 200
            assert (await resp.json())["request"]["status"] == "withdrawn"
            # The hold is gone; the run continues from where it stands.
            state = engine.status(str(proj), scope="w1")
            assert state["status"] == "step"
            assert state["step_id"] == "two"
            [withdrawn] = _events(proj, "w1", "goto_withdrawn")
            assert withdrawn["by"] == "lead"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_vanished_engine_request_settles_as_moot(home, tmp_path):
    """The child driver can still withdraw its own run's hold; the gate then
    has nothing to settle, and an approve must say so instead of pretending
    the move happened."""
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            rec = (
                await (
                    await client.post(
                        "/api/cflow/goto-requests",
                        json={
                            "session": "lead",
                            "target_session": "w1",
                            "step": "three",
                            "reason": "verified",
                        },
                        headers=AUTH,
                    )
                ).json()
            )["request"]
            engine.cancel_goto_request(by="w1", cwd=str(proj), scope="w1")
            resp = await client.post(
                f"/api/cflow/goto-requests/{rec['id']}/approve", headers=AUTH
            )
            assert resp.status == 200
            settled = (await resp.json())["request"]
            assert settled["status"] == "moot"
            assert settled["error"]
            assert engine.status(str(proj), scope="w1")["step_id"] == "two"
            await asyncio.sleep(0.1)
            assert any("nothing was applied" in m for m in mgr._sessions["lead"].delivered)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# answers given through one of the other doors
# --------------------------------------------------------------------------- #
def test_an_answer_on_the_run_page_settles_the_card(home, tmp_path):
    """The request is one question with several doors, and the card is only
    one of them: the run page's own Move/Refuse (``/api/cflow/goto/resolve``)
    and ``claunch cflow goto --approve`` settle it on the run and know
    nothing about the gate record.

    Before this reconciliation the record stayed ``pending`` after such an
    answer, so the card kept its countdown for a question nobody could still
    answer, and the leader was never told the move had gone through.
    """
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        client = await _client(app)
        try:
            rec = (
                await (
                    await client.post(
                        "/api/cflow/goto-requests",
                        json={
                            "session": "lead",
                            "target_session": "w1",
                            "step": "three",
                            "reason": "verified landed by git",
                        },
                        headers=AUTH,
                    )
                ).json()
            )["request"]

            # The person answers on the run page instead of on the card.
            resp = await client.post(
                "/api/cflow/goto/resolve",
                json={"cwd": str(proj), "scope": "w1", "decision": "approve"},
                headers=AUTH,
            )
            assert resp.status == 200
            assert engine.status(str(proj), scope="w1")["step_id"] == "three"

            listed = (
                await (
                    await client.get("/api/cflow/goto-requests", headers=AUTH)
                ).json()
            )["requests"]
            [same] = [r for r in listed if r["id"] == rec["id"]]
            assert same["status"] == "approved"
            assert same["settled_elsewhere"] is True
            assert same["decided_by"] == "web"
            assert not [r for r in listed if r["status"] == "pending"]

            await asyncio.sleep(0.1)
            assert any(
                "answered without this gate" in m
                for m in mgr._sessions["lead"].delivered
            )
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_an_answer_elsewhere_stops_the_deadline_approving_it_again(home, tmp_path):
    """A refusal on the run page must survive the gate's own deadline.

    The timeout counts silence as approval, and it used to count it on a
    record that another door had already settled: the auto-approval fired on
    a request that no longer existed, the engine refused the move, and the
    leader was told its request had not applied — a refusal reported as a
    failed approval. Here the run is denied first, and the deadline finds
    nothing left to approve.
    """
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=0.05)
        gate = app["goto_gate"]
        rec = gate.submit(
            session="lead", target_session="w1", step="three", reason="verified"
        )
        try:
            engine.resolve_goto(
                "deny", by="user", reason="not yet", cwd=str(proj), scope="w1"
            )
            await asyncio.sleep(0.3)
            settled = gate.get(rec["id"])
            assert settled["status"] == "denied"
            assert settled["settled_elsewhere"] is True
            assert settled["decided_by"] == "user"
            assert settled["decided_reason"] == "not yet"
            # The run stayed where the refusal left it, and no second
            # settlement was written against it.
            assert engine.status(str(proj), scope="w1")["step_id"] == "two"
            assert _events(proj, "w1", "goto_approved") == []
            assert len(_events(proj, "w1", "goto_denied")) == 1
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_a_forced_third_position_settles_the_card_as_moot(home, tmp_path):
    """The operator's other answer: send the run somewhere neither the leader
    nor the graph asked for. The engine supersedes the request; the gate says
    moot and names what overtook it, rather than leaving a card for a
    question the run has already left behind."""
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        gate = app["goto_gate"]
        rec = gate.submit(
            session="lead", target_session="w1", step="three", reason="verified"
        )
        try:
            engine.goto("one", by="user", reason="redo it", cwd=str(proj), scope="w1")
            [listed] = [r for r in gate.list() if r["id"] == rec["id"]]
            assert listed["status"] == "moot"
            assert "one" in listed["error"]
            assert listed["settled_elsewhere"] is True
            assert engine.status(str(proj), scope="w1")["step_id"] == "one"
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_an_unreadable_run_leaves_the_card_standing(home, tmp_path):
    """Reconciliation settles on evidence of an answer, never on the absence
    of one: a run whose journal cannot be read is not a run that was
    answered, and a card taken down on a failed read is a live question
    nobody is looking at any more."""
    proj = _child_run(tmp_path)

    async def run():
        mgr = _manager(("lead", None, ""), ("w1", "lead", str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), goto_timeout=600)
        gate = app["goto_gate"]
        rec = gate.submit(
            session="lead", target_session="w1", step="three", reason="verified"
        )
        try:
            gate.records[rec["id"]]["cwd"] = str(tmp_path / "gone")
            [listed] = [r for r in gate.list() if r["id"] == rec["id"]]
            assert listed["status"] == "pending"
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())


def test_settling_with_nothing_pending_is_a_conflict(home, tmp_path):
    async def run():
        mgr = _manager(("lead", None, ""))
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = await _client(app)
        try:
            for verb in ("approve", "deny", "withdraw"):
                resp = await client.post(
                    f"/api/cflow/goto-requests/nope/{verb}",
                    json={"session": "lead"} if verb == "withdraw" else {},
                    headers=AUTH,
                )
                assert resp.status == 409, verb
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_endpoints_need_a_credential(home, tmp_path):
    async def run():
        mgr = _manager(("lead", None, ""))
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = await _client(app)
        try:
            assert (await client.get("/api/cflow/goto-requests")).status == 401
            assert (
                await client.post("/api/cflow/goto-requests", json={})
            ).status == 401
            assert (
                await client.post("/api/cflow/goto-requests/x/approve", json={})
            ).status == 401
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the MCP courier
# --------------------------------------------------------------------------- #
class _FakeDaemonClient:
    def __init__(self):
        self.posted = []

    def post(self, path, body=None, **kw):
        self.posted.append((path, body))
        if path.endswith("/withdraw"):
            return {"ok": True, "request": {"id": "g1", "status": "withdrawn"}}
        return {
            "ok": True,
            "request": {
                "id": "g1",
                "target_session": "w1",
                "status": "pending",
                "deadline": "2026-08-29T03:00:00+00:00",
            },
        }


def test_the_mcp_tool_files_under_the_calling_session(home, monkeypatch):
    from claude_launcher import daemon_client

    fake = _FakeDaemonClient()
    monkeypatch.setattr(daemon_client, "connect", lambda: fake)
    monkeypatch.setenv(state_mod.SESSION_ENV, "lead")
    mcp._seen_run = None

    payload = mcp.call_tool(
        "request_child_goto",
        {"session": "w1", "step": "three", "reason": "verified by git"},
    )
    assert payload["status"] == "child_goto_requested"
    assert fake.posted == [
        (
            "/api/cflow/goto-requests",
            {
                "session": "lead",
                "target_session": "w1",
                "step": "three",
                "reason": "verified by git",
            },
        )
    ]

    payload = mcp.call_tool("request_child_goto", {"withdraw": "g1"})
    assert payload["status"] == "child_goto_withdrawn"
    assert fake.posted[-1] == (
        "/api/cflow/goto-requests/g1/withdraw",
        {"session": "lead"},
    )


def test_the_mcp_tool_refuses_without_a_session_or_a_daemon(home, monkeypatch):
    from claude_launcher import daemon_client

    mcp._seen_run = None
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    with pytest.raises(CflowError, match="managed session"):
        mcp.call_tool(
            "request_child_goto",
            {"session": "w1", "step": "three", "reason": "x"},
        )

    monkeypatch.setenv(state_mod.SESSION_ENV, "lead")
    monkeypatch.setattr(daemon_client, "connect", lambda: None)
    with pytest.raises(CflowError, match="daemon is not running"):
        mcp.call_tool(
            "request_child_goto",
            {"session": "w1", "step": "three", "reason": "x"},
        )


# --------------------------------------------------------------------------- #
# the five minutes: the value, pinned
# --------------------------------------------------------------------------- #
def test_the_five_minute_default_is_pinned_in_both_places(home):
    """The user's spec ("5분 지나서 fallback 되면 자동 승인") lives in two
    constants that must agree: the gate's own default and the machine-config
    default. Every behavioral test injects its own timeout, so none of them
    can see either value — changing one 300.0 must make this red."""
    from claude_launcher import store

    assert goto_gate.GATE_TIMEOUT == 300.0
    assert store.DAEMON_DEFAULTS["goto_approval_timeout"] == 300.0
    assert store.daemon_config()["goto_approval_timeout"] == 300.0
