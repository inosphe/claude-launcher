"""A person's gate on a child run is the parent session's to answer
(claunch-vjp4y).

The rule under test: a ``chooser: user`` choice and the driver's move/report
request are answered by a person -- and, when the driving session has a parent,
by that parent too. Nobody else, and never the driver itself (a grant the asker
can give itself is not a grant). The parent edge is the session registry's
``sdef.parent``, copied into the run as ``driver_parent``; a run with no
parent, a stale edge, or a dead daemon loses only the delegation and keeps the
person.

Three surfaces are covered: the engine (``select``/``resolve_move``/
``authorize``), the MCP tools a parent calls (``flow_gates``, ``flow_select``,
``flow_move``), and the daemon line that tells the parent (``RunEventClock``'s
``child-gate``).
"""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from claude_launcher.cflow2 import frontend, mcp, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

# Rounds in a try body with a person's end decision in the finally: the shape
# every worker run has when it stops on ``end``/``new-task``.
WORKER = """
cflow: 2
name: t
nodes:
  intake: {prompt: I, force_target: true}
  work: {prompt: W}
  wrap: {prompt: X}
body:
  try:
    id: rounds
    loop:
      while: "true"
      max: 3
      do:
        seq:
          - intake
          - work
          - choose:
              chooser: agent
              options:
                more: {then: []}
                stop: {then: {break: rounds}}
  finally:
    seq:
      - wrap
      - choose:
          chooser: user
          prompt: the round is over -- take a new task, hold, or end?
          options:
            new-task: {then: []}
            hold: {then: []}
            end: {then: []}
"""


# The same worker, with the reason a person would type made mandatory: the
# parent answers as an agent, so the requirement reaches it too.
WORKER_STRICT = WORKER.replace(
    "          chooser: user\n", "          chooser: user\n          require_reason: true\n")


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


class Drv:
    """A run driven by ``w1``, whose parent is ``boss``."""

    def __init__(self, tmp_path, *, parent="boss", driver="w1", src=WORKER, scope=None):
        self.cwd = str(tmp_path)
        self.scope = scope or driver
        chart = frontend.compile_text(textwrap.dedent(src))
        self.st = runtime.start(chart, cwd=self.cwd, scope=self.scope, caps={}, now=T0,
                                driver=driver, driver_parent=parent)

    def kw(self):
        return {"cwd": self.cwd, "scope": self.scope, "now": T0}

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()

    def do(self, *labels):
        for label in labels:
            hit = [t["instance"] for t in self.st["tasks"] if t["label"] == label]
            self.st = runtime.report(hit[0], "ok", None, **self.kw())
        return self.st

    def pick(self, option, **kw):
        inst = self.st["choices"][0]["instance"]
        self.st = runtime.select(inst, option, **{**self.kw(), **kw})
        return self.st

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope)
                if e["event"] == name]

    def to_end_decision(self):
        self.do("intake", "work")
        self.pick("stop")
        self.do("wrap")
        assert self.st["choices"] and self.st["choices"][0]["chooser"] == "user"
        return self.st


# --------------------------------------------------------------------------- #
# the engine's authority
# --------------------------------------------------------------------------- #
def test_gate_authority_is_the_person_or_the_parent(tmp_path):
    d = Drv(tmp_path)
    state = d.state()
    assert runtime.driver_parent_of(state) == "boss"
    assert runtime.gate_authority(state, "boss") == runtime.CALLER_PARENT
    assert runtime.gate_authority(state, "w1") == ""        # the driver never
    assert runtime.gate_authority(state, "sibling") == ""
    assert runtime.gate_authority(state, "") == runtime.CALLER_USER


def test_no_parent_leaves_the_gate_a_persons(tmp_path):
    d = Drv(tmp_path, parent="")
    state = d.state()
    assert runtime.driver_parent_of(state) == ""
    assert runtime.gate_authority(state, "boss") == ""
    assert runtime.gate_authority(state, "") == runtime.CALLER_USER


def test_authorize_gate_opens_for_the_parent_only(tmp_path):
    d = Drv(tmp_path)
    assert runtime.authorize(cwd=d.cwd, scope=d.scope, session="w1",
                             gate=True) == runtime.CALLER_AGENT
    assert runtime.authorize(cwd=d.cwd, scope=d.scope, session="boss",
                             gate=True) == runtime.CALLER_PARENT
    with pytest.raises(runtime.FlowError, match="may not operate it"):
        runtime.authorize(cwd=d.cwd, scope=d.scope, session="sibling", gate=True)
    # without the gate door nothing changed: a parent may not drive the child
    with pytest.raises(runtime.FlowError, match="may not operate it"):
        runtime.authorize(cwd=d.cwd, scope=d.scope, session="boss")


# --------------------------------------------------------------------------- #
# a chooser: user choice
# --------------------------------------------------------------------------- #
def test_the_parent_answers_the_users_end_decision(tmp_path):
    d = Drv(tmp_path)
    d.to_end_decision()
    inst = d.st["choices"][0]["instance"]
    # the driver is refused, exactly as before
    with pytest.raises(runtime.FlowError, match="chooser: user"):
        d.pick("end")
    st = d.pick("end", caller=runtime.CALLER_PARENT, by="boss")
    assert st["status"] == "done"
    # a finished run's status returns early, so the pick is read from the run
    record = d.state()["person_picks"][-1]
    assert record["option"] == "end" and record["by"] == "boss"
    assert record["delegated"] is True
    event = d.events("selected")[-1]
    assert event["by"] == "boss" and event["delegated"] is True


def test_a_persons_pick_is_still_journaled_as_the_persons(tmp_path):
    d = Drv(tmp_path)
    d.to_end_decision()
    d.pick("end", caller=runtime.CALLER_USER)
    record = d.state()["person_picks"][-1]
    assert record["by"] == "user" and record["delegated"] is False
    assert "delegated" not in d.events("selected")[-1]


def test_a_parent_pick_still_obeys_a_required_reason(tmp_path):
    """The parent answers as an agent, not as a person: a choice that demands
    a reason still demands one from it."""
    d = Drv(tmp_path, src=WORKER_STRICT)
    d.to_end_decision()
    with pytest.raises(runtime.FlowError, match="requires a reason"):
        d.pick("end", caller=runtime.CALLER_PARENT, by="boss")
    d.st = d.pick("end", reason="the round is over", caller=runtime.CALLER_PARENT,
                  by="boss")
    assert d.st["status"] == "done"
    # a person may still answer without one (docs 2.11)
    other = Drv(tmp_path / "p", src=WORKER_STRICT)
    other.to_end_decision()
    assert other.pick("end", caller=runtime.CALLER_USER)["status"] == "done"


# --------------------------------------------------------------------------- #
# the move / report request
# --------------------------------------------------------------------------- #
def test_the_parent_answers_a_move_request(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "the user gave a new task", by="w1", **d.kw())
    st = runtime.resolve_move("approve", by="boss", caller=runtime.CALLER_PARENT,
                              reason="a new task arrived", **d.kw())
    assert [t["label"] for t in st["tasks"]] == ["intake"]
    assert st["move_request"]["decision"] == "approved"
    event = d.events("move_approved")[-1]
    assert event["by"] == "boss"


def test_the_parent_refuses_and_the_driver_reads_why(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "redo", by="w1", **d.kw())
    st = runtime.resolve_move("deny", by="boss", caller=runtime.CALLER_PARENT,
                              reason="finish this round first", **d.kw())
    assert st["move_request"]["decision"] == "denied"
    assert st["move_request"]["decided_by"] == "boss"
    assert "finish this round first" in st["next"]


def test_no_other_hand_grants_a_move_request(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "x", by="w1", **d.kw())
    with pytest.raises(runtime.FlowError, match="may not answer"):
        runtime.resolve_move("approve", by="sibling", caller=runtime.CALLER_PARENT, **d.kw())
    # the driver cannot grant its own request either
    with pytest.raises(runtime.FlowError, match="may not answer"):
        runtime.resolve_move("approve", by="w1", caller=runtime.CALLER_PARENT, **d.kw())
    # and the affordance never existed for a caller that says it is a person
    st = runtime.resolve_move("approve", by="user", **d.kw())
    assert st["move_request"]["decision"] == "approved"


# --------------------------------------------------------------------------- #
# read: what the driver and the daemon see
# --------------------------------------------------------------------------- #
def test_status_names_the_parent_and_its_hand(tmp_path):
    d = Drv(tmp_path)
    assert d.st["driver_parent"] == "boss"
    d.to_end_decision()
    assert "boss" in d.st["next"] and "flow_gates" in d.st["next"]


def test_a_run_without_a_parent_says_nothing_about_one(tmp_path):
    d = Drv(tmp_path, parent="")
    d.to_end_decision()
    assert "driver_parent" not in d.st
    assert "brief them and stop your turn" in d.st["next"]


# --------------------------------------------------------------------------- #
# the MCP tools a parent calls
# --------------------------------------------------------------------------- #
def test_flow_gates_lists_a_childs_person_gates(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    d.to_end_decision()
    monkeypatch.setattr(mcp, "_live_children", lambda session: [("w1", d.cwd)])
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    out = mcp.call_tool("flow_gates", {})
    rows = out["children"]
    assert [r["child"] for r in rows] == ["w1"]
    row = rows[0]
    choice = row["choices"][0]
    assert "new task" in choice["prompt"]
    assert {o["name"] for o in choice["options"]} == {"new-task", "hold", "end"}
    assert all(o["available"] for o in choice["options"])
    assert "move_request" not in row or row["move_request"] is None


def test_flow_gates_is_silent_about_a_child_that_owes_nothing(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    monkeypatch.setattr(mcp, "_live_children", lambda session: [("w1", d.cwd)])
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    assert mcp.call_tool("flow_gates", {})["children"] == []


def test_flow_select_from_the_parent_answers_the_childs_choice(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    d.to_end_decision()
    inst = d.st["choices"][0]["instance"]
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    out = mcp.call_tool("flow_select", {"node": inst, "option": "end",
                                        "reason": "the round is finished",
                                        "scope": "w1", "cwd": d.cwd})
    assert out["status"] == "done"
    record = d.state()["person_picks"][-1]
    assert record["delegated"] is True and record["by"] == "boss"


def test_the_parents_answer_wakes_the_childs_session(tmp_path, monkeypatch):
    """The child's turn had ended -- that is why the gate was open. Filing the
    answer in its run files does not wake it, so the daemon types a line."""
    from claude_launcher.cflow2 import asks

    d = Drv(tmp_path)
    d.to_end_decision()
    inst = d.st["choices"][0]["instance"]
    calls = []
    monkeypatch.setattr(asks, "nudge_driver",
                        lambda message, *, cwd=None, scope=None: calls.append((message, scope)))
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    mcp.call_tool("flow_select", {"node": inst, "option": "end", "reason": "done",
                                  "scope": "w1", "cwd": d.cwd})
    assert calls, "the child was not woken"
    message, scope = calls[0]
    assert "boss" in message and "flow_status" in message
    assert scope == "w1"


def test_flow_move_from_the_parent_answers_the_childs_request(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "a new task", by="w1", **d.kw())
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    out = mcp.call_tool("flow_move", {"decision": "approve", "reason": "a new round",
                                      "scope": "w1", "cwd": d.cwd})
    assert [t["label"] for t in out["tasks"]] == ["intake"]


def test_flow_move_refuses_the_driver_itself(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "a new task", by="w1", **d.kw())
    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    with pytest.raises(runtime.FlowError, match="as its parent"):
        mcp.call_tool("flow_move", {"decision": "approve", "cwd": d.cwd})


def test_an_unrelated_session_cannot_read_or_answer(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    d.to_end_decision()
    monkeypatch.setenv("CLAUNCH_SESSION", "sibling")
    with pytest.raises(runtime.FlowError, match="may not operate it"):
        mcp.call_tool("flow_status", {"scope": "w1", "cwd": d.cwd})
    with pytest.raises(runtime.FlowError, match="may not operate it"):
        mcp.call_tool("flow_select", {"option": "end", "scope": "w1", "cwd": d.cwd})


def test_the_parent_reads_the_childs_run_without_ticking_its_wait(tmp_path, monkeypatch):
    d = Drv(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    out = mcp.call_tool("flow_status", {"scope": "w1", "cwd": d.cwd})
    assert out["run"] and out["driver_parent"] == "boss"


# --------------------------------------------------------------------------- #
# the daemon line that tells the parent
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str, parent=None, *, held_at=None):
        from claude_launcher.daemon.harness import SessionDef

        self.exited = False
        self.ended_by = None
        self.held_at = held_at
        self.sdef = SessionDef(name=name, cwd=cwd, parent=parent)
        self.delivered: list = []
        self.status_value = "idle"

    def status(self, threshold=None):
        return self.status_value

    def append_wal(self, text: str) -> bool:
        return True

    async def deliver(self, text: str, **kwargs) -> bool:
        self.delivered.append(text)
        return True

    def kill(self, *, force: bool = False) -> None:
        self.exited = True


class _FakeManager:
    def __init__(self, sessions):
        self._sessions = dict(sessions)

    def get(self, name):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())

    def persist(self):
        pass


def test_the_daemon_tells_the_parent_when_its_child_stops(tmp_path):
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    boss = tmp_path / "boss"
    boss.mkdir()
    manager = _FakeManager({"w1": _FakeSession("w1", d.cwd, parent="boss"),
                            "boss": _FakeSession("boss", str(boss))})
    clock = cflow_clock.RunEventClock(manager)
    assert clock.scan() == []                       # first sight arms
    # the registry edge lands on the run within one pass
    assert d.state().get("driver_parent") == "boss"
    # nothing owed yet: no line
    assert clock.scan() == []
    d.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    event = events[0]
    assert event["to"] == "boss"
    assert "flow_gates" in event["block"] and "flow_select" in event["block"]
    assert "choose:" in event["block"]
    # the same gate is not news twice
    assert clock.scan() == []


def test_a_child_without_a_parent_sends_nothing(tmp_path):
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    manager = _FakeManager({"w1": _FakeSession("w1", d.cwd, parent=None)})
    clock = cflow_clock.RunEventClock(manager)
    clock.scan()
    d.to_end_decision()
    assert clock.scan() == []
    assert d.state().get("driver_parent") == ""


def test_a_nested_runs_gate_is_not_pushed_to_the_parent(tmp_path):
    """A nested run (a ``calls`` slot, ``<main>~cN``) is not the session's own
    run: ``parent`` in its state names the slot, not a session, and
    ``rail_entry``/``flow_gates`` never list it. The line for it would name the
    main run from ``session_of(scope)`` and point the parent at a gate that is
    not there, so the clock stays out of it entirely (peer review of
    claunch-vjp4y)."""
    from claude_launcher.daemon import cflow_clock

    main = Drv(tmp_path, parent="")
    nested = Drv(tmp_path, parent="", scope="w1~c1")
    # what the engine writes when it starts the slot (runtime._new_run)
    store = RunStore(nested.cwd, nested.scope)
    state = store.load_state()
    state["parent"] = {"cwd": nested.cwd, "scope": "w1", "call": "c1", "run": "x"}
    with store.lock():
        store.save_state(state)

    manager = _FakeManager({"w1": _FakeSession("w1", main.cwd, parent="boss"),
                            "boss": _FakeSession("boss", str(tmp_path / "boss"))})
    (tmp_path / "boss").mkdir(exist_ok=True)
    clock = cflow_clock.RunEventClock(manager)
    assert clock.scan() == []                       # first sight arms
    nested.to_end_decision()
    assert [c["chooser"] for c in nested.st["choices"]] == ["user"]
    assert clock.scan() == []                       # the nested gate is silent
    # and nothing was written into the slot's state: its gates stay a person's
    assert nested.state().get(runtime.DRIVER_PARENT) in (None, "")
    # the main run is still the one the parent hears about
    main.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    assert f"session: {main.scope}" in events[0]["block"]


def test_a_dead_parent_leaves_the_gate_alone(tmp_path):
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="boss")
    # the parent is not a session on this daemon any more
    manager = _FakeManager({"w1": _FakeSession("w1", d.cwd, parent="boss")})
    clock = cflow_clock.RunEventClock(manager)
    clock.scan()
    d.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    # nothing is typed anywhere: the delivery finds no live parent and settles
    assert asyncio.run(clock._deliver(events[0])) is True
    # the gate itself is untouched -- the person still answers it
    assert runtime.gate_authority(d.state(), "boss") == runtime.CALLER_PARENT
    assert runtime.gate_authority(d.state(), "") == runtime.CALLER_USER


# --------------------------------------------------------------------------- #
# a held child is still a child (claunch-7vf2z)
# --------------------------------------------------------------------------- #
# `hold` stops the program and leaves the run live with its question open
# (daemon/session.Session.hold sets held_at, then kills). Every surface below
# used to read that record as dead, so a held child's gate reached nobody.
class _FakeDaemon:
    """The daemon client `mcp._live_children` talks to."""

    def __init__(self, children, cwds=None):
        self.children = children
        self.cwds = cwds or {}

    def get(self, path):
        if path.endswith("/children"):
            return {"children": self.children}
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return {"cwd": self.cwds.get(name, "")}


def test_the_children_row_says_whether_a_child_is_held(home, tmp_path):
    """The row the parent's own MCP path reads. Without `held` a parent cannot
    tell a held child from one that ended (daemon/api.h_session_children)."""
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from test_cflow_hold_wake import HELD_AT, add_dead
    from test_daemon_e2e import _manager

    async def run():
        from claude_launcher.daemon.harness import SessionDef

        mgr = _manager()
        cwd = str(tmp_path)
        add_dead(mgr, "boss", cwd)
        add_dead(mgr, "kid", cwd, held_at=HELD_AT)
        add_dead(mgr, "gone", cwd)
        for name in ("kid", "gone"):
            mgr.get(name).sdef = SessionDef(
                name=name, harness="claude", cwd=cwd, parent="boss")

        client = TestClient(TestServer(build_app(mgr, "sekrit", started_at=0.0)))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/sessions/boss/children",
                headers={"Authorization": "Bearer sekrit"},
            )
            assert resp.status == 200, await resp.text()
            return (await resp.json())["children"]
        finally:
            await client.close()

    rows = asyncio.run(run())
    by_name = {row["name"]: row for row in rows}
    assert by_name["kid"]["held"] is True
    assert by_name["gone"]["held"] is False


def test_flow_gates_lists_a_held_childs_gate(tmp_path, monkeypatch):
    """The discovery path: `_live_children` keeps the held row, so the gate it
    waits on reaches `flow_gates` (cflow2/mcp._live_children)."""
    d = Drv(tmp_path)
    d.to_end_decision()
    rows = [
        {"name": "w1", "status": "exited", "held": True},
        {"name": "gone", "status": "exited", "held": False},
    ]
    monkeypatch.setattr(mcp, "_daemon",
                        lambda: _FakeDaemon(rows, {"w1": d.cwd, "gone": d.cwd}))
    monkeypatch.setenv("CLAUNCH_SESSION", "boss")
    out = mcp.call_tool("flow_gates", {})
    # the held child is listed with its open choice ...
    assert [r["child"] for r in out["children"]] == ["w1"]
    assert "new task" in out["children"][0]["choices"][0]["prompt"]
    # ... and the dead one, on the same answer, is still left out
    assert "gone" not in [r["child"] for r in out["children"]]


def test_a_held_childs_gate_reaches_the_clock(tmp_path):
    """The scan path: a held child's run is walked, so its `driver_parent` is
    kept current and its gate is announced (cflow_clock._scan_v2_gates)."""
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    child = _FakeSession("w1", d.cwd, parent="boss")
    child.exited = True
    child.held_at = "2026-11-06T00:00:00+00:00"
    boss = _FakeSession("boss", str(tmp_path / "boss"))
    (tmp_path / "boss").mkdir()
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": child, "boss": boss}))
    assert clock.scan() == []                       # first sight arms
    assert d.state().get("driver_parent") == "boss"
    d.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    assert events[0]["to"] == "boss"


def test_an_exited_child_that_is_not_held_is_not_scanned(tmp_path):
    """The narrowing that keeps the change small: exited without held_at is
    still nobody's run, exactly as before."""
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    child = _FakeSession("w1", d.cwd, parent="boss")
    child.exited = True
    boss = _FakeSession("boss", str(tmp_path / "boss"))
    (tmp_path / "boss").mkdir()
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": child, "boss": boss}))
    clock.scan()
    d.to_end_decision()
    assert clock.scan() == []


def test_a_held_parent_is_told_and_woken(tmp_path):
    """A parent that is itself held is woken by the delivery, so the gate is
    not lost behind the parent's own hold (cflow_clock.RunEventClock._deliver)."""
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    boss = _FakeSession("boss", str(tmp_path / "boss"), held_at="2026-11-06T00:00:00+00:00")
    boss.exited = True
    (tmp_path / "boss").mkdir()
    clock = cflow_clock.RunEventClock(
        _FakeManager({"w1": _FakeSession("w1", d.cwd, parent="boss"), "boss": boss})
    )
    clock.scan()
    d.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    assert asyncio.run(clock._deliver(events[0])) is True
    assert boss.delivered and "flow_gates" in boss.delivered[0]


def test_a_dead_parent_is_still_not_written_to(tmp_path):
    """Client-side counterpart of `test_a_dead_parent_leaves_the_gate_alone`:
    the wider predicate reaches held records only."""
    from claude_launcher.daemon import cflow_clock

    d = Drv(tmp_path, parent="")
    boss = _FakeSession("boss", str(tmp_path / "boss"))
    boss.exited = True                              # exited, no held_at
    (tmp_path / "boss").mkdir()
    clock = cflow_clock.RunEventClock(
        _FakeManager({"w1": _FakeSession("w1", d.cwd, parent="boss"), "boss": boss})
    )
    clock.scan()
    d.to_end_decision()
    events = clock.scan()
    assert [e["kind"] for e in events] == ["child-gate"]
    assert asyncio.run(clock._deliver(events[0])) is True
    assert boss.delivered == []
