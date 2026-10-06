"""cflow v2 fixes from the claunch-qc2tk reviews (A1, A2, B1, B2, C1, C2, D,
E1-E4, F1-F4) and typed start inputs (acceptance): each test is a reviewer
repro or the rule the fix states (docs/cflow-v2.md 2.2, 4, 5, 7)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from claude_launcher import install
from claude_launcher.cflow import responders
from claude_launcher.cflow2 import asks, executors, expr, frontend, ir, runtime
from claude_launcher.cflow2 import mcp as cflow2_mcp
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_approve

T0 = 1_000_000.0
SCOPE = "w1"
SUBAGENT_CAPS = {"subagent.spawn": True, "mcp.claunch": True}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    executors.register(None)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    sent = []
    monkeypatch.setattr(runtime, "WITHDRAW_FN", lambda item, sender: sent.append((item, sender)))
    yield sent
    runtime.verify_join(30)
    executors.register(None)


def _compile(src):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))


class Drv:
    def __init__(self, tmp_path, src, *, caps=None, scope=SCOPE, driver=None, chart=None,
                 params=None, inputs=None, now=T0):
        self.cwd = str(tmp_path)
        self.scope = scope
        self.now = now
        self.st = runtime.start(chart or _compile(src), cwd=self.cwd, scope=scope,
                                params=params, caps=caps if caps is not None else {}, now=now,
                                driver=driver, inputs=inputs)

    def kw(self, **over):
        out = {"cwd": self.cwd, "scope": self.scope, "now": self.now}
        out.update(over)
        return out

    def status(self):
        self.st = runtime.status(**self.kw())
        return self.st

    def tasks(self):
        return sorted(t["label"] for t in self.st.get("tasks", []))

    def inst(self, label):
        (iid,) = [t["instance"] for t in self.st["tasks"] if t["label"] == label]
        return iid

    def report(self, ref, outcome="ok", value=None, runner=None):
        self.st = runtime.report(ref, outcome, value, verify_runner=runner, **self.kw())
        if self.st.get("verifying"):
            runtime.verify_join(30)
            self.status()
        return self.st

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope) if e["event"] == name]

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()


# ---------------------------------------------------------------------------
# A1: a halt holds the whole run


HALT = """
    nodes:
      a: {prompt: A}
      b: {prompt: B}
      c: {prompt: C}
      m: {prompt: M}
    data: {tip: abc}
    body:
      par: {branches: {x: [a, b], y: c, z: {group: {name: g, run: p, do: m}}}}
    procedures:
      p:
        nodes:
          t: {prompt: T}
        body: t
    spec:
      monitors:
        tip:
          always: "$probe.tip == $data.tip"
          probe: {tip: "git rev-parse HEAD"}
          every: 60
          on_violation: halt
"""


def _halt(d):
    found = runtime.monitors_tick(T0 + 1, lambda cmd, cwd: (0, "zzz"), cwd=d.cwd,
                                  scope=d.scope)
    assert found and found[0]["action"] == "halt"


def test_a_halt_refuses_progress_everywhere_until_resumed(tmp_path):
    d = Drv(tmp_path, HALT)
    _halt(d)
    st = d.status()
    assert st["status"] == "waiting_approval" and st["reason"] == "monitor_violation"
    assert st["halted"] == ["tip"] and "HALTED" in st["next"]
    for call in (lambda: runtime.report("a", **d.kw()),
                 lambda: runtime.report("c", **d.kw()),
                 lambda: runtime.fork("g", {}, **d.kw())):
        with pytest.raises(runtime.FlowError, match="halted by monitor tip"):
            call()
    # the reviewer's repro: nothing reached done, and the hold is still there
    st = d.state()
    assert st["status"] == "waiting_approval" and st["approvals"][0]["kind"] == "monitor_violation"
    runtime.release_halt(**d.kw())
    d.status()
    d.report(d.inst("a"))
    assert "b" in d.tasks()


def test_a_halt_stops_time_moves_and_select(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          seq:
            - wait: {seconds: 10}
            - choose: {options: {go: {then: a}, stop: {then: b}}}
        spec:
          monitors:
            m: {always: "false", every: 1, on_violation: halt}
    """)
    runtime.monitors_tick(T0, lambda *a: (0, ""), cwd=d.cwd, scope=d.scope)
    runtime.wait_tick(T0 + 100, cwd=d.cwd, scope=d.scope)
    assert d.state()["inst"] and not any(
        i.get("await") == "choose" for i in d.state()["inst"].values())
    runtime.release_halt(**d.kw())
    d.st = runtime.wait_tick(T0 + 100, cwd=d.cwd, scope=d.scope)
    assert d.st["choices"]
    runtime.monitors_tick(T0 + 200, lambda *a: (0, ""), cwd=d.cwd, scope=d.scope)
    d.st = runtime.status(**d.kw())
    # the monitor stayed violated: no second halt until it held in between
    assert not d.st.get("halted")


def test_a_human_ask_holds_only_its_own_task(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A, ask: {prompt: "ok?"}}
          c: {prompt: C}
        body:
          par: {branches: {x: a, y: c}}
    """)
    st = d.status()
    assert st["status"] == "waiting_approval" and st["reason"] == "ask"
    with pytest.raises(runtime.FlowError, match="waits for its ask"):
        runtime.report("a", **d.kw())
    d.report(d.inst("c"))  # the other branch moves on
    assert d.events("reported")[0]["node"] == "c"


def test_a_daemon_result_during_a_halt_is_kept_and_applied_on_resume(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, executor: [subagent]}
          after: {prompt: after}
        body: [t, after]
        spec:
          monitors:
            m: {always: "false", every: 1, on_violation: halt}
    """, caps=SUBAGENT_CAPS)
    did = d.st["delegations"][0]["id"]
    runtime.monitors_tick(T0, lambda *a: (0, ""), cwd=d.cwd, scope=d.scope)
    with pytest.raises(runtime.FlowError, match="halted"):
        runtime.delegate_submit(did, "ok", {}, via=runtime.VIA_MCP, session="", **d.kw())
    runtime.executor_submit(did, "ok", {}, cwd=d.cwd, scope=d.scope, now=T0)
    assert d.state()["delegations"][did]["held"]
    assert d.status()["tasks"] == []
    d.st = runtime.release_halt(**d.kw())
    assert d.tasks() == ["after"]


def test_finish_keeps_a_run_wide_hold_on_record(tmp_path):
    d = Drv(tmp_path, "nodes:\n  a: {prompt: A}\nbody: a\n")
    r = runtime.Run.load(RunStore(d.cwd, d.scope), T0)
    r.state["approvals"].append({"kind": "monitor_violation", "monitor": "m",
                                 "instance": None})
    runtime._finish_run(r, "ok", None)
    assert r.state["status"] == "done"
    assert r.state["approvals"] == [{"kind": "monitor_violation", "monitor": "m",
                                     "instance": None}]


# ---------------------------------------------------------------------------
# A2: delegated subtrees with control structure run as nested runs


NESTED = """
    nodes:
      a: {prompt: "A ${params.who}"}
      ship: {prompt: ship}
      hold: {prompt: hold}
      after: {prompt: after}
    params: {who: {default: me}}
    body:
      seq:
        - id: unit
          executor: [subagent, inline]
          seq:
            - a
            - choose: {options: {go: {then: ship}, wait: {then: hold}}}
        - after
"""


def test_a_subtree_with_a_choice_becomes_a_nested_run(tmp_path):
    d = Drv(tmp_path, NESTED, caps=SUBAGENT_CAPS, driver="drv")
    (dl,) = d.st["delegations"]
    nested = dl["nested"]
    assert nested["scope"] == f"{SCOPE}~{dl['id']}"
    assert "nested run" in dl["brief"]["submit"] and dl["brief"]["nested"] == nested
    # the delegate drives it with the normal tools, under the nested scope
    kw = {"cwd": nested["cwd"], "scope": nested["scope"], "now": T0}
    st = runtime.status(**kw)
    assert st["driver"] == "drv" and st["parent"]["delegation"] == dl["id"]
    assert [t["prompt"] for t in st["tasks"]] == ["A me"]
    runtime.report("a", **kw)
    runtime.select(None, "go", **kw)
    runtime.report("ship", **kw)
    assert runtime.status(**kw)["status"] == "done"
    # the nested run's result came back as the delegation's
    d.status()
    assert d.tasks() == ["after"]
    rec = d.state()["delegations"][dl["id"]]
    assert rec["status"] == "returned"
    # a direct submission for it is refused
    with pytest.raises(runtime.FlowError):
        runtime.delegate_submit(dl["id"], "ok", {}, **d.kw())


def test_nested_run_authority_follows_the_delegation(tmp_path):
    d = Drv(tmp_path, NESTED, caps=SUBAGENT_CAPS, driver="drv")
    nested = d.st["delegations"][0]["nested"]
    state = RunStore(nested["cwd"], nested["scope"]).load_state()
    assert runtime.may_drive(state, "drv")
    assert not runtime.may_drive(state, "intruder")
    with pytest.raises(runtime.FlowError, match="driven by drv"):
        runtime.authorize(cwd=nested["cwd"], scope=nested["scope"], session="intruder")


def test_cancelling_the_delegation_aborts_its_nested_run(tmp_path):
    d = Drv(tmp_path, NESTED, caps=SUBAGENT_CAPS)
    nested = d.st["delegations"][0]["nested"]
    runtime.abort("stop", **d.kw())
    assert RunStore(nested["cwd"], nested["scope"]).load_state()["status"] == "aborted"


def test_a_user_gate_inside_a_delegated_subtree_runs_inline(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A}
          ship: {prompt: ship}
          hold: {prompt: hold}
        body:
          id: unit
          executor: [subagent]
          seq:
            - a
            - choose: {chooser: user, options: {ship: {then: ship}, hold: {then: hold}}}
    """, caps=SUBAGENT_CAPS)
    assert not d.st.get("delegations")
    assert d.tasks() == ["a"]
    fb = [e for e in d.events("fallback") if "person's gate" in str(e.get("cause"))]
    assert fb and fb[0]["route"].endswith("-> inline")
    d.report(d.inst("a"))
    with pytest.raises(runtime.FlowError, match="user's"):
        runtime.select(None, "ship", **d.kw())


def test_a_flat_delegation_runs_its_tasks_verify_before_accepting(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A, verify: "check-a"}
          b: {prompt: B}
          after: {prompt: after}
        body:
          seq:
            - {seq: [a, b], executor: [subagent], id: pair}
            - after
    """, caps=SUBAGENT_CAPS)
    did = d.st["delegations"][0]["id"]
    assert "verify of a" in d.st["delegations"][0]["brief"]["submit"]
    ran = []
    out = runtime.delegate_submit(did, "ok", {}, verify_runner=lambda c, *a: ran.append(c)
                                  or (1, "nope"), **d.kw())
    assert out["verifying"]["tasks"] == ["a"]
    runtime.verify_join(30)
    st = d.status()
    assert ran == ["check-a"] and st["tasks"] == []
    assert d.state()["delegations"][did]["status"] == "pending"  # refused once, re-run
    assert d.events("verify_failed")[0]["delegation"] == did
    runtime.delegate_submit(did, "ok", {}, verify_runner=lambda *a: (0, ""), **d.kw())
    runtime.verify_join(30)
    d.status()
    assert d.tasks() == ["after"]


# ---------------------------------------------------------------------------
# B1, B2


def test_a_cancelled_loop_limit_leaves_no_waiting_status(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          timeout:
            seconds: 10
            do: {loop: {while: "true", max: 1, do: a}}
            else: b
    """)
    d.report("a")
    assert d.state()["reason"] == "loop_limit"
    d.st = runtime.wait_tick(T0 + 11, cwd=d.cwd, scope=d.scope)
    st = d.state()
    assert st["approvals"] == [] and st["status"] == "running" and st["reason"] is None
    assert d.tasks() == ["b"]


@pytest.mark.parametrize("text, env", [
    ("$data.m[$data.k] == 1", {"data": {"m": {"x": 1}, "k": [1, 2]}}),
    ("1" * 5000 + " == 1", {}),
    ("(" * 5000 + "1" + ")" * 5000, {}),
    ("not " * 5000 + "true", {}),
])
def test_expressions_raise_only_expr_error(text, env):
    with pytest.raises(expr.ExprError):
        expr.evaluate(text, env)


def test_deep_run_data_is_an_expr_error():
    deep = [1]
    for _ in range(3000):
        deep = [deep]
    with pytest.raises(expr.ExprError):
        expr.evaluate("$data.x == $data.y", {"data": {"x": deep, "y": deep}})


def test_a_guard_on_bad_run_data_fails_the_node_with_a_message(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: "A ${data.m}", returns: {ok: {k: list}}}
          b: {prompt: B}
        data: {m: {x: 1}}
        body:
          seq:
            - {seq: [a], bind: got}
            - cond: [{if: "$data.m[$data.got.k] == 1", then: b}]
    """)
    d.report("a", "ok", {"k": [1]})
    assert d.st["status"] == "failed"
    assert "cond guard error" in json.dumps(d.st["value"])


# ---------------------------------------------------------------------------
# C1: max_visits


def test_a_node_past_max_visits_holds_for_a_person(tmp_path):
    d = Drv(tmp_path, """
        max_visits: 2
        nodes:
          a: {prompt: A}
        body:
          loop: {while: "true", max: 10, do: a}
    """)
    d.report(d.inst("a"))
    d.report(d.inst("a"))
    st = d.st
    assert st["status"] == "waiting_approval" and st["reason"] == "visit_limit"
    assert st["visit_limits"][0]["visits"] == 3 and st["visit_limits"][0]["max"] == 2
    with pytest.raises(runtime.FlowError, match="no loop limit"):
        runtime.extend_loop(**d.kw())
    d.st = runtime.extend_visits(by=1, **d.kw())
    assert d.st["status"] == "running" and d.tasks() == ["a"]
    d.report(d.inst("a"))
    assert d.st["reason"] == "visit_limit"
    d.st = cflow2_approve.extend(d.cwd, d.scope, {"kind": "visits", "stop": True})
    assert d.st["status"] == "failed"


# ---------------------------------------------------------------------------
# C2 / E3: the driver is the run's identity


def test_the_driver_is_recorded_and_used_for_the_roster(tmp_path, monkeypatch):
    # E3 repro: drv starts a run under the forged scope 'child'
    monkeypatch.setenv("CLAUNCH_SESSION", "drv")
    d = Drv(tmp_path, """
        nodes:
          a:
            prompt: a
            ask: {prompt: "ok?", from: [{role: reviewer, scope: any}], otherwise: self}
        body: a
    """, scope="child")
    assert d.state()["driver"] == "drv"
    seen = {}

    def pool(**kw):
        seen.update(kw)
        return responders.Pool(mesh="m", me="drv", members={}, reachable=set(),
                               ancestors=[], siblings=set())
    asks.route(cwd=d.cwd, scope=d.scope, now=T0, pool_fn=pool, deliver_fn=lambda *a: None)
    assert seen["session"] == "drv"  # never the scope
    rec = next(iter(d.state()["asks"].values()))
    rec_state = d.state()
    rec_state["asks"][rec["id"]].update(resolution="asked", asked=[
        {"session": "drv", "handle": "drv"}])
    RunStore(d.cwd, d.scope).save_state(rec_state)
    with pytest.raises(runtime.FlowError, match="own run"):
        runtime.answer(rec["id"], "approve", by_session="drv", **d.kw())


def test_mcp_tools_refuse_another_sessions_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Drv(tmp_path, "nodes:\n  a: {prompt: A}\nbody: a\n", scope="other", driver="other")
    monkeypatch.setenv("CLAUNCH_SESSION", "me")
    for tool, args in (("flow_status", {"scope": "other"}),
                       ("flow_report", {"scope": "other", "node": "a"}),
                       ("flow_rule", {"scope": "other", "name": "x"})):
        with pytest.raises(runtime.FlowError, match="driven by other"):
            cflow2_mcp.call_tool(tool, args)
    with pytest.raises(runtime.FlowError, match="own slot"):
        cflow2_mcp.call_tool("flow_start", {"workflow": "x", "scope": "other"})
    assert "flow_abort" not in {t["name"] for t in cflow2_mcp.TOOLS}
    monkeypatch.setenv("CLAUNCH_SESSION", "other")
    st = cflow2_mcp.call_tool("flow_status", {})
    assert st["tasks"][0]["label"] == "a"


def test_force_start_is_refused_while_a_person_is_waited_on(tmp_path):
    src = """
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          choose: {chooser: user, options: {x: {then: a}, y: {then: b}}}
    """
    Drv(tmp_path, src)
    with pytest.raises(runtime.FlowError, match="waits on a person.*user choice"):
        runtime.start(_compile(src), cwd=str(tmp_path), scope=SCOPE, force=True, now=T0,
                      caller=runtime.CALLER_AGENT)
    runtime.start(_compile(src), cwd=str(tmp_path), scope=SCOPE, force=True, now=T0)


def test_delegation_results_by_executor_and_receipt_paths(tmp_path, monkeypatch):
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, executor: [subagent]}
        body: t
    """, caps=SUBAGENT_CAPS, driver="drv")
    did = d.st["delegations"][0]["id"]
    with pytest.raises(runtime.FlowError, match="driven by drv"):
        runtime.delegate_submit(did, "ok", {}, via=runtime.VIA_MCP, session="else", **d.kw())
    # a receipt outside the receipts dir / scratch, also through a link
    st = RunStore(d.cwd, d.scope)
    st.receipts_dir.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"outcome": "ok", "value": {}}))
    link = st.receipts_dir / "link.json"
    os.symlink(secret, link)
    with pytest.raises(runtime.FlowError, match="refused"):
        runtime.delegate_submit(did, receipt_path=str(link), via=runtime.VIA_MCP,
                                session="drv", **d.kw())
    good = st.receipts_dir / f"{did}.child.123.json"
    good.write_text(json.dumps({"outcome": "ok", "value": {}}))
    d.st = runtime.delegate_submit(did, receipt_path=str(good), via=runtime.VIA_MCP,
                                   session="drv", **d.kw())
    assert d.st["status"] == "done"


def test_headless_and_session_results_have_their_own_doors(tmp_path, monkeypatch):
    monkeypatch.setattr(executors, "available", lambda cwd: True)
    monkeypatch.setattr(executors, "dispatch", lambda rec, run: None)
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, executor: [headless]}
          u: {prompt: U, executor: [session]}
        body: [t, u]
    """, caps={"headless.run": True, "session.spawn": True, "mcp.claunch": True},
        driver="drv")
    (dl,) = d.st["delegations"]
    assert dl["executor"] == "headless"
    with pytest.raises(runtime.FlowError, match="headless"):
        runtime.delegate_submit(dl["id"], "ok", {}, via=runtime.VIA_MCP, session="drv",
                                **d.kw())
    runtime.executor_submit(dl["id"], "ok", {}, cwd=d.cwd, scope=d.scope, now=T0)
    st = d.status()
    (sd,) = [x for x in st["delegations"] if x["executor"] == "session"]
    runtime.delegate_update(sd["id"], cwd=d.cwd, scope=d.scope, now=T0, child="kid",
                            assignee="kid")
    with pytest.raises(runtime.FlowError, match="carried out by session kid"):
        runtime.delegate_submit(sd["id"], "ok", {}, via=runtime.VIA_MCP, session="drv",
                                **d.kw())
    d.st = runtime.delegate_submit(sd["id"], "ok", {}, via=runtime.VIA_MCP, session="kid",
                                   **d.kw())
    assert d.st["status"] == "done"


# ---------------------------------------------------------------------------
# D


def test_parallel_foreach_break_keeps_finished_values(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          s: {prompt: "s ${item}", returns: {ok: {n: int}, stop: {}}}
        data: {xs: [1, 2, 3]}
        body:
          id: L
          foreach: {in: "$data.xs", par: true, do: {seq: [s], on: {stop: {break: L}}}}
    """)
    ids = {t["prompt"]: t["instance"] for t in d.st["tasks"]}
    d.report(ids["s 1"], "ok", {"n": 1})
    d.report(ids["s 3"], "ok", {"n": 3})
    d.report(ids["s 2"], "stop")
    assert d.st["status"] == "done" and d.st["value"] == [{"n": 1}, {"n": 3}]


def test_bind_keeps_the_nodes_own_value_after_an_ensures_failure(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          land: {prompt: L, returns: {ok: {merge: "str?"}}}
          after: {prompt: after}
        spec:
          ensures:
            wrap.ok: "$result.land.value.merge != null"
        body:
          try: {seq: [land], bind: got, id: wrap}
          catch: {fail: after}
    """)
    d.report("land", "ok", {"merge": None})
    st = d.state()
    assert st["data"]["got"] == {"merge": None}  # the node's own return shape
    assert st["result"]["wrap"]["outcome"] == "fail"
    assert d.tasks() == ["after"]


# ---------------------------------------------------------------------------
# E1: no value is spliced into a shell command


def _chart_with_verify(command):
    chart = _compile("""
        params: {target: {default: ok}}
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body: [a, b]
    """)
    # the frontend refuses ${...} in verify; the runtime is the backstop for
    # a chart from anywhere else
    chart.by_label("a").props["verify"] = {"run": command, "timeout": 30}
    return chart


def test_verify_values_reach_the_shell_only_as_env(tmp_path):
    d = Drv(tmp_path, "", chart=_chart_with_verify("false ${params.target}"),
            params={"target": "x || true"})
    st = d.report(d.inst("a"))  # the real default runner, a real shell
    assert st["tasks"][0]["label"] == "a" and st["tasks"][0]["last_verify"]["exit"] == 1
    ev = d.events("verify_failed")[0]
    assert ev["command"] == 'false "${CFLOW_IN_TARGET}"'
    assert not d.events("verify_passed")


def test_verify_value_in_double_quotes_stays_one_word(tmp_path):
    d = Drv(tmp_path, "", chart=_chart_with_verify('test "${params.target}" = "a b; true"'),
            params={"target": "a b; true"})
    d.report(d.inst("a"))
    assert d.events("verify_passed")


def test_unsupported_references_are_refused_at_start(tmp_path):
    with pytest.raises(runtime.FlowError, match="may only name"):
        Drv(tmp_path, "", chart=_chart_with_verify("x ${prev.value}"))
    with pytest.raises(runtime.FlowError, match="single quotes"):
        Drv(tmp_path, "", chart=_chart_with_verify("x '${params.target}'"))
    # a shell variable is the shell's
    assert runtime.shell_ref_problems("echo ${HOME} ${CFLOW_IN_X:-d}", "v") == []


def test_wait_probes_get_values_through_env_and_undecided_ends(tmp_path):
    d = Drv(tmp_path, """
        params: {n: {default: 7}}
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          try: {seq: [{wait: {probe: "check", poll: 1}}, a]}
          catch: {undecidable: b}
    """)
    seen = []

    def runner(cmd, cwd, env=None):
        seen.append(env)
        return (2, "cannot tell")
    for k in range(3):
        d.st = runtime.wait_tick(T0 + 10 * (k + 1), runner, cwd=d.cwd, scope=d.scope)
    assert seen[0]["CFLOW_IN_N"] == "7"
    assert d.tasks() == ["b"]
    assert len(d.events("probe_undecided")) == 3


def test_a_wait_probe_reads_a_map_of_data_as_json(tmp_path):
    # improv-worker-v2's landed-wait hands data.landing (a map) to
    # tools/landed_check.py --landing "$CFLOW_DATA_LANDING" (claunch-yq80p.4)
    d = Drv(tmp_path, """
        data:
          landing: {medium: pr, remote: ghe, base: master}
          note: plain
        nodes:
          a: {prompt: A}
        body:
          seq: [{wait: {probe: "check", poll: 1}}, a]
    """)
    seen = []

    def runner(cmd, cwd, env=None):
        seen.append(env)
        return (0, "landed")
    d.st = runtime.wait_tick(T0 + 10, runner, cwd=d.cwd, scope=d.scope)
    assert json.loads(seen[0]["CFLOW_DATA_LANDING"]) == {"medium": "pr", "remote": "ghe",
                                                         "base": "master"}
    assert seen[0]["CFLOW_DATA_NOTE"] == "plain"
    assert d.tasks() == ["a"]


def test_a_verify_reads_a_map_of_data_as_json_like_a_probe(tmp_path):
    # improv-leader-v2's sweep verify reads the integrate step's bound result
    # (data.merged, a map) through CFLOW_DATA_MERGED (claunch-6xg). A verify
    # used to get the scalars only, so the map never reached it.
    chart = _compile("""
        data:
          merged: {repo: /elsewhere, master: abc}
          note: plain
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body: [a, b]
    """)
    check = ("import json, os, sys; m = json.loads(os.environ['CFLOW_DATA_MERGED']); "
             "sys.exit(0 if m == {'repo': '/elsewhere', 'master': 'abc'} "
             "and os.environ['CFLOW_DATA_NOTE'] == 'plain' else 1)")
    chart.by_label("a").props["verify"] = {"run": [sys.executable, "-c", check],
                                           "timeout": 30}
    d = Drv(tmp_path, "", chart=chart)
    d.report(d.inst("a"))
    assert d.events("verify_passed") and not d.events("verify_failed")
    assert d.tasks() == ["b"]


def test_a_verify_gets_no_map_past_the_probe_size_cap(tmp_path):
    big = "x" * (64 * 1024 + 1)
    chart = _compile("""
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body: [a, b]
    """)
    d = Drv(tmp_path, "", chart=chart)
    run = runtime.Run.load(runtime.RunStore(d.cwd, d.scope))
    run.state.setdefault("data", {}).update({"huge": {"v": big}, "long": big})
    env = runtime.verify_env(run, d.inst("a"))
    assert "CFLOW_DATA_HUGE" not in env       # a map past 64KB, as for a probe
    assert env["CFLOW_DATA_LONG"] == big      # a scalar whatever its size


# ---------------------------------------------------------------------------
# E2: ask options without case


def test_ask_options_with_capitals_are_answerable(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          r:
            prompt: review
            returns: {LGTM: {}, Changes: {}}
            ask:
              prompt: verdict?
              options: {LGTM: {description: fine}, Changes: {description: no}}
          c: {prompt: c}
        body:
          try: r
          catch: {Changes: c}
    """)
    runtime.approve(None, decision="lgtm", **d.kw())
    assert d.state()["result"]["r"]["outcome"] == "LGTM"
    assert d.events("ask_answered")[0]["decision"] == "LGTM"


# ---------------------------------------------------------------------------
# E4: CLI and deny rules


def test_deny_rules_cover_other_spellings():
    rules = set(install.GATE_DENY_RULES)
    for cmd in ("approve", "select", "extend", "resume", "abort"):
        assert f"Bash(claunch cflow2 {cmd}:*)" in rules
        assert f"Bash(*claunch* cflow2 {cmd}*)" in rules
        assert f"Bash(*claude_launcher* cflow2 {cmd}*)" in rules
    assert "Bash(*claunch* cflow2 start*--force*)" in rules
    assert not any("status" in r or "journal" in r for r in rules)


def _cli(cwd, *args, session=None):
    env = dict(os.environ)
    env.pop("CLAUNCH_SESSION", None)
    if session:
        env["CLAUNCH_SESSION"] = session
    return subprocess.run([sys.executable, "-c", "import sys; from claude_launcher.cli import "
                           "main; sys.exit(main())", "cflow2", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=60)


def test_cli_refuses_the_driving_sessions_shell(tmp_path):
    Drv(tmp_path, """
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          choose: {chooser: user, options: {x: {then: a}, y: {then: b}}}
    """, driver=SCOPE)
    out = _cli(str(tmp_path), "select", "--scope", SCOPE, "body", "x", "--reason", "r", session=SCOPE)
    assert out.returncode == 1 and "drives this run" in out.stderr
    out = _cli(str(tmp_path), "abort", "--scope", SCOPE, "--reason", "r", session=SCOPE)
    assert out.returncode == 1 and "drives this run" in out.stderr
    out = _cli(str(tmp_path), "select", "--scope", SCOPE, "body", "x", "--reason", "r")
    assert out.returncode == 0, out.stderr


# ---------------------------------------------------------------------------
# F1, F2, F4


ROUTED = """
    nodes:
      a:
        prompt: a
        ask:
          prompt: "ship it?"
          from: [{role: reviewer, scope: any}, {role: leader, scope: any}]
          otherwise: human
          timeout: 60
      b: {prompt: b}
    body: [a, b]
"""


def _member(handle, role, session=None):
    return responders.Responder(session=session or f"s-{handle}", handle=handle, role=role,
                                local=True, reachability="idle")


def _route(d, members, now=T0):
    pool = responders.Pool(mesh="m", me=SCOPE, members=members, reachable=set(members),
                           ancestors=[], siblings=set())
    asks.route(cwd=d.cwd, scope=d.scope, now=now, pool_fn=lambda **_: pool,
               deliver_fn=lambda *a: None)


def test_an_answer_after_the_deadline_is_refused(tmp_path):
    d = Drv(tmp_path, ROUTED, driver=SCOPE)
    _route(d, {"rev": _member("rev", "reviewer")})
    rec = next(iter(d.state()["asks"].values()))
    with pytest.raises(runtime.FlowError, match="not open"):
        runtime.answer(rec["id"], "approve", by_session="s-rev", **d.kw(now=T0 + 10000))
    assert d.state()["asks"][rec["id"]]["resolution"] == "asked"  # nothing was written
    assert runtime.status(**d.kw(now=T0 + 10000))["asks"][0]["asked"] != ["rev"]


def test_fork_refuses_undeclared_keys_and_null_required(tmp_path):
    d = Drv(tmp_path, """
        procedures:
          bare:
            nodes:
              t: {prompt: T}
            body: t
          req:
            params: {need: {required: true}}
            nodes:
              u: {prompt: U}
            body: u
        nodes:
          lead: {prompt: lead}
          lead2: {prompt: lead2}
        body:
          par:
            branches:
              x: {group: {name: g, run: bare, do: lead}}
              y: {group: {name: h, run: req, do: lead2}}
    """)
    with pytest.raises(runtime.FlowError, match="unknown param"):
        runtime.fork("g", {"x": 1}, **d.kw())
    with pytest.raises(runtime.FlowError, match="missing required"):
        runtime.fork("h", {"need": None}, **d.kw())
    runtime.fork("g", {}, **d.kw())


def test_withdraw_on_timeout_hop_and_cancellation(tmp_path, _env):
    d = Drv(tmp_path, ROUTED, driver=SCOPE)
    _route(d, {"rev": _member("rev", "reviewer"), "lead": _member("lead", "leader")})
    runtime.status(**d.kw(now=T0 + 61))
    (item, sender), = _env
    assert [e["handle"] for e in item["pending"]] == ["rev"] and sender == SCOPE
    _route(d, {"rev": _member("rev", "reviewer"), "lead": _member("lead", "leader")},
           now=T0 + 61)
    runtime.abort("stop", **d.kw(now=T0 + 62))
    assert [e["handle"] for e in _env[-1][0]["pending"]] == ["lead"]
    assert _env[-1][0]["decision"] == "cancelled"


def test_one_abstention_leaves_the_question_with_the_group(tmp_path, _env):
    d = Drv(tmp_path, ROUTED, driver=SCOPE)
    _route(d, {"r1": _member("r1", "reviewer"), "r2": _member("r2", "reviewer")})
    rec = next(iter(d.state()["asks"].values()))
    out = runtime.answer(rec["id"], "abstain", by_session="s-r1", **d.kw())
    assert out["moved"] is False
    with pytest.raises(runtime.FlowError, match="already abstained"):
        runtime.answer(rec["id"], "approve", by_session="s-r1", **d.kw())
    runtime.answer(rec["id"], "approve", by_session="s-r2", **d.kw())
    assert d.status()["tasks"][0]["label"] == "a"


def test_everyone_abstaining_moves_the_question_on(tmp_path, _env):
    d = Drv(tmp_path, ROUTED, driver=SCOPE)
    _route(d, {"r1": _member("r1", "reviewer"), "r2": _member("r2", "reviewer")})
    rec = next(iter(d.state()["asks"].values()))
    runtime.answer(rec["id"], "abstain", by_session="s-r1", **d.kw())
    out = runtime.answer(rec["id"], "abstain", by_session="s-r2", **d.kw())
    assert out["moved"] is True
    assert d.state()["asks"][rec["id"]]["group"] == 1


# ---------------------------------------------------------------------------
# typed start inputs: values, acceptance


INPUTS = """
    inputs:
      goal: {type: string, required: true}
      done: {type: acceptance}
    nodes:
      a: {prompt: "do ${data.inputs.goal}"}
    body: a
"""


def test_start_inputs_are_validated_applied_and_shown(tmp_path):
    with pytest.raises(runtime.FlowError, match="invalid inputs: goal"):
        Drv(tmp_path, INPUTS, inputs={})
    d = Drv(tmp_path, INPUTS, inputs={"goal": "ship"})
    assert d.st["inputs"]["goal"] == "ship"
    assert d.st["tasks"][0]["prompt"].startswith("do ship")
    assert d.events("started")[0]["inputs"]["goal"] == "ship"


def test_acceptance_probes_hold_an_ok_finish(tmp_path, monkeypatch):
    results = {"code": 1}
    monkeypatch.setattr(runtime, "ACCEPTANCE_RUNNER",
                        lambda cmd, cwd, env=None: (results["code"], "out"))
    d = Drv(tmp_path, INPUTS, inputs={"goal": "g", "done": [
        {"text": "tests pass", "probe": "pytest -q"}, {"text": "docs read well"}]})
    d.report(d.inst("a"))
    runtime.verify_join(30)
    st = d.status()
    assert st["status"] == "waiting_approval" and st["reason"] == "acceptance"
    assert [x["text"] for x in st["acceptance"]["failed"]] == ["tests pass"]
    with pytest.raises(runtime.FlowError, match="acceptance check failed"):
        runtime.approve("acceptance", caller=runtime.CALLER_AGENT, **d.kw())
    # continuing work: the next status (past the recheck interval) checks again
    results["code"] = 0
    d.st = runtime.wait_tick(T0 + 100, lambda *a, **k: (0, ""), cwd=d.cwd, scope=d.scope)
    assert d.st["status"] == "done"


def test_a_person_accepts_a_failed_acceptance_anyway(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ACCEPTANCE_RUNNER", lambda cmd, cwd, env=None: (1, ""))
    d = Drv(tmp_path, INPUTS, inputs={"goal": "g", "done": [{"text": "t", "probe": "p"}]})
    d.report(d.inst("a"))
    runtime.verify_join(30)
    st = asks.human_approve("acceptance", reason="fine", cwd=d.cwd, scope=d.scope, now=T0,
                            nudge_fn=lambda *a: None)
    assert st["status"] == "done" and d.events("acceptance_waived")


def test_cli_start_takes_inputs(tmp_path):
    (tmp_path / ".claunch" / "workflows" / "v2").mkdir(parents=True)
    (tmp_path / ".claunch" / "workflows" / "v2" / "inp.yaml").write_text(
        "cflow: 2\nname: inp\n" + textwrap.dedent(INPUTS), encoding="utf-8")
    (tmp_path / "in.json").write_text(json.dumps({"goal": "from file"}))
    out = _cli(str(tmp_path), "start", "inp", "--inputs-file", "in.json", "--input",
               "goal=from flag", "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["inputs"]["goal"] == "from flag"


# ---------------------------------------------------------------------------
# loading by path: only generated procedures


GEN = "cflow: 2\nname: gen\nnodes:\n  a: {prompt: A}\nbody: a\n"


def test_a_path_loads_only_from_the_generated_directory(tmp_path):
    gen = tmp_path / ".cflow2" / "generated"
    gen.mkdir(parents=True)
    (gen / "p.yaml").write_text(GEN)
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(GEN)
    (gen / "link.yaml").symlink_to(outside)
    cwd = str(tmp_path)
    assert runtime.load_definition(".cflow2/generated/p.yaml", cwd).name == "gen"
    assert runtime.load_definition(str(gen / "p.yaml"), cwd).name == "gen"
    for bad in (str(outside), "elsewhere.yaml", ".cflow2/generated/../../elsewhere.yaml",
                ".cflow2/generated/link.yaml"):
        with pytest.raises(runtime.FlowError, match="outside"):
            runtime.load_definition(bad, cwd)
    with pytest.raises(runtime.FlowError, match="not found"):
        runtime.load_definition(".cflow2/generated/missing.yaml", cwd)


def test_flow_start_and_cli_validate_take_a_generated_path(tmp_path, monkeypatch):
    gen = tmp_path / ".cflow2" / "generated"
    gen.mkdir(parents=True)
    (gen / "p.yaml").write_text(GEN)
    (tmp_path / "x.yaml").write_text(GEN)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", SCOPE)
    with pytest.raises(runtime.FlowError, match="outside"):
        cflow2_mcp._call("flow_start", {"workflow": "x.yaml"})
    out = cflow2_mcp._call("flow_start", {"workflow": ".cflow2/generated/p.yaml"})
    assert out["workflow"] == "gen" and out["driver"] == SCOPE
    ok = _cli(str(tmp_path), "validate", ".cflow2/generated/p.yaml")
    assert ok.returncode == 0, ok.stderr
    bad = _cli(str(tmp_path), "validate", "x.yaml")
    assert bad.returncode != 0 and "outside" in (bad.stderr + bad.stdout)


def test_argv_commands_carry_no_value_references(tmp_path):
    chart = _compile("""
        nodes:
          a: {prompt: A, verify: [check, ok]}
        body: a
    """)
    task = next(n for n in chart.nodes.values() if n.type == ir.T_TASK)
    task.props["verify"] = {"run": ["check", "${params.who}"], "timeout": 5}
    with pytest.raises(runtime.FlowError, match="argv item"):
        runtime.start(chart, cwd=str(tmp_path), scope=SCOPE, caps={}, now=T0)


# -- G1 / G2 (review of ba2921be) --------------------------------------------

NESTED_LOOP = """
    nodes:
      a: {prompt: A}
      after: {prompt: after}
    body:
      seq:
        - id: unit
          executor: [subagent, inline]
          loop: {while: "true", max: 1, do: a}
        - after
"""


def test_no_door_submits_for_a_nested_delegation(tmp_path):
    """G1: the daemon's own door (a child receipt picked up by the clock)
    cannot settle a nested delegation either; only the nested run can."""
    d = Drv(tmp_path, NESTED, caps=SUBAGENT_CAPS, driver="drv")
    (dl,) = d.st["delegations"]
    receipt = RunStore(d.cwd, SCOPE).receipt_path(dl["id"], ".child.json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"outcome": "ok", "value": {}}))
    with pytest.raises(runtime.FlowError, match="nested run"):
        runtime.executor_submit(dl["id"], receipt_path=str(receipt), **d.kw())
    assert d.state()["delegations"][dl["id"]]["status"] not in ("returned", "failed")


def test_force_start_is_refused_while_a_nested_run_waits_on_a_person(tmp_path):
    """G2: the hold sits in the nested run; replacing the parent would
    abort it, so an agent's forced start is refused and status shows it."""
    d = Drv(tmp_path, NESTED_LOOP, caps=SUBAGENT_CAPS, driver="drv")
    (dl,) = d.st["delegations"]
    nested = dl["nested"]
    kw = {"cwd": nested["cwd"], "scope": nested["scope"], "now": T0}
    runtime.report("a", **kw)
    assert RunStore(nested["cwd"], nested["scope"]).load_state()["reason"] == "loop_limit"
    d.status()
    assert any("nested run" in w for w in d.st.get("waiting_in_nested") or [])
    with pytest.raises(runtime.FlowError, match="waits on a person.*nested run"):
        runtime.start(_compile(NESTED_LOOP), cwd=d.cwd, scope=SCOPE, force=True, now=T0,
                      caps=SUBAGENT_CAPS, caller=runtime.CALLER_AGENT)
    assert RunStore(nested["cwd"], nested["scope"]).load_state()["status"] in \
        runtime.LIVE_STATUSES


@pytest.mark.parametrize("argv", [
    ["cflow2", "start", "sample-v2", "--forc"],
    ["cflow", "add", "x", "--forc"],
])
def test_option_abbreviations_are_refused(argv, capsys):
    """G3: `--forc` must not pass for `--force` past the deny rules."""
    from claude_launcher import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "--forc" in capsys.readouterr().err


def test_an_agents_forced_start_keeps_the_persons_acceptance(tmp_path):
    """H1: an agent may add checks on itself; a forced restart cannot drop
    the acceptance items the person started the run with."""
    person = {"text": "tests pass", "probe": "true"}
    d = Drv(tmp_path, INPUTS, inputs={"goal": "ship", "done": [person]})
    st = runtime.start(_compile(INPUTS), cwd=d.cwd, scope=SCOPE, force=True, now=T0,
                       caps={}, caller=runtime.CALLER_AGENT,
                       inputs={"goal": "ship", "done": [{"text": "mine", "probe": None}]})
    texts = [i["text"] for i in st["inputs"]["done"]]
    assert texts == ["tests pass", "mine"]
