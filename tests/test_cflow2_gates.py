"""cflow v2 task gates: verify, ask (human / self / delegated), procedure param
defaults (docs/cflow-v2.md 2.2)."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from claude_launcher import install
from claude_launcher.cflow import responders
from claude_launcher.cflow2 import asks, executors, frontend, runtime
from claude_launcher.cflow2 import mcp as cflow2_mcp
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_approve, cflow2_clock

T0 = 1_000_000.0
SCOPE = "w1"


@pytest.fixture(autouse=True)
def _no_dispatcher(monkeypatch):
    executors.register(None)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    yield
    executors.register(None)


def _compile(src):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))


class Drv:
    def __init__(self, tmp_path, src, params=None, scope=SCOPE):
        self.cwd = str(tmp_path)
        self.scope = scope
        self.st = runtime.start(_compile(src), cwd=self.cwd, scope=scope, params=params,
                                caps={}, now=T0)

    def kw(self, now=None):
        return {"cwd": self.cwd, "scope": self.scope, "now": T0 if now is None else now}

    def status(self, now=None):
        self.st = runtime.status(**self.kw(now))
        return self.st

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def report(self, ref, outcome="ok", value=None, runner=None, now=None):
        """Report and wait for a background verify to come back."""
        self.st = runtime.report(ref, outcome, value, verify_runner=runner, **self.kw(now))
        if self.st.get("verifying"):
            runtime.verify_join(30)
            self.st = runtime.status(**self.kw(now))
        return self.st

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope)
                if e["event"] == name]

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()


# ---------------------------------------------------------------------------
# verify


def test_verify_spellings_normalise():
    chart = _compile("""
        nodes:
          s: {prompt: s, verify: "pytest -q"}
          l: {prompt: l, verify: [python, -c, "pass"]}
          m: {prompt: m, verify: {run: "make t", timeout: 30}}
          c: {prompt: c, verify: {command: "x"}}
          n: {prompt: n}
        body: [s, l, m, c, n]
    """)
    assert chart.node("s").get("verify") == {"run": "pytest -q", "timeout": None}
    assert chart.node("l").get("verify") == {"run": ["python", "-c", "pass"], "timeout": None}
    assert chart.node("m").get("verify") == {"run": "make t", "timeout": 30}
    assert chart.node("c").get("verify") == {"run": "x", "timeout": None}
    assert chart.node("n").get("verify") is None
    assert runtime.verify_spec(chart.node("s"))["timeout"] == runtime.DEFAULT_VERIFY_TIMEOUT
    for bad in ('{run: x, nope: 1}', '{timeout: 3}', '7', '{run: x, timeout: 0}'):
        with pytest.raises(frontend.FrontendError):
            _compile("nodes:\n  a: {prompt: a, verify: %s}\nbody: a\n" % bad)


def test_verify_pass_carries_run_env_and_proceeds(tmp_path):
    d = Drv(tmp_path, """
        params: {who: {default: me}, n: {default: 3}}
        nodes:
          a: {prompt: a, verify: 'check "$CFLOW_IN_WHO"'}
          b: {prompt: b}
        body: [a, b]
    """)
    seen = {}

    def runner(cmd, cwd, env, timeout):
        seen.update(cmd=cmd, cwd=cwd, timeout=timeout,
                    env={k: v for k, v in env.items() if k.startswith(("CFLOW2_", "CFLOW_IN_"))})
        return 0, "fine"

    first = runtime.report("a", verify_runner=runner, **d.kw())
    assert first["verifying"]["task"] == "a"  # the call returns before verify ran
    runtime.verify_join(30)
    d.status()
    assert d.tasks() == ["b"]
    # E1: the value reaches the command only as an environment variable
    assert seen["cmd"] == 'check "$CFLOW_IN_WHO"' and seen["timeout"] == 900.0
    assert seen["cwd"] == str(tmp_path.resolve())
    env = seen["env"]
    assert env["CFLOW2_RUN"] == d.state()["run_id"] and env["CFLOW2_NODE"] == "a"
    assert env["CFLOW_IN_WHO"] == "me" and env["CFLOW_IN_N"] == "3"
    assert env["CFLOW2_SCOPE"] == f"{SCOPE}@main"  # the slot names its daemon
    assert d.events("verify_passed")[0]["exit"] == 0


def test_verify_failure_refuses_the_report_and_keeps_the_task(tmp_path):
    d = Drv(tmp_path, "nodes:\n  a: {prompt: a, verify: t}\n  b: {prompt: b}\nbody: [a, b]\n")
    out = "\n".join(f"line {i}" for i in range(100)) + "\nFAILED test_x"
    st = d.report("a", runner=lambda *a: (1, out))
    (task,) = st["tasks"]
    assert task["label"] == "a"
    lv = task["last_verify"]
    assert lv["why"] == "exit 1" and "FAILED test_x" in lv["tail"] and "line 0\n" not in lv["tail"]
    ev = d.events("verify_failed")[0]
    assert ev["exit"] == 1 and "FAILED test_x" in ev["tail"]
    assert d.events("verify_started")
    assert not d.events("reported")
    # a non-ok outcome is not verified
    d.report("a", "fail", runner=lambda *a: pytest.fail("verify ran for a fail report"))
    assert d.state()["status"] == "failed"


def test_verify_timeout_kills_the_command(tmp_path):
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    d = Drv(tmp_path, "nodes:\n  a: {prompt: a, verify: {run: %s, timeout: 0.5}}\nbody: a\n"
            % json.dumps(cmd))
    st = d.report("a")
    assert st["tasks"][0]["last_verify"]["why"] == "timed out after 0.5s"
    assert d.events("verify_failed")[0]["timed_out"] is True
    assert d.status()["tasks"][0]["label"] == "a"


def test_verify_runs_outside_the_run_lock(tmp_path):
    """The verify command itself takes the run's lock without blocking: it can
    only succeed if report() is not holding it while the command runs."""
    st = RunStore(str(tmp_path), SCOPE)
    probe = textwrap.dedent(f"""
        import fcntl, os, sys
        fd = os.open({str(st.dir / ".lock")!r}, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("run lock is held during verify")
            sys.exit(3)
        fcntl.flock(fd, fcntl.LOCK_UN)
        print("lock free")
    """)
    script = tmp_path / "probe.py"
    script.write_text(probe, encoding="utf-8")
    pytest.importorskip("fcntl")
    d = Drv(tmp_path, "nodes:\n  a: {prompt: a, verify: %s}\nbody: a\n"
            % json.dumps([sys.executable, str(script)]))
    # control: the probe does detect a held lock
    import subprocess
    with st.lock():
        held = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert held.returncode == 3
    d.report("a")
    assert d.state()["status"] == "done"
    assert d.events("verify_passed")


def test_a_run_that_moved_while_verify_ran_applies_nothing(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: a, verify: t}
          b: {prompt: b}
        body:
          par: {branches: {x: a, y: b}, settle: race}
    """)

    def runner(*_a):
        # the other branch wins the race while the command runs
        runtime.report("b", cwd=d.cwd, scope=d.scope, now=T0)
        return 0, ""

    d.report("a", runner=runner)
    assert [e["node"] for e in d.events("reported")] == ["b"]
    assert not d.events("verify_passed")
    assert d.state()["status"] == "done"


# ---------------------------------------------------------------------------
# ask: human


ASK_HUMAN = """
    nodes:
      a: {prompt: do a, ask: {prompt: "may I a?"}}
      b: {prompt: b}
    body:
      try: [a, b]
      catch:
        declined: b
"""


def test_human_ask_parks_the_task_and_a_person_approves(tmp_path):
    d = Drv(tmp_path, ASK_HUMAN)
    st = d.status()
    assert st["tasks"] == [] and st["status"] == "waiting_approval" and st["reason"] == "ask"
    (a,) = st["asks"]
    assert a["resolution"] == "human" and a["prompt"] == "may I a?" and a["kind"] == "approval"
    assert a["task_prompt"] == "do a" and "claunch cflow2 approve" in a["how"]
    assert d.events("fallback")[0]["route"] == "ask -> human"
    with pytest.raises(runtime.FlowError, match="waits for its ask"):
        d.report("a")
    with pytest.raises(runtime.FlowError, match="not yours to answer"):
        runtime.approve("a", caller=runtime.CALLER_AGENT, **d.kw())
    out = asks.human_approve("a", reason="go", nudge_fn=lambda *a: None, **d.kw())
    assert out["answered"]["decision"] == "approve"
    assert out["status"] == "running" and [t["label"] for t in out["tasks"]] == ["a"]
    ev = d.events("ask_answered")[0]
    assert ev["by"] == "user" and ev["channel"] == "human" and ev["reason"] == "go"


def test_human_decline_finishes_the_task_declined(tmp_path):
    d = Drv(tmp_path, ASK_HUMAN)
    runtime.approve("a", decline=True, reason="no", **d.kw())
    d.status()
    assert d.tasks() == ["b"]
    left = [e for e in d.events("left") if e["node"] == "a"]
    assert left[0]["outcome"] == "declined"
    assert d.state()["result"]["a"]["value"]["reason"] == "no"


def test_on_decline_names_the_outcome(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: a, ask: {prompt: p, on_decline: skip}}
          b: {prompt: b}
        body:
          try: a
          catch: {skip: b}
    """)
    runtime.approve(None, decline=True, **d.kw())
    assert d.status()["tasks"][0]["label"] == "b"
    with pytest.raises(frontend.FrontendError, match="on_decline"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, on_decline: ok}}\nbody: a\n")


def test_ask_is_required_again_on_every_visit(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: a, ask: {prompt: p}}
        body:
          loop: {while: "true", max: 3, do: a}
    """)
    ids = set()
    for _ in range(2):
        st = d.status()
        ids.add(st["asks"][0]["ask"])
        runtime.approve("a", **d.kw())
        d.report("a")
    assert len(ids) == 2 and len(d.state()["asks"]) == 3


def test_ask_cancelled_with_its_instance(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: a, ask: {prompt: p}}
          b: {prompt: b}
        body:
          par: {branches: {x: a, y: b}, settle: race}
    """)
    assert d.status()["status"] == "waiting_approval"
    d.report("b")
    s = d.state()
    assert s["status"] == "done"
    assert [r["status"] for r in s["asks"].values()] == ["cancelled"]


# ---------------------------------------------------------------------------
# ask: otherwise self


def test_self_fallthrough_lets_the_driver_approve(tmp_path, monkeypatch):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: a, ask: {prompt: p, otherwise: self}}
        body: a
    """)
    st = d.status()
    assert st["status"] == "running" and st["asks"][0]["resolution"] == "self"
    assert "flow_approve" in st["asks"][0]["how"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", SCOPE)  # the driver's own MCP server
    out = cflow2_mcp.call_tool("flow_approve", {"node": "a", "reason": "nobody else"})
    assert [t["label"] for t in out["tasks"]] == ["a"]
    assert d.events("ask_answered")[0]["channel"] == "self"


def test_self_with_default_option_takes_it_unanswered(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          r:
            prompt: r
            ask:
              prompt: review?
              otherwise: "self:pass"
              options:
                pass: {outcome: ok, description: fine}
                changes: {description: fix}
            returns: {ok: {}, changes: {}}
          b: {prompt: b}
        body: [r, b]
    """)
    assert d.status()["tasks"][0]["label"] == "b"
    ev = d.events("ask_answered")[0]
    assert ev["decision"] == "pass" and ev["by"] == "unanswered"


def test_ask_options_validation():
    with pytest.raises(frontend.FrontendError, match="names no option"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, otherwise: 'self:x', "
                 "options: {y: {}}}}\nbody: a\n")
    with pytest.raises(frontend.FrontendError, match="approval has none"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, otherwise: 'self:x'}}\nbody: a\n")
    with pytest.raises(frontend.FrontendError, match="unknown key"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, gate: 1}}\nbody: a\n")
    with pytest.raises(frontend.FrontendError, match="scope"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, from: [{role: r, scope: x}]}}\n"
                 "body: a\n")


# ---------------------------------------------------------------------------
# ask: delegated


def _member(handle, role, session=None):
    return responders.Responder(session=session or f"s-{handle}", handle=handle, role=role,
                                local=True, reachability="idle")


def _pool(**kw):
    members = {"lead": _member("lead", "leader"), "rev": _member("rev", "reviewer")}
    base = dict(mesh="m", me="w1", members=members, reachable={"lead", "rev"},
                ancestors=["lead"], siblings={"rev"})
    base.update(kw)
    return lambda **_: responders.Pool(**base)


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, rec, ctx):
        self.sent.append((rec, ctx))
        return None


DELEGATED = """
    nodes:
      a:
        prompt: a
        ask:
          prompt: "ship it?"
          from: [{role: reviewer, scope: sibling}, {role: leader, scope: ancestor}]
          otherwise: human
          timeout: 60
      b: {prompt: b}
    body:
      try: [a, b]
      catch: {declined: b}
"""


def _route(d, pool=None, out=None, now=T0):
    return asks.route(cwd=d.cwd, scope=d.scope, now=now, pool_fn=pool or _pool(),
                      deliver_fn=out or Outbox())


def test_delegated_ask_is_routed_and_answered_by_the_responder(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    assert d.status()["asks"][0]["resolution"] == "routing"
    out = Outbox()
    _route(d, out=out)
    (a,) = d.status()["asks"]
    assert a["resolution"] == "asked" and a["asked"] == ["rev"]
    assert a["deadline"] == T0 + 60 and d.state()["status"] == "running"
    rec, ctx = out.sent[0]
    assert ctx["mesh"] == "m" and ctx["sender"] == "w1"
    text = asks.question_text(rec, workflow="t", sender="w1", cwd=d.cwd, scope=d.scope)
    assert rec["id"] in text and "flow_answer" in text and "ship it?" in text
    assert d.events("ask_routed")[0]["to"] == ["rev"]
    # the index lets the responder answer by id alone
    assert asks.pending_for("s-rev")[0]["ask_id"] == rec["id"]
    assert asks.pending_for("s-lead") == []
    with pytest.raises(runtime.FlowError, match="no open request"):
        asks.answer_by_id(rec["id"], "approve", by_session="s-lead", nudge_fn=lambda *a: [])
    nudged = []
    receipt = asks.answer_by_id(rec["id"], "approve", "looks right", by_session="s-rev",
                                now=T0, nudge_fn=lambda *a: nudged.append(a) or ["w1"])
    assert receipt["decision"] == "approve" and receipt["as"] == "rev"
    assert nudged and nudged[0][0] == SCOPE
    assert d.status()["tasks"][0]["label"] == "a"
    assert asks.pending_for("s-rev") == []


def test_the_run_cannot_answer_its_own_ask(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    members = {"self": _member("self", "reviewer", session=SCOPE)}
    _route(d, pool=_pool(members=members, reachable={"self"}, siblings={"self"}))
    rec = next(iter(d.state()["asks"].values()))
    with pytest.raises(runtime.FlowError, match="own run"):
        runtime.answer(rec["id"], "approve", by_session=SCOPE, **d.kw())
    with pytest.raises(runtime.FlowError, match="not yours to answer"):
        runtime.approve(None, caller=runtime.CALLER_AGENT, **d.kw())
    with pytest.raises(runtime.FlowError, match="no session identity"):
        runtime.answer(rec["id"], "approve", by_session="", **d.kw())


def test_cli_gate_commands_are_denied_to_the_agent():
    for cmd in ("approve", "select", "extend", "resume"):
        for tool in ("Bash", "PowerShell"):
            assert f"{tool}(claunch cflow2 {cmd})" in install.GATE_DENY_RULES
            assert f"{tool}(claunch cflow2 {cmd}:*)" in install.GATE_DENY_RULES
    assert not any("cflow2 status" in r for r in install.GATE_DENY_RULES)


def test_timeouts_hop_down_the_from_list_then_otherwise(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    out = Outbox()
    _route(d, out=out)
    # not due yet
    asks.tick(cwd=d.cwd, scope=d.scope, now=T0 + 30, pool_fn=_pool(), deliver_fn=out)
    assert d.status(T0 + 30)["asks"][0]["asked"] == ["rev"]
    notes = asks.tick(cwd=d.cwd, scope=d.scope, now=T0 + 61, pool_fn=_pool(), deliver_fn=out)
    assert notes == []
    a = d.status(T0 + 61)["asks"][0]
    assert a["asked"] == ["lead"] and a["deadline"] == T0 + 121
    assert d.events("ask_timeout")[0]["asked"] == ["rev"]
    assert [rec["asked"][0]["handle"] for rec, _ in out.sent] == ["rev", "lead"]
    notes = asks.tick(cwd=d.cwd, scope=d.scope, now=T0 + 200, pool_fn=_pool(), deliver_fn=out)
    a = d.status(T0 + 200)["asks"][0]
    assert a["resolution"] == "human" and d.state()["status"] == "waiting_approval"
    assert len(notes) == 1 and "a person decides" in notes[0]
    # said once
    assert asks.tick(cwd=d.cwd, scope=d.scope, now=T0 + 300, pool_fn=_pool(),
                     deliver_fn=out) == []
    fb = [e for e in d.events("fallback") if e.get("decision") == "ask"]
    assert fb[-1]["route"] == "ask -> human" and "no candidate group left" in fb[-1]["cause"]


def test_no_candidate_falls_back_with_the_reasons(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    _route(d, pool=lambda **_: responders.Pool(problem="the daemon is not running"))
    a = d.status()["asks"][0]
    assert a["resolution"] == "human" and len(a["skipped"]) == 2
    causes = [e["cause"] for e in d.events("fallback")]
    assert any("the daemon is not running" in c for c in causes)


def test_unreadable_roster_defers_instead_of_spending_groups(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    silent = lambda **_: responders.Pool(problem="no reply", unreadable=True)  # noqa: E731
    _route(d, pool=silent)
    rec = next(iter(d.state()["asks"].values()))
    assert rec["resolution"] == "routing" and rec["group"] == 0 and rec["deferrals"] == 1
    assert _route(d, pool=silent) == []  # not before retry_at
    _route(d, now=T0 + runtime.ROSTER_RETRY + 1)
    assert d.status(T0 + 31)["asks"][0]["asked"] == ["rev"]


# -- responders preview (claunch-3va8h) -----------------------------------------


REVIEW = """
    nodes:
      w: {prompt: w}
      review:
        prompt: r
        ask:
          prompt: "pass?"
          from:
            - {role: reviewer, scope: descendant}
            - {role: reviewer, scope: sibling, connect: true}
            - {role: leader, scope: ancestor}
          otherwise: "self:pass"
          options: {pass: {outcome: ok}, changes: {outcome: changes}}
    body: [w, review]
"""


class _NoWire(responders.Pool):
    def _wire_to(self, matched):  # pragma: no cover - failing is the assertion
        raise AssertionError("a preview must not wire")


def _preview_pool(**kw):
    members = {"lead": _member("lead", "leader"), "rev": _member("rev", "reviewer")}
    base = dict(mesh="m", me="w1", members=members, reachable={"lead"},
                ancestors=["lead"], siblings={"rev"})
    base.update(kw)
    return lambda **_: _NoWire(**base)


def test_responders_lists_every_group_and_wires_nothing(tmp_path):
    d = Drv(tmp_path, REVIEW)  # the ask is not open yet: w is the active task
    out = asks.who_answers("review", cwd=d.cwd, scope=d.scope, pool_fn=_preview_pool())
    assert out["node"] == "review" and out["me"] == "w1" and out["mesh"] == "m"
    desc, sib, lead = out["groups"]
    assert desc["asks"] == [] and "reason" in desc
    # connect: true lists the unwired reviewer it would wire when the ask opens
    assert sib["asks"] == ["rev"] and sib["relations"] == {"rev": "sibling"}
    assert lead["asks"] == ["lead"] and lead["relations"] == {"lead": "ancestor"}
    assert out["resolves"] is True and out["unreadable"] is False
    assert not d.state().get("asks")  # read-only: no ask was opened


def test_responders_role_filter_keeps_the_leader_from_answering(tmp_path):
    d = Drv(tmp_path, REVIEW)
    nobody = _preview_pool(members={"lead": _member("lead", "leader")}, siblings=set())
    out = asks.who_answers("review", role="Reviewer", cwd=d.cwd, scope=d.scope,
                           pool_fn=nobody)
    assert [g["candidate"] for g in out["groups"]] == [
        "reviewer (descendant)", "reviewer (sibling, connect)"]
    assert out["resolves"] is False
    full = asks.who_answers("review", cwd=d.cwd, scope=d.scope, pool_fn=nobody)
    assert full["resolves"] is True  # only through the leader group


def test_responders_cannot_tell_without_a_run_a_node_or_an_ask(tmp_path):
    with pytest.raises(runtime.FlowError, match="no cflow2 run"):
        asks.who_answers("review", cwd=str(tmp_path), scope=SCOPE, pool_fn=_preview_pool())
    d = Drv(tmp_path, REVIEW)
    with pytest.raises(runtime.FlowError, match="no node 'nope'"):
        asks.who_answers("nope", cwd=d.cwd, scope=d.scope, pool_fn=_preview_pool())
    with pytest.raises(runtime.FlowError, match="asks nobody"):
        asks.who_answers("w", cwd=d.cwd, scope=d.scope, pool_fn=_preview_pool())


def test_responders_cli_exit_codes(tmp_path, monkeypatch, capsys):
    from claude_launcher import cli_cflow2

    d = Drv(tmp_path, REVIEW)
    monkeypatch.chdir(tmp_path)

    def run(node, role="", pool=None):
        monkeypatch.setattr(asks, "default_pool", pool or _preview_pool())
        args = SimpleNamespace(node=node, role=role, scope=d.scope, json=False)
        return cli_cflow2._cmd_responders(args)

    assert run("review", "reviewer") == 0
    assert "reviewer (sibling, connect): rev (sibling)" in capsys.readouterr().out
    lonely = _preview_pool(members={"lead": _member("lead", "leader")}, siblings=set())
    assert run("review", "reviewer", lonely) == 1
    assert run("review", "", _preview_pool(problem="no reply", unreadable=True)) == 2
    assert run("nope") == 2
    assert "no node 'nope'" in capsys.readouterr().err
    monkeypatch.chdir(tmp_path / "..")
    assert run("review") == 2  # no v2 run in this directory


def test_abstain_moves_to_the_next_group(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    out = Outbox()
    _route(d, out=out)
    rec = next(iter(d.state()["asks"].values()))
    asks.answer_by_id(rec["id"], "abstain", "no idea", by_session="s-rev",
                      pool_fn=_pool(), deliver_fn=out, now=T0, nudge_fn=lambda *a: [])
    assert d.status()["asks"][0]["asked"] == ["lead"]
    asks.answer_by_id(rec["id"], "decline", "not now", by_session="s-lead",
                      nudge_fn=lambda *a: [], now=T0)
    assert d.status()["tasks"][0]["label"] == "b"


def test_delegated_decision_maps_the_option_to_the_outcome(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          r:
            prompt: prepare the review material
            ask:
              prompt: review?
              from: [{role: reviewer, scope: sibling}]
              options:
                pass: {outcome: ok}
                changes: {description: fix it}
            returns: {ok: {}, changes: {}}
          b: {prompt: b}
          c: {prompt: c}
        body:
          try: [r, b]
          catch: {changes: c}
    """)
    _route(d)
    a = d.status()["asks"][0]
    assert [o["name"] for o in a["options"]] == ["pass", "changes"]
    assert a["task_prompt"] == "prepare the review material"
    with pytest.raises(runtime.FlowError, match="unknown decision"):
        runtime.answer(a["ask"], "approve", by_session="s-rev", **d.kw())
    runtime.answer(a["ask"], "changes", "see board", by_session="s-rev", **d.kw())
    assert d.status()["tasks"][0]["label"] == "c"
    assert d.state()["result"]["r"]["value"]["decision"] == "changes"


def test_a_person_answers_over_the_responders_and_they_are_told(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    _route(d, now=time.time())  # the web handler answers on the real clock
    told = []
    out = cflow2_approve.approve(d.cwd, d.scope, {"node": "a", "decision": "approve",
                                                   "reason": "mine"},
                                 withdraw_fn=lambda ans, sender: told.append((ans, sender)),
                                 nudge_fn=lambda *a: None)
    assert out["answered"]["decision"] == "approve"
    assert [t["label"] for t in out["tasks"]] == ["a"]
    (ans, sender), = told
    assert [e["handle"] for e in ans["pending"]] == ["rev"] and sender == SCOPE
    assert asks.pending_for("s-rev") == []
    # a late responder answer is refused
    with pytest.raises(runtime.FlowError):
        runtime.answer(ans["ask"], "approve", by_session="s-rev", **d.kw())


def test_web_handler_decline_and_pending(tmp_path):
    d = Drv(tmp_path, ASK_HUMAN)
    assert cflow2_approve.pending(d.cwd, d.scope)["asks"][0]["resolution"] == "human"
    cflow2_approve.approve(d.cwd, d.scope, {"decision": "decline"}, nudge_fn=lambda *a: None)
    assert d.status()["tasks"][0]["label"] == "b"
    with pytest.raises(cflow2_approve.ERRORS):
        cflow2_approve.approve(d.cwd, d.scope, {}, nudge_fn=lambda *a: None)


def test_the_daemon_clock_routes_and_expires_asks(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    session = SimpleNamespace(sdef=SimpleNamespace(name=SCOPE, cwd=d.cwd), exited=False,
                              queue_delivery=lambda text: None)
    mgr = SimpleNamespace(list=lambda: [session], get=lambda name: session)
    out = Outbox()
    clock = cflow2_clock.Cflow2Clock(mgr, heartbeat=False, threaded=False, probe_runner=None,
                                     ask_pool=_pool(), ask_deliver=out)
    clock.scan(now=T0)
    assert d.status()["asks"][0]["asked"] == ["rev"]
    clock.scan(now=T0 + 61)
    clock.scan(now=T0 + 122)
    notices = clock.scan(now=T0 + 123)
    assert d.status(T0 + 123)["asks"][0]["resolution"] == "human"
    assert len(out.sent) == 2
    assert notices == []  # the fall was announced on the tick it happened


def test_mcp_lists_the_new_tools_and_answers_by_identity(tmp_path, monkeypatch):
    names = {t["name"] for t in cflow2_mcp.TOOLS}
    assert {"flow_approve", "flow_answer", "flow_asks"} <= names
    d = Drv(tmp_path, DELEGATED)
    _route(d, now=time.time())  # flow_answer runs on the real clock
    rec = next(iter(d.state()["asks"].values()))
    monkeypatch.setattr(asks, "default_nudge", lambda *a: None)
    monkeypatch.setenv("CLAUNCH_SESSION", "s-rev")
    listed = cflow2_mcp.call_tool("flow_asks", {})
    assert listed["asks"][0]["ask_id"] == rec["id"]
    cflow2_mcp.call_tool("flow_answer", {"ask_id": rec["id"], "decision": "approve"})
    assert d.status()["tasks"][0]["label"] == "a"


# ---------------------------------------------------------------------------
# procedure param defaults


PROC = """
    procedures:
      p:
        params:
          who: {default: anon}
          n: {type: int, default: 2}
          need: {required: true}
        nodes:
          t: {prompt: "hi ${params.who} ${params.n} ${params.need}"}
        body: t
"""


def test_call_applies_param_defaults(tmp_path):
    d = Drv(tmp_path, PROC + """
    body:
      call: p
      with: {need: x}
    """)
    assert d.status()["tasks"][0]["prompt"] == "hi anon 2 x"


def test_fork_applies_defaults_and_refuses_missing_required(tmp_path):
    d = Drv(tmp_path, PROC + """
    nodes:
      lead: {prompt: lead}
    body:
      group: {name: g, run: p, do: lead}
    """)
    with pytest.raises(runtime.FlowError, match="missing required param.*need"):
        runtime.fork("g", {}, **d.kw())
    with pytest.raises(runtime.FlowError, match="unknown param"):
        runtime.fork("g", {"need": 1, "bogus": 2}, **d.kw())
    runtime.fork("g", {"need": "y", "who": "bo"}, **d.kw())
    prompts = sorted(t["prompt"] for t in d.status()["tasks"])
    assert prompts == ["hi bo 2 y", "lead"]


# -- the run's mesh and unroutable asks (claunch-5itbf) -------------------------


UNROUTABLE = """
    nodes:
      r:
        prompt: prepare the review material
        ask:
          prompt: review?
          from: [{role: reviewer, scope: sibling}, {role: leader, scope: ancestor}]
          otherwise: "self:pass"
          on_unroutable: unrouted
          options: {pass: {outcome: ok}, changes: {outcome: changes}}
        returns: {ok: {}, changes: {}, unrouted: {reason: str, meshes: "list?"}}
      b: {prompt: b}
      c: {prompt: "c ${result.r.value.meshes}"}
    body:
      try: [r, b]
      catch: {unrouted: c}
"""


def _ambiguous(**_):
    return responders.Pool(problem="session 'w1' is in several meshes (m, t)",
                           ambiguous=("m", "t"))


def test_an_unroutable_ask_finishes_with_on_unroutable_not_otherwise(tmp_path):
    d = Drv(tmp_path, UNROUTABLE)
    out = Outbox()
    _route(d, pool=_ambiguous, out=out)
    assert out.sent == []
    st = d.status()
    assert st["tasks"][0]["label"] == "c" and '["m", "t"]' in st["tasks"][0]["prompt"]
    value = d.state()["result"]["r"]["value"]
    assert value["meshes"] == ["m", "t"] and "several meshes" in value["reason"]
    rec = next(iter(d.state()["asks"].values()))
    assert rec["status"] == "unroutable" and not d.state()["approvals"]
    fb = [e for e in d.events("fallback") if e.get("decision") == "ask"]
    assert fb[-1]["route"] == "ask -> unrouted"
    assert not d.events("ask_answered")  # nobody answered, not even "unanswered"


def test_nobody_holding_the_role_still_takes_otherwise(tmp_path):
    d = Drv(tmp_path, UNROUTABLE)
    _route(d, pool=_pool(members={}, reachable=set(), ancestors=[], siblings=set()))
    assert d.status()["tasks"][0]["label"] == "b"  # self:pass, unanswered
    assert d.events("ask_answered")[0]["by"] == "unanswered"


def test_without_on_unroutable_an_unroutable_ask_falls_to_otherwise(tmp_path):
    d = Drv(tmp_path, DELEGATED)
    _route(d, pool=_ambiguous)
    assert d.status()["asks"][0]["resolution"] == "human"


def test_on_unroutable_validation():
    with pytest.raises(frontend.FrontendError, match="on_unroutable cannot be 'ok'"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, from: [{role: r}], "
                 "on_unroutable: ok}}\nbody: a\n")
    with pytest.raises(frontend.FrontendError, match="outcome name"):
        _compile("nodes:\n  a: {prompt: a, ask: {prompt: p, from: [{role: r}], "
                 "on_unroutable: 'bad name'}}\nbody: a\n")


def test_the_run_mesh_is_kept_and_used_for_routing(tmp_path):
    seen = []

    def pool(**kw):
        seen.append(kw["mesh"])
        return _pool()(**kw)

    st = runtime.start(_compile(DELEGATED), cwd=str(tmp_path), scope=SCOPE, caps={}, now=T0,
                       mesh="team")
    assert st["mesh"] == "team"
    asks.route(cwd=str(tmp_path), scope=SCOPE, now=T0, pool_fn=pool, deliver_fn=Outbox())
    assert seen == ["team"]
    assert asks.who_answers("a", cwd=str(tmp_path), scope=SCOPE, pool_fn=pool)["mesh"] == "m"
    assert seen[-1] == "team"
    asks.who_answers("a", cwd=str(tmp_path), scope=SCOPE, mesh="other", pool_fn=pool)
    assert seen[-1] == "other"


def test_set_mesh_reroutes_a_routing_ask_and_only_the_driver_writes_it(tmp_path):
    d = Drv(tmp_path, UNROUTABLE)
    seen = []

    def pool(**kw):
        seen.append(kw["mesh"])
        return _pool()(**kw) if kw["mesh"] == "m" else _ambiguous()

    silent = lambda **_: responders.Pool(problem="no reply", unreadable=True)  # noqa: E731
    _route(d, pool=silent)  # deferred: still routing
    with pytest.raises(runtime.FlowError, match="may not name its mesh"):
        runtime.set_mesh("m", session="intruder", **d.kw())
    st = runtime.set_mesh("m", session=SCOPE, **d.kw())
    assert st["mesh"] == "m"
    assert d.events("mesh_set")[-1]["mesh"] == "m"
    _route(d, pool=pool)
    assert seen == ["m"] and d.status()["asks"][0]["asked"] == ["rev"]
    # a person's terminal may name it too; empty clears it
    assert "mesh" not in runtime.set_mesh("", caller=runtime.CALLER_USER, **d.kw())


def test_home_mesh_is_the_one_mesh_or_nothing():
    assert asks.home_mesh("", pool_fn=_pool()) == ""
    assert asks.home_mesh("w1", pool_fn=_pool()) == "m"
    assert asks.home_mesh("w1", pool_fn=_ambiguous) == ""


def test_responders_cli_says_cannot_tell_for_several_meshes(tmp_path, monkeypatch, capsys):
    from claude_launcher import cli_cflow2

    d = Drv(tmp_path, REVIEW)
    monkeypatch.chdir(tmp_path)
    seen = []

    def pool(**kw):
        seen.append(kw["mesh"])
        return _preview_pool()(**kw) if kw["mesh"] == "m" else _ambiguous()

    monkeypatch.setattr(asks, "default_pool", pool)

    def run(mesh=None):
        return cli_cflow2._cmd_responders(SimpleNamespace(
            node="review", role="reviewer", scope=d.scope, json=False, mesh=mesh))

    assert run() == 2
    assert "claunch cflow2 mesh" in capsys.readouterr().err
    assert run("m") == 0 and seen[-1] == "m"
    runtime.set_mesh("m", caller=runtime.CALLER_USER, cwd=d.cwd, scope=d.scope)
    assert run() == 0 and seen[-1] == "m"


def test_cli_mesh_shows_and_names_the_run_mesh(tmp_path, monkeypatch, capsys):
    from claude_launcher import cli_cflow2

    d = Drv(tmp_path, REVIEW)
    monkeypatch.chdir(tmp_path)

    def run(name=None, clear=False):
        return cli_cflow2._guarded(cli_cflow2._cmd_mesh)(
            SimpleNamespace(name=name, clear=clear, scope=d.scope))

    assert run() == 0 and "none named" in capsys.readouterr().out
    assert run("team") == 0 and "mesh = team" in capsys.readouterr().out
    assert d.state()["mesh"] == "team"
    monkeypatch.setenv("CLAUNCH_SESSION", "someone-else")
    assert run("x") == 1 and "may not operate" in capsys.readouterr().err
    monkeypatch.delenv("CLAUNCH_SESSION")
    assert run(clear=True) == 0 and d.state()["mesh"] == ""
