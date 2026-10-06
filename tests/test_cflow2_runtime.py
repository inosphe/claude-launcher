"""cflow v2 runtime: advance semantics of every combinator (docs/cflow-v2.md 4)."""

from __future__ import annotations

import json
import textwrap

import pytest

from claude_launcher.cflow2 import executors, frontend, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0


class Drv:
    """A driver over one run in a tmp cwd."""

    def __init__(self, tmp_path, src, caps=None, params=None, scope="t"):
        self.cwd = str(tmp_path)
        self.scope = scope
        if src.startswith(TASKS):
            src = TASKS + textwrap.dedent(src[len(TASKS):])
        chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
        self.st = runtime.start(chart, cwd=self.cwd, scope=scope, params=params,
                                caps=caps if caps is not None else {}, now=T0)

    def kw(self, now=None):
        return {"cwd": self.cwd, "scope": self.scope, "now": now if now is not None else T0}

    def status(self, now=None):
        self.st = runtime.status(**self.kw(now))
        return self.st

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def inst(self, label):
        hits = [t["instance"] for t in self.st["tasks"] if t["label"] == label]
        assert len(hits) == 1, (label, self.st.get("tasks"))
        return hits[0]

    def report(self, ref, outcome="ok", value=None, now=None):
        self.st = runtime.report(ref, outcome, value, **self.kw(now))
        return self.st

    def do(self, *labels, outcome="ok"):
        for label in labels:
            self.report(self.inst(label), outcome)
        return self.st

    def select(self, option, node=None, reason=None, caller="agent"):
        self.st = runtime.select(node, option, reason, caller=caller, **self.kw())
        return self.st

    def tick(self, now, probe=None):
        self.st = runtime.wait_tick(now, probe, cwd=self.cwd, scope=self.scope)
        return self.st

    def journal(self):
        return runtime.journal(cwd=self.cwd, scope=self.scope)

    def events(self, name):
        return [e for e in self.journal() if e["event"] == name]

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()


TASKS = """
nodes:
  a: {prompt: A}
  b: {prompt: B}
  c: {prompt: C}
  d: {prompt: D}
  f: {prompt: F, returns: {ok: {}, fail: {}, rework: {}}}
"""


def test_seq_runs_in_order_and_finishes(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a, b, c]}\n")
    assert d.tasks() == ["a"]
    d.do("a")
    assert d.tasks() == ["b"]
    d.do("b", "c")
    assert d.st["status"] == "done" and d.st["outcome"] == "ok"
    st = d.state()
    assert st["visits"]["a"] == 1 and st["inst"] == {}
    assert [e["event"] for e in d.journal()][0] == "started"
    assert d.journal()[-1]["event"] == "done"


def test_waterfall_passes_prev_and_renders_params(tmp_path):
    d = Drv(tmp_path, """
        params:
          who: {default: bob}
        nodes:
          a: {prompt: "A for ${params.who}", returns: {ok: {n: int}}}
          b: {prompt: "B got ${prev.n} ${data.missing}"}
        body: {waterfall: [a, b]}
    """)
    assert d.st["tasks"][0]["prompt"] == "A for bob"
    d.report("a", "ok", {"n": 7})
    assert d.st["tasks"][0]["prompt"] == "B got 7 ${data.missing}"


def test_non_ok_outcome_stops_seq_and_fails_run(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [f, b]}\n")
    d.report("f", "rework")
    assert d.st["status"] == "failed" and d.st["outcome"] == "rework"


def test_report_validation(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A, returns: {ok: {n: int, note: "str?"}, blocked: {why: str}}}
        body: a
    """)
    with pytest.raises(runtime.FlowError, match="unknown outcome"):
        d.report("a", "weird")
    with pytest.raises(runtime.FlowError, match="must be int"):
        d.report("a", "ok", {"n": "x"})
    with pytest.raises(runtime.FlowError, match="missing field 'why'"):
        d.report("a", "blocked", {})
    d.report("a", "ok", {"n": 1})  # optional note missing is fine
    assert d.st["status"] == "done"


def test_fail_is_always_accepted(tmp_path):
    d = Drv(tmp_path, TASKS + "body: a\n")
    d.report("a", "fail", {"reason": "x"})
    assert d.st["status"] == "failed"


def test_cond_first_true_guard_and_else(tmp_path):
    src = TASKS + """
        data: {n: 2}
        body:
          cond:
            - {if: "$data.n > 5", then: a}
            - {if: "$data.n > 1", then: b}
            - {else: c}
    """
    d = Drv(tmp_path, src)
    assert d.tasks() == ["b"]


def test_cond_without_match_fails(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          cond:
            - {if: "false", then: a}
    """)
    assert d.st["status"] == "failed"
    assert "no cond branch" in d.st["value"]["reason"]


def test_choose_guards_reason_and_chooser(tmp_path):
    d = Drv(tmp_path, TASKS + """
        data: {ready: false}
        body:
          choose:
            prompt: pick
            require_reason: true
            options:
              go: {description: go on, when: "$data.ready", then: a}
              wait: {then: b}
    """)
    ch = d.st["choices"][0]
    assert [o["name"] for o in ch["options"]] == ["wait"]
    assert ch["unavailable"][0]["name"] == "go"
    with pytest.raises(runtime.FlowError, match="not available"):
        d.select("go", reason="r")
    with pytest.raises(runtime.FlowError, match="requires a reason"):
        d.select("wait")
    d.select("wait", reason="because")
    assert d.tasks() == ["b"]
    assert d.events("selected")[0]["option"] == "wait"


def test_user_chooser_refuses_agent(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          choose:
            chooser: user
            options:
              x: {then: a}
    """)
    with pytest.raises(runtime.FlowError, match="user"):
        d.select("x")
    d.select("x", caller="user")
    assert d.tasks() == ["a"]


def test_bind_and_while_loop_with_limit_and_extend(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          step: {prompt: "iter", returns: {ok: {more: bool}}}
        body:
          loop:
            while: "$data.r == null or $data.r.more"
            max: 2
            do: {seq: [step], bind: r}
    """)
    d.report("step", "ok", {"more": True})
    d.report("step", "ok", {"more": True})
    assert d.st["status"] == "waiting_approval" and d.st["reason"] == "loop_limit"
    assert d.st["loop_limits"][0]["iterations"] == 2
    d.st = runtime.extend_loop(None, 1, **d.kw())
    assert d.st["status"] == "running" and d.tasks() == ["step"]
    d.report("step", "ok", {"more": False})
    assert d.st["status"] == "done"
    assert d.state()["visits"]["step"] == 3
    assert d.state()["data"]["r"] == {"more": False}


def test_loop_limit_stop(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          loop: {while: "true", max: 1, do: a}
    """)
    d.do("a")
    d.st = runtime.extend_loop(None, stop=True, **d.kw())
    assert d.st["status"] == "failed"


def test_until_runs_body_first(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          s: {prompt: S, returns: {ok: {n: int}}}
        body:
          loop:
            until: "$data.v.n >= 2"
            max: 5
            do: {seq: [s], bind: v}
    """)
    d.report("s", "ok", {"n": 1})
    d.report("s", "ok", {"n": 2})
    assert d.st["status"] == "done"


def test_foreach_sequential_items(tmp_path):
    d = Drv(tmp_path, """
        data: {xs: [one, two]}
        nodes:
          s: {prompt: "do ${item} / ${x}"}
        body:
          foreach: {in: "$data.xs", as: x, do: s}
    """)
    assert d.st["tasks"][0]["prompt"] == "do one / one"
    d.do("s")
    assert d.st["tasks"][0]["prompt"] == "do two / two"
    d.do("s")
    assert d.st["status"] == "done" and d.st["value"] == [None, None]


def test_foreach_parallel_instances(tmp_path):
    d = Drv(tmp_path, """
        data: {xs: [1, 2, 3]}
        nodes:
          s: {prompt: "n=${item}", returns: {ok: {sq: int}}}
        body:
          foreach: {in: "$data.xs", par: true, do: s}
    """)
    ts = d.st["tasks"]
    assert len(ts) == 3 and len({t["instance"] for t in ts}) == 3
    assert [t["prompt"] for t in ts] == ["n=1", "n=2", "n=3"]
    with pytest.raises(runtime.FlowError, match="several"):
        d.report("s", "ok", {"sq": 1})
    for t in reversed(ts):
        d.report(t["instance"], "ok", {"sq": t["item"] ** 2})
    assert d.st["status"] == "done"
    assert d.st["value"] == [{"sq": 1}, {"sq": 4}, {"sq": 9}]


def test_foreach_parallel_over_max_waits_for_approval(tmp_path):
    d = Drv(tmp_path, TASKS + """
        data: {xs: [1, 2, 3]}
        body:
          foreach: {in: "$data.xs", par: true, max: 2, do: a}
    """)
    assert d.st["status"] == "waiting_approval"
    d.st = runtime.extend_loop(None, 1, **d.kw())
    assert len(d.st["tasks"]) == 3


def test_retry_between_and_exhaustion(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          retry: {do: f, max: 2, between: b}
    """)
    d.report("f", "fail")
    assert d.tasks() == ["b"]
    d.do("b")
    assert d.tasks() == ["f"]
    d.report("f", "rework")
    assert d.st["status"] == "failed" and d.st["outcome"] == "rework"


def test_retry_until_and_every_pause(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          probe: {prompt: P, returns: {ok: {green: bool}}}
        body:
          retry:
            do: {seq: [probe], bind: p}
            max: 3
            every: 60
            until: "$data.p.green"
    """)
    d.report("probe", "ok", {"green": False})
    assert d.tasks() == [] and d.st["waits"][0]["kind"] == "pause"
    d.tick(T0 + 30)
    assert d.tasks() == []
    d.tick(T0 + 61)
    assert d.tasks() == ["probe"]
    d.report("probe", "ok", {"green": True}, now=T0 + 70)
    assert d.st["status"] == "done"


PAR = TASKS + """
body:
  par:
    settle: {settle}
    branches:
      x: f
      y: b
      z: c
"""


def test_par_all_fails_fast_and_cancels(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "all"))
    assert sorted(d.tasks()) == ["b", "c", "f"]
    d.report("f", "rework")
    assert d.st["status"] == "failed" and d.st["outcome"] == "rework"
    cancelled = [e for e in d.events("left") if e.get("outcome") == "cancelled"]
    assert {e["node"] for e in cancelled} == {"b", "c"}


def test_par_all_success_value(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "all"))
    d.do("b", "c", "f")
    assert d.st["status"] == "done" and set(d.st["value"]) == {"x", "y", "z"}


def test_par_all_settled_waits_for_every_branch(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "allSettled"))
    d.report("f", "fail")
    assert sorted(d.tasks()) == ["b", "c"]
    d.do("b", "c")
    assert d.st["status"] == "done"
    assert d.st["value"]["x"]["outcome"] == "fail"


def test_par_race_first_finisher_decides(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "race"))
    d.do("c")
    assert d.st["status"] == "done"


def test_par_any(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "any"))
    d.report("f", "fail")
    assert d.st["status"] == "running"
    d.do("b")
    assert d.st["status"] == "done"
    assert [e for e in d.events("left") if e.get("outcome") == "cancelled"][0]["node"] == "c"


def test_par_quorum(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "quorum:2"))
    d.report("f", "fail")
    d.do("b")
    assert d.st["status"] == "running"
    d.do("c")
    assert d.st["status"] == "done"


def test_par_quorum_unreachable(tmp_path):
    d = Drv(tmp_path, PAR.replace("{settle}", "quorum:3"))
    d.report("f", "fail")
    assert d.st["status"] == "failed"


def test_timeout_goes_to_else(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          timeout: {do: a, seconds: 100, else: b}
    """)
    d.tick(T0 + 50)
    assert d.tasks() == ["a"]
    d.tick(T0 + 101)
    assert d.tasks() == ["b"]
    d.do("b")
    assert d.st["status"] == "done"


def test_timeout_without_else_is_caught_by_try(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try: {timeout: {do: a, seconds: 10}}
          catch: {timeout: b}
    """)
    d.tick(T0 + 11)
    assert d.tasks() == ["b"]


def test_try_catch_finally_order(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try: {seq: [f, a]}
          catch: {rework: b}
          finally: c
    """)
    d.report("f", "rework")
    assert d.tasks() == ["b"]
    d.do("b")
    assert d.tasks() == ["c"]
    d.do("c")
    assert d.st["status"] == "done"
    order = [e["node"] for e in d.events("entered") if e["type"] == "task"]
    assert order == ["f", "b", "c"]


def test_finally_runs_on_success_and_keeps_body_result(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try: f
          finally: c
    """)
    d.report("f", "rework")
    assert d.tasks() == ["c"]
    d.do("c")
    assert d.st["outcome"] == "rework"


def test_raise_routes_to_catch(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          try:
            seq:
              - a
              - {raise: oops}
              - b
          catch: {oops: c}
    """)
    d.do("a")
    assert d.tasks() == ["c"]


def test_on_mapping_continue_and_break(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          id: L
          loop:
            while: "true"
            max: 5
            do:
              seq:
                - {seq: [f], on: {rework: {continue: L}, fail: {break: L}}}
                - a
    """)
    d.report("f", "rework")
    assert d.tasks() == ["f"]  # continue skipped a
    d.report("f", "fail")
    assert d.st["status"] == "done"


def test_break_runs_finally_on_the_way(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          id: L
          loop:
            while: "true"
            max: 5
            do:
              try:
                seq: [a, {break: L}]
              finally: c
    """)
    d.do("a")
    assert d.tasks() == ["c"]
    d.do("c")
    assert d.st["status"] == "done"


def test_return_ends_scope_with_outcome_and_bind(tmp_path):
    d = Drv(tmp_path, TASKS + """
        procedures:
          p:
            params: {x: {}}
            returns: {found: {v: int}}
            body:
              seq:
                - a
                - {return: found, value: {v: "$params.x"}}
                - b
        body:
          try:
            seq:
              - {call: p, with: {x: 5}, bind: got}
          catch: {found: c}
    """)
    d.do("a")
    assert d.tasks() == ["c"]
    assert d.state()["data"]["got"] == {"v": 5}


def test_end_finishes_run(tmp_path):
    d = Drv(tmp_path, TASKS + "body: {seq: [a, end, b]}\n")
    d.do("a")
    assert d.st["status"] == "done"


def test_scope_params_render(tmp_path):
    d = Drv(tmp_path, """
        procedures:
          p:
            params: {cmd: {}}
            nodes:
              run: {prompt: "run ${params.cmd}"}
            body: run
        body: {call: p, with: {cmd: "pytest -q"}}
    """)
    assert d.st["tasks"][0]["prompt"] == "run pytest -q"


def test_ensures_violation_turns_outcome_into_fail(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          land: {prompt: L, returns: {ok: {merge: "str?"}}}
        spec:
          ensures:
            land.ok: "$result.land.value.merge != null"
        body: land
    """)
    d.report("land", "ok", {})
    assert d.st["status"] == "failed"
    assert d.events("monitor_violation")[0]["key"] == "land.ok"


def test_wait_seconds_and_probe(tmp_path):
    d = Drv(tmp_path, TASKS + """
        body:
          seq:
            - wait: {seconds: 30}
            - wait: {probe: "check-it", poll: 10}
            - a
    """)
    assert d.st["waits"][0]["seconds"] == 30
    d.tick(T0 + 31)
    calls = []

    def runner(probe, cwd):
        calls.append(probe)
        return (1, "") if len(calls) < 2 else (0, "yes")

    d.tick(T0 + 32, runner)
    assert d.tasks() == [] and calls == ["check-it"]
    d.tick(T0 + 35, runner)  # poll interval not elapsed
    assert len(calls) == 1
    d.tick(T0 + 43, runner)
    assert d.tasks() == ["a"]


def test_group_fork_limit_and_settle(tmp_path):
    d = Drv(tmp_path, TASKS + """
        procedures:
          sub:
            params: {n: {}}
            nodes:
              m: {prompt: "member ${params.n}"}
            body: m
        body:
          group: {name: g, run: sub, limit: 2, do: a}
    """)
    st = runtime.fork("g", {"n": 1}, **d.kw())
    runtime.fork("g", {"n": 2}, **d.kw())
    with pytest.raises(runtime.FlowError, match="limit"):
        runtime.fork("g", {"n": 3}, **d.kw())
    d.status()
    assert sorted(t["prompt"] for t in d.st["tasks"] if t["label"] == "m") == \
        ["member 1", "member 2"]
    d.do("a")
    with pytest.raises(runtime.FlowError, match="joining"):
        runtime.fork("g", {"n": 3}, **d.kw())
    for t in [t for t in d.st["tasks"] if t["label"] == "m"]:
        d.report(t["instance"])
    assert d.st["status"] == "done"
    assert len(d.st["value"]["members"]) == 2
    assert d.events("group_fork") and d.events("group_settled")
    assert st["forked"]["member"]


SUBAGENT_CAPS = {"subagent.spawn": True, "mcp.claunch": True}


def test_subagent_delegation_brief_and_submit(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          scan: {prompt: "scan ${params.area}", returns: {ok: {hits: int}}, effects: [fs.read],
                 executor: [subagent, inline]}
          after: {prompt: after}
        params:
          area: {default: src}
        body: [scan, after]
    """, caps=SUBAGENT_CAPS)
    assert d.tasks() == []
    dl = d.st["delegations"][0]
    assert dl["executor"] == "subagent" and dl["status"] == "pending"
    assert dl["brief"]["tasks"][0]["prompt"] == "scan src"
    assert dl["brief"]["returns"] == {"ok": {"hits": "int"}}
    with pytest.raises(runtime.FlowError, match="refused"):
        runtime.delegate_submit(dl["id"], "ok", {"hits": "many"}, **d.kw())
    assert d.state()["delegations"][dl["id"]]["attempts"] == 1
    d.st = runtime.delegate_submit(dl["id"], "ok", {"hits": 3}, **d.kw())
    assert d.tasks() == ["after"]
    rec = d.state()["delegations"][dl["id"]]
    assert rec["status"] == "returned"
    assert json.loads(open(rec["receipt"]).read())["value"] == {"hits": 3}
    assert d.events("delegate_returned")


def test_subagent_second_invalid_result_fails_node(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          scan: {prompt: s, returns: {ok: {hits: int}}, executor: [subagent]}
        body: scan
    """, caps=SUBAGENT_CAPS)
    did = d.st["delegations"][0]["id"]
    with pytest.raises(runtime.FlowError):
        runtime.delegate_submit(did, "nope", None, **d.kw())
    d.st = runtime.delegate_submit(did, "nope", None, **d.kw())
    assert d.st["status"] == "failed"
    assert d.events("delegate_failed")


def test_subagent_receipt_path_submit(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          a: {prompt: A}
          b: {prompt: B}
        body:
          seq:
            - {seq: [a, b], executor: [subagent], id: pair}
    """, caps=SUBAGENT_CAPS)
    dl = d.st["delegations"][0]
    assert [t["label"] for t in dl["brief"]["tasks"]] == ["a", "b"]
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({"outcome": "ok", "value": {}}))
    d.st = runtime.delegate_submit(dl["id"], receipt_path=str(receipt), **d.kw())
    assert d.st["status"] == "done"


def test_headless_without_dispatcher_falls_back_and_journals(tmp_path):
    executors.register(None)
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, executor: [headless, subagent, inline]}
        body: t
    """, caps={"headless.run": True, "subagent.spawn": True})
    fb = d.events("fallback")
    assert any(e["route"] == "headless -> subagent" for e in fb)
    assert d.st["delegations"][0]["executor"] == "subagent"


def test_missing_caps_fall_back_to_inline(tmp_path):
    d = Drv(tmp_path, """
        nodes:
          t: {prompt: T, executor: [headless, subagent]}
        body: t
    """, caps={})
    assert d.tasks() == ["t"]
    causes = {e["cause"] for e in d.events("fallback")}
    assert {"headless.run", "subagent.spawn"} <= causes


def test_headless_with_dispatcher_is_queued(tmp_path):
    seen = []
    executors.register(lambda rec, run: seen.append(rec["id"]) or "running")
    try:
        d = Drv(tmp_path, """
            nodes:
              t: {prompt: T, executor: [headless]}
            body: t
        """, caps={"headless.run": True})
    finally:
        executors.register(None)
    assert seen == ["d1"] and d.st["delegations"][0]["status"] == "running"


def test_abort_and_restart_archives(tmp_path):
    d = Drv(tmp_path, TASKS + "body: a\n")
    with pytest.raises(runtime.FlowError, match="still"):
        Drv(tmp_path, TASKS + "body: a\n")
    d.st = runtime.abort("stop", **d.kw())
    assert d.st["status"] == "aborted"
    Drv(tmp_path, TASKS + "body: b\n")
    assert len(RunStore(str(tmp_path), "t").archived()) == 1


def test_rule_lookup(tmp_path):
    d = Drv(tmp_path, """
        rules:
          r1: {text: never merge, why: incident, deliver: [a]}
        nodes:
          a: {prompt: A}
        body: a
    """)
    assert d.st["tasks"][0]["rules"] == [{"id": "r1", "text": "never merge"}]
    assert runtime.rule("r1", cwd=d.cwd, scope="t")["why"] == "incident"
