"""The driver's move request on a v2 run (docs/cflow-v2.md 2.11, claunch-kgfr5):
the driver asks for a ``force_target`` node, the run holds the driver until a
person answers, and the answer either moves the run (``granted``) or hands
the refusal back. A person's own move supersedes the request."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

# The worker's shape in small: rounds in a try body, a person's end decision
# in the finally. The end decision is where s931 stood when it took a new task.
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
          options:
            end: {then: []}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


class Drv:
    def __init__(self, tmp_path, src=WORKER):
        self.cwd = str(tmp_path)
        self.scope = "t"
        chart = frontend.compile_text(textwrap.dedent(src))
        self.st = runtime.start(chart, cwd=self.cwd, scope=self.scope, caps={}, now=T0)

    def kw(self):
        return {"cwd": self.cwd, "scope": self.scope, "now": T0}

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def do(self, *labels):
        for label in labels:
            hit = [t["instance"] for t in self.st["tasks"] if t["label"] == label]
            self.st = runtime.report(hit[0], "ok", None, **self.kw())
        return self.st

    def pick(self, option):
        inst = self.st["choices"][0]["instance"]
        self.st = runtime.select(inst, option, **self.kw())
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


def test_a_request_moves_nothing_and_holds_the_driver(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    st = runtime.request_move("intake", "new task arrived", by="s1", **d.kw())
    req = st["move_request"]
    assert req["node"] == "intake" and req["by"] == "s1" and req["id"].startswith("mr-")
    assert req["from"] and not req.get("decision")
    assert [t["label"] for t in st["tasks"]] == ["work"]  # nothing moved
    assert st["next"].startswith("MOVE REQUESTED")
    with pytest.raises(runtime.FlowError, match="nobody has answered"):
        d.do("work")
    ev = d.events("move_requested")[-1]
    assert ev["node"] == "intake" and ev["reason"] == "new task arrived"


def test_a_request_needs_a_reason_and_a_marked_node(tmp_path):
    d = Drv(tmp_path)
    with pytest.raises(runtime.FlowError, match="'reason' is required"):
        runtime.request_move("intake", "  ", by="s1", **d.kw())
    with pytest.raises(runtime.FlowError, match="not a force target"):
        runtime.request_move("work", "why", by="s1", **d.kw())


def test_approval_moves_out_of_the_end_decision_into_a_new_round(tmp_path):
    d = Drv(tmp_path)
    d.to_end_decision()
    runtime.request_move("intake", "user gave a new task", by="s1", **d.kw())
    # The driver cannot answer the user's choice while it holds a request,
    # and could not before either (chooser: user).
    with pytest.raises(runtime.FlowError):
        d.pick("end")
    st = runtime.resolve_move("approve", by="user", **d.kw())
    assert [t["label"] for t in st["tasks"]] == ["intake"]
    assert not st["choices"] and st["status"] == "running"
    assert st["move_request"]["decision"] == "approved"
    assert "move_request" not in RunStore(d.cwd, d.scope).load_state()
    forced = d.events("forced")[-1]
    assert forced["granted"] == st["move_request"]["id"] and forced["asked_by"] == "s1"
    assert d.events("move_approved")
    d.st = st
    d.do("intake", "work")  # the run goes on as an ordinary round
    assert [c["chooser"] for c in d.st["choices"]] == ["agent"]


def test_refusal_stays_for_the_driver_and_releases_it(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "redo", by="s1", **d.kw())
    st = runtime.resolve_move("deny", by="user", reason="finish this first", **d.kw())
    assert st["move_request"]["decision"] == "denied"
    assert [t["label"] for t in st["tasks"]] == ["work"]
    shown = runtime.status(cwd=d.cwd, scope=d.scope, now=T0, tick=False)
    assert shown["move_request"]["decided_reason"] == "finish this first"
    assert "REFUSED" in shown["next"]
    d.st = st
    d.do("work")  # no longer held
    assert d.events("move_denied")


def test_withdraw_and_answering_nothing_is_refused(tmp_path):
    d = Drv(tmp_path)
    with pytest.raises(runtime.FlowError, match="no pending move request"):
        runtime.resolve_move("approve", **d.kw())
    with pytest.raises(runtime.FlowError, match="no pending move request"):
        runtime.cancel_move_request(by="s1", **d.kw())
    runtime.request_move("intake", "x", by="s1", **d.kw())
    st = runtime.cancel_move_request(by="s1", **d.kw())
    assert "move_request" not in st
    assert d.events("move_withdrawn")
    d.st = st
    d.do("intake")


def test_a_persons_own_move_supersedes_the_request(tmp_path):
    d = Drv(tmp_path)
    d.do("intake")
    runtime.request_move("intake", "x", by="s1", **d.kw())
    st = runtime.force_move("intake", "do it this way", **d.kw())
    assert "move_request" not in st
    sup = d.events("move_superseded")[-1]
    assert sup["forced_to"] == "intake"
    assert "granted" not in d.events("forced")[-1]


def test_a_decision_must_be_approve_or_deny(tmp_path):
    d = Drv(tmp_path)
    runtime.request_move("intake", "x", by="s1", **d.kw())
    with pytest.raises(runtime.FlowError, match="approve' or 'deny"):
        runtime.resolve_move("maybe", **d.kw())


def test_a_v1_tool_points_a_v2_session_at_flow_request_move(tmp_path, monkeypatch):
    """s991 (2026-10-11) reached for the v1 ``request_goto`` from a v2 run, got
    "no active cflow run" and told the person to type ``claunch cflow2 move``,
    which its own shell refuses. The v1 tool now names the v2 one."""
    from claude_launcher.cflow import engine, mcp
    from claude_launcher.cflow import state as state_mod

    Drv(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "t")
    with pytest.raises(engine.CflowError) as err:
        mcp.call_tool("request_goto", {"step": "intake", "reason": "a new task"})
    assert "cflow v2 run" in str(err.value) and "flow_request_move" in str(err.value)
    with pytest.raises(engine.CflowError, match="flow_select"):
        mcp.call_tool("select", {"option": "x"})
    # no v2 run of this session here: the v1 answer stays as it was
    monkeypatch.setenv("CLAUNCH_SESSION", "u")
    with pytest.raises(state_mod.StateError, match="no active cflow run"):
        mcp.call_tool("request_goto", {"step": "intake", "reason": "a new task"})


# -- the report kind (claunch-vwmoj) ------------------------------------------
# s214's run stood on a task whose verify could not run where it stood
# (/usr/bin/python3 without claude_launcher), and its only way forward was to
# write the user a 'claunch cflow2 report ...' line to type. The driver now
# asks for that report with flow_request_move(outcome=...), and a person grants
# or refuses it like a move request.

VERIFIED = """
cflow: 2
name: v
nodes:
  policy:
    prompt: P
    verify: [check-policy]
  work:
    prompt: W
    returns:
      ok: {n: int}
  wrap: {prompt: X}
body:
  seq: [policy, work, wrap]
"""


class Runner:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, command, cwd, env, timeout):
        self.calls.append(command)
        return self.code, "no module named claude_launcher" if self.code else "", False


def _report_drv(tmp_path, runner):
    d = Drv(tmp_path, VERIFIED)
    st = runtime.report("policy", "ok", None, **d.kw(), verify_runner=runner)
    runtime.verify_join()
    st = runtime.status(cwd=d.cwd, scope=d.scope, now=T0, tick=False)
    assert st["tasks"][0]["last_verify"]["exit"] == 1
    d.st = st
    return d


def test_a_report_request_holds_the_driver_and_carries_the_failed_verify(tmp_path):
    d = _report_drv(tmp_path, Runner(1))
    st = runtime.request_move("policy", "verify cannot import claude_launcher", by="s1",
                              outcome="ok", **d.kw())
    req = st["move_request"]
    assert req["kind"] == "report" and req["instance"].startswith("policy@")
    assert req["outcome"] == "ok" and "skip_verify" not in req
    assert req["last_verify"]["exit"] == 1
    assert st["next"].startswith("REPORT REQUESTED")
    assert [t["label"] for t in st["tasks"]] == ["policy"]  # nothing reported yet
    with pytest.raises(runtime.FlowError, match="a person to report 'policy'"):
        runtime.report("policy", "ok", None, **d.kw())
    ev = d.events("move_requested")[-1]
    assert ev["kind"] == "report" and ev["outcome"] == "ok"


def test_a_granted_report_still_runs_the_verify_and_a_failure_keeps_the_task(tmp_path):
    runner = Runner(1)
    d = _report_drv(tmp_path, runner)
    runtime.request_move("policy", "verify cannot run here", by="s1", outcome="ok", **d.kw())
    st = runtime.resolve_move("approve", by="user", reason="checked by hand", **d.kw(),
                              verify_runner=runner)
    assert st["move_request"]["decision"] == "approved"
    assert "move_request" not in RunStore(d.cwd, d.scope).load_state()
    runtime.verify_join()
    assert len(runner.calls) == 2  # the driver's report, then the person's grant
    st = runtime.status(cwd=d.cwd, scope=d.scope, now=T0, tick=False)
    assert [t["label"] for t in st["tasks"]] == ["policy"]


def test_granting_runs_the_verify(tmp_path):
    d = _report_drv(tmp_path, Runner(1))
    runtime.request_move("policy", "the import path is fixed now", by="s1", outcome="ok",
                         **d.kw())
    ok = Runner(0)
    st = runtime.resolve_move("approve", by="user", **d.kw(), verify_runner=ok)
    assert st["verifying"]["task"] == "policy"
    runtime.verify_join()
    st = runtime.status(cwd=d.cwd, scope=d.scope, now=T0, tick=False)
    assert ok.calls and [t["label"] for t in st["tasks"]] == ["work"]
    rep = d.events("reported")[-1]
    assert rep["by"] == "user" and "s1's request" in rep["text"]


def test_a_report_request_is_checked_when_filed(tmp_path):
    d = Drv(tmp_path, VERIFIED)
    with pytest.raises(runtime.FlowError, match="'reason' is required"):
        runtime.request_move("policy", " ", by="s1", outcome="ok", **d.kw())
    with pytest.raises(runtime.FlowError, match="not active|no active|task"):
        runtime.request_move("work", "x", by="s1", outcome="ok", value={"n": 1}, **d.kw())
    d.st = runtime.report("policy", "ok", None, **d.kw(), verify_runner=Runner(0))
    runtime.verify_join()
    with pytest.raises(runtime.FlowError, match="refused"):
        runtime.request_move("work", "x", by="s1", outcome="ok", value={"n": "one"}, **d.kw())
    with pytest.raises(runtime.FlowError, match="give 'outcome' too"):
        runtime.request_move("intake", "x", by="s1", value={"n": 1}, **d.kw())


def test_refusing_a_report_request_releases_the_driver(tmp_path):
    d = _report_drv(tmp_path, Runner(1))
    runtime.request_move("policy", "x", by="s1", outcome="ok", **d.kw())
    st = runtime.resolve_move("deny", by="user", reason="fix the verify", **d.kw())
    assert st["move_request"]["decision"] == "denied"
    shown = runtime.status(cwd=d.cwd, scope=d.scope, now=T0, tick=False)
    assert "report request for 'policy' was REFUSED" in shown["next"]
    assert [t["label"] for t in shown["tasks"]] == ["policy"]
    runtime.report("policy", "fail", None, **d.kw())  # the driver may report again


def test_a_persons_own_report_supersedes_a_report_request(tmp_path):
    d = Drv(tmp_path, VERIFIED)
    d.st = runtime.report("policy", "ok", None, **d.kw(), verify_runner=Runner(0))
    runtime.verify_join()
    runtime.request_move("work", "x", by="s1", outcome="ok", value={"n": 1}, **d.kw())
    st = runtime.report("work", "ok", {"n": 2}, "by hand", **d.kw(), by="user")
    assert "move_request" not in st
    assert d.events("move_superseded")[-1]["reported"].startswith("work@")
    assert [t["label"] for t in st["tasks"]] == ["wrap"]
