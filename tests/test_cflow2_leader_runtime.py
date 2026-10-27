"""cflow v2 runtime features the leader needs (claunch-yq80p.6, docs/cflow-v2.md):
``recur`` (2.1), a task's ``restart`` (2.2), a choose option's ``interval``
(2.3) and ``wait.checks`` (2.3), plus the clock's side of each."""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import check, executors, frontend, inputs, ir, runtime
from claude_launcher.daemon import cflow2_clock

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    executors.register(None)
    yield
    executors.register(None)


def _compile(src):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))


def _start(cwd, src, scope="own", now=T0, **kw):
    return runtime.start(_compile(src), cwd=str(cwd), scope=scope, caps={}, now=now,
                         driver=scope, **kw)


def _kw(cwd, now=T0, scope="own"):
    return {"cwd": str(cwd), "scope": scope, "now": now}


def _events(cwd, name, scope="own"):
    return [e for e in runtime.journal(cwd=str(cwd), scope=scope) if e["event"] == name]


def _err(src):
    with pytest.raises(frontend.FrontendError) as exc:
        _compile(src)
    return str(exc.value)


class FakeSession:
    def __init__(self, name, cwd):
        self.sdef = SimpleNamespace(name=name, cwd=cwd)
        self.exited = False
        self.delivered = []

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        return self.sessions[name]


def _clock(mgr, **kw):
    kw.setdefault("probe_runner", None)
    kw.setdefault("threaded", False)
    kw.setdefault("heartbeat", False)
    return cflow2_clock.Cflow2Clock(mgr, poll=5, **kw)


def _deliver(mgr, notices):
    for _cwd, name, text in notices:
        mgr.get(name).queue_delivery(text)


# -- frontend -------------------------------------------------------------------------


def test_recur_compiles_to_meta_and_rejects_bad_forms():
    base = "nodes:\n  a: {prompt: A}\nbody: a\n"
    assert _compile("recur: true\n" + base).meta["recur"] == {"auto": False, "min_gap": 60.0}
    assert _compile("recur: {auto: true}\n" + base).meta["recur"] == {"auto": True,
                                                                     "min_gap": 60.0}
    assert _compile("recur: {auto: true, min_gap: 0}\n" + base).meta["recur"]["min_gap"] == 0
    assert "recur.min_gap must be" in _err("recur: {auto: true, min_gap: -1}\n" + base)
    assert "recur" not in _compile("recur: false\n" + base).meta
    assert "recur.auto must be" in _err("recur: {auto: 1}\n" + base)
    assert "unknown key" in _err("recur: {every: 3}\n" + base)
    assert "recur must be" in _err("recur: 3\n" + base)
    assert "only a workflow" in _err("kind: procedure\nrecur: true\n" + base)


def test_restart_task_compiles_and_rejects_bad_forms():
    chart = _compile("""
        nodes:
          r: {prompt: R, restart: {linux: "bash tools/restart_live.sh", timeout: 900}}
        body: r
    """)
    assert chart.node("r").get("restart") == {
        "windows": None, "linux": "bash tools/restart_live.sh", "timeout": 900.0}
    assert _compile("nodes:\n  r: {prompt: R, restart: {windows: tools/restart_live.ps1}}\nbody: r\n") \
        .node("r").get("restart")["timeout"] == 120.0
    assert "needs a windows or linux" in _err(
        "nodes:\n  r: {prompt: R, restart: {timeout: 5}}\nbody: r\n")
    assert "restart.timeout must be" in _err(
        "nodes:\n  r: {prompt: R, restart: {linux: tools/restart_live.sh, timeout: 901}}\nbody: r\n")
    assert "is not substituted" in _err(
        "nodes:\n  r: {prompt: R, restart: {linux: 'run ${params.x}'}}\nbody: r\n")


def test_restart_inside_a_delegation_is_an_error():
    chart = _compile("""
        nodes:
          r: {prompt: R, restart: {linux: tools/restart_live.sh}}
        body:
          seq: [r]
          executor: subagent
    """)
    codes = [f.code for f in check.check(chart)]
    assert "delegated-restart" in codes


def test_interval_compiles_and_needs_an_agent_chooser():
    chart = _compile("""
        nodes:
          a: {prompt: A}
        body:
          choose:
            options:
              go: {then: a, interval: 300}
              stop: {then: end}
    """)
    opts = {o["name"]: o for o in chart.node(chart.root).get("options")}
    assert opts["go"]["interval"] == 300.0 and "interval" not in opts["stop"]
    assert "needs chooser: agent" in _err("""
        body:
          choose:
            chooser: user
            options: {go: {then: end, interval: 5}}
    """)
    assert "greater than 0" in _err("""
        body:
          choose:
            options: {go: {then: end, interval: 0}}
    """)


def test_wait_checks_compile_and_reject_bad_forms():
    chart = _compile("""
        body:
          wait:
            poll: 30
            seconds: 600
            checks:
              - {id: deployed, describe: live, probe: "deploy_check"}
              - {id: clean, probe: [git, diff, --quiet]}
    """)
    w = chart.node(chart.root)
    assert [c["id"] for c in w.get("checks")] == ["deployed", "clean"]
    assert w.get("probe") is None and w.get("undecided_max") == 3
    assert "undecidable" in str(check._possible_outcomes(w))
    assert "duplicate id" in _err("""
        body:
          wait: {checks: [{id: a, probe: x}, {id: a, probe: y}]}
    """)
    assert "cannot be used with probe" in _err("""
        body:
          wait: {probe: x, checks: [{id: a, probe: y}]}
    """)
    assert "needs a probe" in _err("body:\n  wait: {checks: [{id: a}]}\n")
    assert "at least one item" in _err("body:\n  wait: {checks: []}\n")


# -- choose option interval --------------------------------------------------------------

INTERVAL = """
    nodes:
      merge: {prompt: M}
    body:
      loop:
        max: 10
        while: "true"
        do:
          choose:
            options:
              integrate: {then: merge, interval: 300}
              wait: {then: {continue: null}}
"""


def _choice(cwd, now):
    st = runtime.status(**_kw(cwd, now))
    return st, (st["choices"][0] if st["choices"] else None)


def test_first_take_is_immediate_and_recorded(tmp_path):
    _start(tmp_path, INTERVAL)
    st = runtime.select(None, "integrate", "batch 1", **_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["merge"]
    state = runtime.RunStore(str(tmp_path), "own").load_state()
    assert list(state["windows"].values()) == [T0]


def test_take_inside_the_window_is_held_then_released_with_latest_reason(tmp_path):
    _start(tmp_path, INTERVAL)
    runtime.select(None, "integrate", "batch 1", **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path, T0 + 10))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 10))
    st = runtime.select(None, "integrate", "batch 2: a", **_kw(tmp_path, T0 + 20))
    assert st["held"]["option"] == "integrate" and st["held"]["opens_at"] == T0 + 300
    assert st["tasks"] == [] and st["waiting_window"][0]["option"] == "integrate"
    assert "held until" in st["note"]
    # choosing it again only updates the reason
    runtime.select(None, "integrate", "batch 2: a, b", **_kw(tmp_path, T0 + 40))
    held = _events(tmp_path, "select_held")
    assert len(held) == 2 and held[1]["updated"] is True
    # not yet open
    st = runtime.wait_tick(T0 + 299, cwd=str(tmp_path), scope="own")
    assert st["tasks"] == [] and st["ticked"] == 0
    st = runtime.wait_tick(T0 + 300, cwd=str(tmp_path), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["merge"] and st["ticked"] == 1
    sel = _events(tmp_path, "selected")[-1]
    assert sel["by"] == "window" and sel["reason"] == "batch 2: a, b"
    state = runtime.RunStore(str(tmp_path), "own").load_state()
    assert list(state["windows"].values()) == [T0 + 300]


def test_another_option_cancels_the_hold(tmp_path):
    _start(tmp_path, INTERVAL)
    runtime.select(None, "integrate", "x", **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path, T0 + 1))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    runtime.select(None, "integrate", "y", **_kw(tmp_path, T0 + 2))
    st = runtime.select(None, "wait", "nothing to do", **_kw(tmp_path, T0 + 3))
    assert _events(tmp_path, "hold_cancelled")[0]["option"] == "integrate"
    _st, choice = _choice(tmp_path, T0 + 400)
    assert choice is not None and "held" not in choice  # nothing released later
    assert [o.get("window_opens_at") for o in choice["options"]
            if o["name"] == "integrate"] == [None]


def test_a_person_is_not_paced(tmp_path):
    _start(tmp_path, INTERVAL)
    runtime.select(None, "integrate", "x", **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path, T0 + 1))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    _st, choice = _choice(tmp_path, T0 + 5)
    assert [o["window_opens_at"] for o in choice["options"] if o["name"] == "integrate"]
    st = runtime.select(None, "integrate", "now", caller=runtime.CALLER_USER,
                        **_kw(tmp_path, T0 + 5))
    assert [t["label"] for t in st["tasks"]] == ["merge"]
    assert _events(tmp_path, "selected")[-1]["override"] is True


def test_windows_carry_to_the_next_run_of_the_slot(tmp_path):
    src = """
        nodes:
          merge: {prompt: M}
        body:
          choose:
            options:
              integrate: {then: merge, interval: 300}
              idle: {then: end}
    """
    _start(tmp_path, src)
    st = runtime.select(None, "integrate", "x", **_kw(tmp_path))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    _start(tmp_path, src, now=T0 + 10)
    st = runtime.select(None, "integrate", "y", **_kw(tmp_path, T0 + 10))
    assert st["held"]["opens_at"] == T0 + 300


def test_hold_dropped_when_the_guard_turns_false(tmp_path):
    _start(tmp_path, """
        data: {open: true}
        nodes:
          merge: {prompt: M}
          close: {prompt: C}
        body:
          loop:
            max: 5
            while: "true"
            do:
              choose:
                options:
                  integrate: {then: merge, interval: 100, when: "$data.open"}
                  other: {then: close}
    """)
    st = runtime.select(None, "integrate", "x", **_kw(tmp_path))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    runtime.select(None, "integrate", "y", **_kw(tmp_path, T0 + 2))
    store = runtime.RunStore(str(tmp_path), "own")
    state = store.load_state()
    state["data"]["open"] = False
    store.save_state(state)
    st = runtime.wait_tick(T0 + 100, cwd=str(tmp_path), scope="own")
    assert st["tasks"] == [] and st["choices"] and "held" not in st["choices"][0]
    assert "guard is false" in _events(tmp_path, "hold_cancelled")[-1]["why"]


# -- choose option batch (claunch-1o6az) ----------------------------------------------

BATCHED = """
    collections:
      jobs:
        kind: queue
        key: id
        item: {id: str}
        status: {initial: requested, values: [requested, done]}
    nodes:
      merge: {prompt: M}
    body:
      loop:
        max: 10
        while: "true"
        do:
          choose:
            options:
              integrate: {then: merge, interval: 300,
                          batch: {collection: jobs, status: requested, size: 3}}
              wait: {then: {continue: null}}
"""


def _push(cwd, key, now):
    runtime.collection_op("push", "jobs", cwd=str(cwd), scope="own", principal="driver",
                          item={"id": key}, now=now)


def test_batch_compiles_and_needs_an_interval():
    chart = _compile(BATCHED)
    choose = next(n for n in chart.nodes.values() if n.get("options"))
    opts = {o["name"]: o for o in choose.get("options")}
    assert opts["integrate"]["batch"] == {"collection": "jobs", "status": "requested", "size": 3}
    assert "needs an interval" in _err("""
        body:
          choose:
            options:
              go: {then: end, batch: {collection: jobs, status: requested, size: 3}}
    """)
    assert "size must be" in _err("""
        body:
          choose:
            options:
              go: {then: end, interval: 5, batch: {collection: jobs, status: requested, size: 0}}
    """)


def test_a_full_batch_opens_the_window_at_once(tmp_path):
    _start(tmp_path, BATCHED)
    runtime.select(None, "integrate", "first", **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path, T0 + 1))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    for i, key in enumerate("abc"):
        _push(tmp_path, key, T0 + 2 + i)
    st = runtime.select(None, "integrate", "three wait", **_kw(tmp_path, T0 + 10))
    assert "held" not in st and [t["label"] for t in st["tasks"]] == ["merge"]


def test_a_partial_batch_opens_300_seconds_after_the_first_arrival(tmp_path):
    _start(tmp_path, BATCHED)
    _push(tmp_path, "a", T0 + 100)
    _push(tmp_path, "b", T0 + 150)
    # the last take is long past: the first arrival is the reference
    st = runtime.select(None, "integrate", "two wait", **_kw(tmp_path, T0 + 200))
    assert st["held"]["opens_at"] == T0 + 400
    assert st["waiting_window"][0]["opens_at"] == runtime._iso(T0 + 400)
    st = runtime.wait_tick(T0 + 399, cwd=str(tmp_path), scope="own")
    assert st["tasks"] == [] and st["ticked"] == 0
    st = runtime.wait_tick(T0 + 400, cwd=str(tmp_path), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["merge"] and st["ticked"] == 1


def test_the_third_candidate_releases_a_hold_before_the_deadline(tmp_path):
    _start(tmp_path, BATCHED)
    _push(tmp_path, "a", T0 + 100)
    st = runtime.select(None, "integrate", "one waits", **_kw(tmp_path, T0 + 110))
    assert st["held"]["opens_at"] == T0 + 400
    st = runtime.wait_tick(T0 + 200, cwd=str(tmp_path), scope="own")
    assert st["tasks"] == []
    _push(tmp_path, "b", T0 + 210)
    _push(tmp_path, "c", T0 + 220)
    st = runtime.wait_tick(T0 + 230, cwd=str(tmp_path), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["merge"]
    assert _events(tmp_path, "selected")[-1]["by"] == "window"


def test_batch_status_reports_count_and_deadline(tmp_path):
    _start(tmp_path, BATCHED)
    _push(tmp_path, "a", T0 + 100)
    _push(tmp_path, "b", T0 + 150)
    _st, choice = _choice(tmp_path, T0 + 160)
    batch = [o for o in choice["options"] if o["name"] == "integrate"][0]["batch"]
    assert batch["count"] == 2 and batch["size"] == 3
    assert batch["first_at"] == runtime._iso(T0 + 100)
    assert batch["opens_at"] == runtime._iso(T0 + 400) and batch["opens_epoch"] == T0 + 400


def test_no_candidate_means_nothing_to_wait_for(tmp_path):
    _start(tmp_path, BATCHED)
    runtime.select(None, "integrate", "first", **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path, T0 + 1))
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 1))
    st = runtime.select(None, "integrate", "again", **_kw(tmp_path, T0 + 2))
    assert "held" not in st and [t["label"] for t in st["tasks"]] == ["merge"]


# -- recur -----------------------------------------------------------------------------


def test_plain_recur_files_a_request_the_driver_performs(tmp_path):
    st = _start(tmp_path, "recur: true\nnodes:\n  a: {prompt: A}\nbody: a\n")
    assert st["round"] == 1
    st = runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    assert st["status"] == "done"
    assert st["next_round"]["round"] == 2 and st["next_round"]["auto"] is False
    assert "flow_start" in st["note"]
    assert runtime.start_next_round(str(tmp_path), "own") is None  # not auto
    st = _start(tmp_path, "recur: true\nnodes:\n  a: {prompt: A}\nbody: a\n", now=T0 + 5)
    assert st["round"] == 2


def test_failed_round_files_nothing(tmp_path):
    st = _start(tmp_path, "recur: {auto: true}\nnodes:\n  a: {prompt: A}\nbody: a\n")
    st = runtime.report(st["tasks"][0]["instance"], "fail", **_kw(tmp_path))
    assert st["status"] == "failed" and "next_round" not in st


def test_auto_recur_round_started_by_the_clock(tmp_path):
    src = """
        recur: {auto: true}
        inputs:
          goal: {type: string, required: true}
        params: {mode: {default: x}}
        nodes:
          a: {prompt: "A ${data.inputs.goal}"}
        body: a
    """
    st = _start(tmp_path, src, inputs={"goal": "g1"}, params={"mode": "y"})
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr)
    # recur.min_gap (default 60s): not before the gap has passed
    _deliver(mgr, clock.scan(now=T0 + 5))
    assert runtime.status(cwd=str(tmp_path), scope="own", tick=False)["round"] == 1
    _deliver(mgr, clock.scan(now=T0 + 61))
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert st["status"] == "running" and st["round"] == 2
    assert st["tasks"][0]["prompt"] == "A g1" and st["inputs"] == {"goal": "g1"}
    assert runtime.RunStore(str(tmp_path), "own").load_state()["params"] == {"mode": "y"}
    assert st["driver"] == "own"
    assert "next round started" in mgr.get("own").delivered[-1]
    assert _events(tmp_path, "round_auto_started")[0]["round"] == 2
    # single use: the next pass starts nothing
    clock.scan(now=T0 + 200)
    assert runtime.status(cwd=str(tmp_path), scope="own", tick=False)["round"] == 2


def test_cancel_stops_the_loop(tmp_path):
    st = _start(tmp_path, "recur: {auto: true}\nnodes:\n  a: {prompt: A}\nbody: a\n")
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    st = runtime.cancel_next_round("enough", **_kw(tmp_path))
    assert "next_round" not in st
    assert runtime.start_next_round(str(tmp_path), "own", T0 + 100) is None
    with pytest.raises(runtime.FlowError, match="no next round"):
        runtime.cancel_next_round(None, **_kw(tmp_path))


def test_reloaded_inputs_normalize_again():
    schema = inputs.schema({"goal": {"type": "text"}, "rounds": {"type": "int"},
                            "inv": {"type": "invariants"}, "tags": {"type": "list"},
                            "flag": {"type": "bool"}})
    raw = {"goal": "x\ny", "rounds": "3", "inv": "keep it green", "tags": "a,b",
           "flag": "yes"}
    once, errors = inputs.normalize(schema, raw)
    assert not errors
    twice, errors = inputs.normalize(schema, once)
    assert not errors and twice == once


# -- restart --------------------------------------------------------------------------

RESTART = """
    nodes:
      reflect:
        prompt: R
        restart: {linux: "bash tools/restart_live.sh", windows: "pwsh -File tools/restart_live.ps1", timeout: 60}
        returns: {ok: {}, again: {}}
    body:
      loop:
        max: 3
        until: "$result.reflect.outcome == 'ok'"
        do:
          seq: [reflect]
          on: {again: {continue: null}}
"""


def test_restart_runs_once_per_visit_and_gates_the_report(tmp_path):
    st = _start(tmp_path, RESTART)
    iid = st["tasks"][0]["instance"]
    assert st["tasks"][0]["restart"]["status"] == "pending"
    with pytest.raises(runtime.FlowError, match="restart is pending"):
        runtime.report(iid, **_kw(tmp_path))
    acts = runtime.claim_restarts(platform="Linux", boot="b1", **_kw(tmp_path))
    assert [(a["kind"], a["command"], a["timeout"]) for a in acts] == \
        [("run", "bash tools/restart_live.sh", 60.0)]
    assert runtime.claim_restarts(platform="Linux", boot="b1", **_kw(tmp_path)) == []
    with pytest.raises(runtime.FlowError, match="restart is running"):
        runtime.report(iid, **_kw(tmp_path))
    assert runtime.complete_restart(iid, "wrong", 0, "", **_kw(tmp_path)) is None
    done = runtime.complete_restart(iid, acts[0]["token"], 0, "restarted", **_kw(tmp_path))
    assert done["status"] == "succeeded" and done["driver"] == "own"
    st = runtime.status(**_kw(tmp_path))
    assert st["tasks"][0]["restart"]["exit_code"] == 0
    st = runtime.report(iid, "again", **_kw(tmp_path))
    # the loop re-enters reflect: a new visit, a new restart
    assert st["tasks"][0]["restart"]["status"] == "pending"
    acts = runtime.claim_restarts(platform="Windows", boot="b1", **_kw(tmp_path))
    assert acts[0]["command"] == "pwsh -File tools/restart_live.ps1"
    names = [e["event"] for e in runtime.journal(cwd=str(tmp_path), scope="own")]
    assert names.count("restart_requested") == 2 and "restart_completed" in names


def test_restart_of_another_boot_is_interrupted_not_rerun(tmp_path):
    st = _start(tmp_path, RESTART)
    iid = st["tasks"][0]["instance"]
    runtime.claim_restarts(platform="Linux", boot="b1", **_kw(tmp_path))
    acts = runtime.claim_restarts(platform="Linux", boot="b2", **_kw(tmp_path))
    assert [a["kind"] for a in acts] == ["interrupted"]
    assert runtime.claim_restarts(platform="Linux", boot="b2", **_kw(tmp_path)) == []
    runtime.report(iid, **_kw(tmp_path))  # interrupted is final


def test_restart_unsupported_platform(tmp_path):
    st = _start(tmp_path, "nodes:\n  r: {prompt: R, restart: {windows: tools/restart_live.ps1}}\nbody: r\n")
    acts = runtime.claim_restarts(platform="Darwin", boot="b", **_kw(tmp_path))
    assert acts[0]["kind"] == "unsupported"
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    assert _events(tmp_path, "restart_unsupported")


def test_clock_runs_restart_with_the_driver_session(tmp_path):
    st = _start(tmp_path, RESTART)
    ran = []

    def runner(cwd, action, session):
        ran.append((cwd, action["command"], session))
        return {"exit_code": 0, "output": "ok"}

    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, restart_runner=runner, platform="Linux")
    _deliver(mgr, clock.scan(now=T0 + 1))
    assert "restart -- machine-generated" in mgr.get("own").delivered[-1]
    assert len(clock.restarts) == 1 and ran == []
    cwd, scope, action = clock.restarts.pop()
    _deliver(mgr, clock.perform_restart(cwd, scope, action))
    assert ran == [(str(tmp_path), "bash tools/restart_live.sh", "own")]
    assert "exit code: 0 (succeeded)" in mgr.get("own").delivered[-1]
    _deliver(mgr, clock.scan(now=T0 + 2))  # claimed once per visit
    assert clock.restarts == []
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path, T0 + 3))


# -- wait checks ----------------------------------------------------------------------

CHECKS = """
    nodes:
      a: {prompt: A}
      late: {prompt: L}
    body:
      seq:
        - wait:
            poll: 10
            seconds: 600
            undecided_max: 2
            checks:
              - {id: deployed, describe: live, probe: "deploy"}
              - {id: clean, probe: "clean"}
          on: {timeout: late, undecidable: late}
        - a
"""


def _runner(codes):
    def run(cmd, cwd):
        return (codes[cmd], f"{cmd} said {codes[cmd]}")
    return run


def test_checks_pass_only_when_every_item_holds(tmp_path):
    _start(tmp_path, CHECKS)
    codes = {"deploy": 1, "clean": 0}
    st = runtime.wait_tick(T0 + 1, _runner(codes), cwd=str(tmp_path), scope="own")
    w = st["waits"][0]
    assert [(c["id"], c["verdict"], c["exit"]) for c in w["checks"]] == \
        [("deployed", "not_yet", 1), ("clean", "holds", 0)]
    codes["deploy"] = 0
    st = runtime.wait_tick(T0 + 12, _runner(codes), cwd=str(tmp_path), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["a"]
    ev = _events(tmp_path, "checks_passed")[0]
    assert [c["id"] for c in ev["checks"]] == ["deployed", "clean"]
    assert ev["checks"][0]["output"] == "deploy said 0"


def test_checks_undecided_and_timeout(tmp_path):
    _start(tmp_path, CHECKS)
    codes = {"deploy": 2, "clean": 1}
    runtime.wait_tick(T0 + 1, _runner(codes), cwd=str(tmp_path), scope="own")
    st = runtime.wait_tick(T0 + 12, _runner(codes), cwd=str(tmp_path), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["late"]
    assert [e["count"] for e in _events(tmp_path, "probe_undecided")] == [1, 2]

    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    _start(tmp2, CHECKS)
    runtime.wait_tick(T0 + 1, _runner({"deploy": 1, "clean": 0}), cwd=str(tmp2), scope="own")
    st = runtime.wait_tick(T0 + 600, cwd=str(tmp2), scope="own")
    assert [t["label"] for t in st["tasks"]] == ["late"]
    assert _events(tmp2, "checks_expired")[0]["checks"][0]["id"] == "deployed"


# -- the clock wakes the driver ----------------------------------------------------------


def test_clock_tells_the_driver_when_a_wait_moves_the_run(tmp_path):
    _start(tmp_path, """
        nodes:
          a: {prompt: A}
        body:
          seq:
            - wait: {seconds: 30}
            - a
    """)
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr)
    _deliver(mgr, clock.scan(now=T0 + 5))
    assert mgr.get("own").delivered == []
    _deliver(mgr, clock.scan(now=T0 + 31))
    assert len(mgr.get("own").delivered) == 1
    assert "waiting on you: a" in mgr.get("own").delivered[0]
    _deliver(mgr, clock.scan(now=T0 + 40))
    assert len(mgr.get("own").delivered) == 1


def test_declared_non_ok_outcome_ends_the_loop(tmp_path):
    st = _start(tmp_path, """
        recur: {auto: true}
        returns: {wound_down: {}}
        nodes:
          a: {prompt: A}
        body:
          seq: [a, {return: wound_down}]
    """)
    st = runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    assert st["status"] == "done" and st["outcome"] == "wound_down"
    assert "next_round" not in st


def test_a_cancel_during_the_reload_wins(tmp_path, monkeypatch):
    """Y2: start_next_round reads the request, then loads the definition; a
    cancel landing in between must not be lost (the start re-checks under
    the slot lock)."""
    st = _start(tmp_path, "recur: {auto: true}\nnodes:\n  a: {prompt: A}\nbody: a\n")
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))
    real = runtime._next_round_chart

    def reload_then_cancel(store, state):
        out = real(store, state)
        runtime.cancel_next_round("stop", **_kw(tmp_path, T0 + 70))
        return out
    monkeypatch.setattr(runtime, "_next_round_chart", reload_then_cancel)
    assert runtime.start_next_round(str(tmp_path), "own", T0 + 70) is None
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert st["status"] == "done" and st["round"] == 1 and "next_round" not in st
    assert not _events(tmp_path, "round_auto_started")


def test_restart_commands_outside_the_allowlist_are_refused():
    """Y5: the daemon runs a restart with its own rights, so only the
    project's restart_live scripts compile."""
    for cmd in ("claunch daemon stop", "bash -c claunch tools/restart_live.sh",
                "bash tools/restart_live.sh; claunch daemon stop", "bash restart.sh",
                "pwsh -Command x tools/restart_live.ps1"):
        assert "restart.linux" in _err(
            "nodes:\n  r: {prompt: R, restart: {linux: '%s'}}\nbody: r\n" % cmd), cmd
    for cmd in ("bash tools/restart_live.sh", "./tools/restart_live.sh",
                "powershell -NoProfile -ExecutionPolicy Bypass -File tools/restart_live.ps1"):
        assert ir.restart_refusal(cmd) is None, cmd


@pytest.mark.parametrize("storage", ["legacy", "checkpoint"])
def test_a_snapshot_with_a_refused_command_is_not_run(tmp_path, storage):
    from claude_launcher.cflow2 import checkpoint

    st = _start(tmp_path, "nodes:\n  r: {prompt: R, restart: {linux: tools/restart_live.sh}}\n"
                          "body: r\n")
    store = runtime.RunStore(str(tmp_path), "own")
    chart = json.loads(store.chart_path.read_text())
    node = next(n for n in chart["nodes"].values() if n.get("restart"))
    node["restart"]["linux"] = "claunch daemon stop"
    text = json.dumps(chart)
    if storage == "legacy":
        store.checkpoint_path.unlink()
        store.chart_path.write_text(text)
    else:
        # Exercise the runtime allowlist even with a well-formed, checksummed
        # persisted definition that predates the command's refusal.
        with checkpoint.connect(store.checkpoint_path) as db:
            db.execute("UPDATE definition SET body=?,sha=? WHERE id=1",
                       (text, checkpoint.digest(text)))
    acts = runtime.claim_restarts(platform="Linux", boot="b", **_kw(tmp_path))
    assert [a["kind"] for a in acts] == ["refused"]
    assert "refused" in cflow2_clock.restart_block(acts[0])
    st = runtime.status(**_kw(tmp_path))
    assert st["tasks"][0]["restart"]["status"] == "failed"
    assert _events(tmp_path, "restart_refused")
    runtime.report(st["tasks"][0]["instance"], **_kw(tmp_path))  # failed is final


def test_a_runner_exception_is_recorded_as_failed(tmp_path):
    """Y3: a runner that raises must not leave the record 'running'."""
    st = _start(tmp_path, RESTART)

    def runner(cwd, action, session):
        raise RuntimeError("no temp dir")
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, restart_runner=runner, platform="Linux")
    _deliver(mgr, clock.scan(now=T0 + 1))
    cwd, scope, action = clock.restarts.pop()
    _deliver(mgr, clock.perform_restart(cwd, scope, action))
    rec = runtime.status(**_kw(tmp_path, T0 + 2))["tasks"][0]["restart"]
    assert rec["status"] == "failed" and "no temp dir" in rec["output"]
    assert "exit code: None (failed)" in mgr.get("own").delivered[-1]
    runtime.report(st["tasks"][0]["instance"], "again", **_kw(tmp_path, T0 + 3))
