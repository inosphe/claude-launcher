"""cflow v2 dynamic calls (docs/cflow-v2.md 2.12, claunch-z17c9): a task's
``calls: [...]`` allow-list, ``runtime.call`` starting a procedure resolved
from the layers as a nested run ``<scope>~c<n>``, the caller task waiting
(``calling``) and coming back active with ``call_result``, refusals, and
cancellation reaching the callee."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, mcp as cflow2_mcp, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

MAIN = """
cflow: 2
name: m
nodes:
  work:
    prompt: W
    calls: [probe, loopy, bad-return, wf, quick]
    returns: {ok: {seen: "str?"}}
  plain: {prompt: P}
body:
  - work
  - plain
"""

PROBE = """
cflow: 2
name: probe
kind: procedure
params:
  target: {type: str, required: true}
returns:
  ok: {found: str}
nodes:
  look: {prompt: "look at ${params.target}", returns: {ok: {found: str}}}
body:
  - look
  - {return: ok, value: {found: "$result.look.value.found"}}
"""

# calls itself: refused one level down
LOOPY = """
cflow: 2
name: loopy
kind: procedure
nodes:
  again: {prompt: A, calls: [loopy]}
body:
  - again
"""

BAD_RETURN = """
cflow: 2
name: bad-return
kind: procedure
returns:
  ok: {found: str}
nodes:
  t: {prompt: T}
body:
  - t
"""

# ends on entry: the result is back before flow_call returns
QUICK = """
cflow: 2
name: quick
kind: procedure
returns:
  ok: {n: int}
body:
  - {return: ok, value: {n: 3}}
"""

WF = """
cflow: 2
name: wf
nodes:
  t: {prompt: T}
body:
  - t
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    for name, text in (("probe", PROBE), ("loopy", LOOPY), ("bad-return", BAD_RETURN),
                       ("wf", WF), ("quick", QUICK), ("m", MAIN)):
        (v2 / f"{name}.yaml").write_text(textwrap.dedent(text), encoding="utf-8")


def _cwd(tmp_path):
    return str(tmp_path / "proj")


def _start(tmp_path, text=MAIN):
    chart = frontend.compile_text(textwrap.dedent(text))
    return runtime.start(chart, cwd=_cwd(tmp_path), scope="w1", caps={}, driver="w1", now=T0)


def _status(tmp_path, scope="w1"):
    return runtime.status(cwd=_cwd(tmp_path), scope=scope, now=T0, tick=False)


def _task(st, label):
    return next(t for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)


def _call(tmp_path, procedure, with_=None, scope="w1", node="work"):
    return runtime.call(procedure, node, with_, cwd=_cwd(tmp_path), scope=scope, now=T0)


def _report(tmp_path, scope, label, value=None, outcome="ok"):
    inst = _task(_status(tmp_path, scope), label)["instance"]
    return runtime.report(inst, outcome, value, cwd=_cwd(tmp_path), scope=scope, now=T0)


# -- compile ----------------------------------------------------------------------


def test_calls_compiles_to_the_task_props():
    chart = frontend.compile_text(textwrap.dedent(MAIN))
    work = next(n for n in chart.nodes.values() if n.label == "work")
    plain = next(n for n in chart.nodes.values() if n.label == "plain")
    assert work.get("calls") == ["probe", "loopy", "bad-return", "wf", "quick"]
    assert "calls" not in plain.props  # default deny; old charts keep their bytes


def test_calls_takes_names_only():
    with pytest.raises(frontend.FrontendError, match="not a path"):
        frontend.compile_text(textwrap.dedent("""
            cflow: 2
            name: x
            nodes:
              t: {prompt: T, calls: [../probe.yaml]}
            body: [t]
        """))


# -- a call and its return -----------------------------------------------------------


def test_call_runs_the_procedure_and_returns_to_the_same_task(tmp_path):
    _start(tmp_path)
    work = _task(_status(tmp_path), "work")
    assert work["calls"] == ["probe", "loopy", "bad-return", "wf", "quick"]
    out = _call(tmp_path, "probe", {"target": "x.py"})
    sub = out["called"]["scope"]
    assert sub.endswith("~c1") and out["called"]["instance"] == work["instance"]
    # the caller waits; its task is not reportable meanwhile
    assert out["tasks"] == [] and work["instance"] in out["active"]
    assert out["calls"][0]["status"] == "running" and out["calls"][0]["scope"] == sub
    with pytest.raises(runtime.FlowError):
        runtime.report(work["instance"], "ok", {}, cwd=_cwd(tmp_path), scope="w1", now=T0)
    # the callee is a run of its own, driven by the same driver
    callee = _status(tmp_path, sub)
    assert _task(callee, "look")["prompt"] == "look at x.py"
    assert runtime.may_drive(RunStore(_cwd(tmp_path), sub).load_state(), "w1")
    _report(tmp_path, sub, "look", {"found": "bug"})
    st = _status(tmp_path)
    back = _task(st, "work")
    assert back["instance"] == work["instance"]
    assert back["call_result"] == {"call": "c1", "procedure": "probe", "outcome": "ok",
                                   "value": {"found": "bug"}}
    assert st["calls"][0]["status"] == "returned"
    _report(tmp_path, "w1", "work", {"seen": back["call_result"]["value"]["found"]})
    _report(tmp_path, "w1", "plain")
    assert _status(tmp_path)["status"] == "done"
    kinds = [e["event"] for e in RunStore(_cwd(tmp_path), "w1").journal()]
    assert "call_started" in kinds and "call_returned" in kinds


def test_a_task_may_call_again_after_a_return(tmp_path):
    _start(tmp_path)
    _call(tmp_path, "probe", {"target": "a"})
    _report(tmp_path, "w1~c1", "look", {"found": "1"})
    out = _call(tmp_path, "probe", {"target": "b"})
    assert out["called"]["call"] == "c2"
    assert "call_result" not in str(out["tasks"])  # the old result went with the new call
    _report(tmp_path, "w1~c2", "look", {"found": "2"})
    assert _task(_status(tmp_path), "work")["call_result"]["value"] == {"found": "2"}


def test_a_callee_that_ends_on_entry_returns_at_once(tmp_path):
    _start(tmp_path)
    out = _call(tmp_path, "quick")
    assert _task(out, "work")["call_result"]["value"] == {"n": 3}
    assert out["calls"][0]["status"] == "returned"


def test_a_result_that_misses_returns_comes_back_as_fail(tmp_path):
    _start(tmp_path)
    _call(tmp_path, "bad-return")
    _report(tmp_path, "w1~c1", "t")
    res = _task(_status(tmp_path), "work")["call_result"]
    assert res["outcome"] == "fail" and "does not match its returns" in res["value"]["reason"]


# -- refusals ---------------------------------------------------------------------------


def _no_call_written(tmp_path):
    state = RunStore(_cwd(tmp_path), "w1").load_state()
    assert not state.get("calls")
    assert _task(_status(tmp_path), "work")


def test_a_procedure_not_in_calls_is_refused(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="may not call 'other'"):
        _call(tmp_path, "other")
    _no_call_written(tmp_path)


def test_a_task_without_calls_calls_nothing(tmp_path):
    _start(tmp_path)
    _report(tmp_path, "w1", "work")
    with pytest.raises(runtime.FlowError, match="none declared"):
        _call(tmp_path, "probe", {"target": "x"}, node="plain")


def test_params_are_checked_before_anything_starts(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="missing required param 'target'"):
        _call(tmp_path, "probe")
    with pytest.raises(runtime.FlowError, match="unknown params"):
        _call(tmp_path, "probe", {"target": "x", "nope": 1})
    _no_call_written(tmp_path)


def test_only_a_procedure_is_called(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="not a procedure"):
        _call(tmp_path, "wf")
    with pytest.raises(runtime.FlowError, match="not a path"):
        _call(tmp_path, "../wf")


def test_recursion_is_refused_in_the_callee(tmp_path):
    _start(tmp_path)
    _call(tmp_path, "loopy")
    with pytest.raises(runtime.FlowError, match="recursive call: loopy -> loopy"):
        _call(tmp_path, "loopy", scope="w1~c1", node="again")


# -- cancellation --------------------------------------------------------------------------


def test_aborting_the_caller_aborts_the_callee(tmp_path):
    _start(tmp_path)
    _call(tmp_path, "probe", {"target": "x"})
    runtime.abort("stop", cwd=_cwd(tmp_path), scope="w1", now=T0)
    assert RunStore(_cwd(tmp_path), "w1~c1").load_state()["status"] == "aborted"


def test_an_aborted_callee_returns_fail_to_the_task(tmp_path):
    _start(tmp_path)
    _call(tmp_path, "probe", {"target": "x"})
    runtime.abort("gone", cwd=_cwd(tmp_path), scope="w1~c1", now=T0)
    res = _task(_status(tmp_path), "work")["call_result"]
    assert res["outcome"] == "fail" and "aborted" in res["value"]["reason"]


# -- the MCP tool ------------------------------------------------------------------------


def test_flow_call_tool(tmp_path):
    _start(tmp_path)
    names = [t["name"] for t in cflow2_mcp.TOOLS]
    assert "flow_call" in names


# -- review fixes (REVIEW-1 on claunch-z17c9) ----------------------------------------------

OUTER = """
cflow: 2
name: outer
kind: procedure
nodes:
  mid: {prompt: M, calls: [probe]}
body:
  - mid
"""

# a nested delegation inside a procedure that calls itself again from there
DEEP = """
cflow: 2
name: deep
kind: procedure
nodes:
  d1: {prompt: D, calls: [deep]}
  d2: {prompt: E}
body:
  - id: sub
    executor: [subagent]
    seq:
      - d1
      - cond:
          - {if: "true", then: d2}
"""


def _write(tmp_path, name, text):
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    (v2 / f"{name}.yaml").write_text(textwrap.dedent(text), encoding="utf-8")


def test_aborting_the_caller_reaches_the_callees_callee(tmp_path):
    _write(tmp_path, "outer", OUTER)
    _start(tmp_path, MAIN.replace("calls: [probe,", "calls: [outer, probe,"))
    _call(tmp_path, "outer")
    _call(tmp_path, "probe", {"target": "x"}, scope="w1~c1", node="mid")
    runtime.abort("stop", cwd=_cwd(tmp_path), scope="w1", now=T0)
    for sub in ("w1~c1", "w1~c1~c1"):
        assert RunStore(_cwd(tmp_path), sub).load_state()["status"] == "aborted"


def test_a_session_delegated_caller_hands_its_callee_to_the_child(tmp_path, monkeypatch):
    from claude_launcher.cflow2 import executors

    monkeypatch.setattr(executors, "available", lambda cwd: True)
    monkeypatch.setattr(executors, "dispatch", lambda rec, run: None)
    chart = frontend.compile_text(textwrap.dedent("""
        cflow: 2
        name: sd
        nodes:
          a: {prompt: A, calls: [probe]}
          b: {prompt: B}
        body:
          - id: sub
            executor: [session]
            seq:
              - a
              - cond:
                  - {if: "true", then: b}
    """))
    caps = {"session.spawn": True, "mcp.claunch": True}
    st = runtime.start(chart, cwd=_cwd(tmp_path), scope="w1", caps=caps, driver="drv", now=T0)
    (d,) = st["delegations"]
    nested = d["nested"]["scope"]
    # no child yet: nobody could drive a callee
    with pytest.raises(runtime.FlowError, match="no session drives"):
        _call(tmp_path, "probe", {"target": "x"}, scope=nested, node="a")
    runtime.delegate_update(d["id"], cwd=_cwd(tmp_path), scope="w1", now=T0, child="kid",
                            assignee="kid")
    out = _call(tmp_path, "probe", {"target": "x"}, scope=nested, node="a")
    callee = RunStore(_cwd(tmp_path), out["called"]["scope"]).load_state()
    assert callee["driver"] == "kid" and runtime.may_drive(callee, "kid")
    assert not runtime.may_drive(callee, "drv")


def test_the_chain_crosses_delegation_links(tmp_path):
    _write(tmp_path, "deep", DEEP)
    chart = frontend.compile_text(textwrap.dedent(
        MAIN.replace("calls: [probe,", "calls: [deep, probe,")))
    # the callee inherits the caps: a subagent may be spawned, so 'sub' nests
    runtime.start(chart, cwd=_cwd(tmp_path), scope="w1", caps={"subagent.spawn": True, "mcp.claunch": True},
                  driver="w1", now=T0)
    out = _call(tmp_path, "deep")
    callee = out["called"]["scope"]
    (d,) = _status(tmp_path, callee)["delegations"]
    with pytest.raises(runtime.FlowError, match="recursive call: deep -> deep"):
        _call(tmp_path, "deep", scope=d["nested"]["scope"], node="d1")


def test_slot_migration_requalifies_call_scopes():
    from claude_launcher.cflow2 import slot_migration

    out = slot_migration.requalify(
        {"scope": "w1", "calls": {"c1": {"nested": {"cwd": "/x", "scope": "w1~c1"}}}}, "other")
    assert out["calls"]["c1"]["nested"]["scope"] == "w1@other~c1"


# -- display: web run page, run list, CLI status (claunch-z17c9.1) -----------------------


def test_the_web_run_page_lists_calls_and_links_the_callee(tmp_path):
    from claude_launcher.daemon import cflow2_web

    _start(tmp_path)
    _call(tmp_path, "probe", {"target": "x"})
    detail = cflow2_web.run_detail(_cwd(tmp_path), "w1")
    (c,) = detail["calls"]
    assert c["id"] == "c1" and c["procedure"] == "probe" and c["status"] == "running"
    assert c["label"] == "work" and c["scope"].endswith("~c1") and c["cwd"] == _cwd(tmp_path)
    assert c["outcome"] is None
    # the callee's own page exists under that slot
    assert cflow2_web.run_detail(_cwd(tmp_path), c["scope"])["run"]["workflow"] == "probe"
    assert cflow2_web.run_summary(_cwd(tmp_path), "w1")["calls"] == 1
    _report(tmp_path, "w1~c1", "look", {"found": "bug"})
    (c,) = cflow2_web.run_detail(_cwd(tmp_path), "w1")["calls"]
    assert c["status"] == "returned" and c["outcome"] == "ok"
    assert cflow2_web.run_summary(_cwd(tmp_path), "w1")["calls"] == 0


def test_cli_status_prints_calls_and_the_call_result(tmp_path, capsys):
    from claude_launcher import cli_cflow2

    _start(tmp_path)
    cli_cflow2._print_status(_call(tmp_path, "probe", {"target": "x"}), False)
    out = capsys.readouterr().out
    assert "  call c1 work: probe running (scope w1@main~c1)" in out
    _report(tmp_path, "w1~c1", "look", {"found": "bug"})
    cli_cflow2._print_status(_status(tmp_path), False)
    out = capsys.readouterr().out
    assert "  call c1 work: probe returned -> ok (scope w1@main~c1)" in out
    assert "    call c1 probe returned ok" in out
