"""cflow v2 side runs (docs/cflow-v2.md 2.9): slots ``<session>+<side>``,
``sides:``, reads across the session's runs, named consume, ``side()``,
the daemon ending them with the session, ``max_visits: 0``, and the
stack-v2 / improv-worker-v2 wiring."""

from __future__ import annotations

import os
import textwrap
import types

import pytest

from claude_launcher.cflow2 import check, expr, frontend, mcp as cflow2_mcp, runtime
from claude_launcher.cflow2 import store as cstore
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

MAIN = """
cflow: 2
name: m
sides: {s: side-def}
collections:
  signals: {kind: log, item: {name: str, tip: "str?"}, lifecycle: {inherit: true}}
nodes:
  ask-cut:
    prompt: "side live: ${data.x}"
    do:
      - push: {to: signals, item: {name: "'cut-wanted'"}}
        when: "$outcome == 'ok'"
  frozen:
    prompt: F
    do:
      - push: {to: signals, item: {name: "'frozen'"}}
  merge: {prompt: M}
  begin: {prompt: B}
body:
  - begin
  - id: open-check
    cond:
      - {if: "side('s').live", then: [ask-cut]}
      - {else: []}
  - id: cut-wait
    wait:
      until: "fresh('side:s:signals', 'cut')"
      consume: [{from: "side:s:signals", name: cut}]
      seconds: 3600
    on:
      absent: {return: absent}
  - merge
  - frozen
returns:
  absent: {}
"""

SIDE = """
cflow: 2
name: side-def
max_visits: 0
collections:
  signals: {kind: log, item: {name: str, tip: "str?"}, lifecycle: {inherit: true}}
nodes:
  open: {prompt: O}
  cut:
    prompt: C
    returns: {ok: {tip: str}}
    do:
      - {consume: {from: "main:signals", name: cut-wanted}}
      - push: {to: signals, item: {name: "'cut'", tip: "$result.cut.value.tip"}}
  note-frozen:
    prompt: N
    do:
      - {consume: {from: "main:signals", name: frozen}}
body:
  - open
  - id: loop
    loop:
      while: "true"
      max: 50
      do:
        id: standby
        choose:
          options:
            cut:
              when: "fresh('main:signals', 'cut-wanted')"
              then: cut
            frozen:
              when: "fresh('main:signals', 'frozen')"
              then: note-frozen
            seal:
              then: {break: loop}
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    # the definitions live in the project layer of the run's directory
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "side-def.yaml").write_text(textwrap.dedent(SIDE), encoding="utf-8")
    (v2 / "m.yaml").write_text(textwrap.dedent(MAIN), encoding="utf-8")


def _cwd(tmp_path):
    return str(tmp_path / "proj")


def _start_main(tmp_path, text=MAIN):
    chart = frontend.compile_text(textwrap.dedent(text))
    return runtime.start(chart, cwd=_cwd(tmp_path), scope="w1", caps={}, driver="w1", now=T0)


def _start_side(tmp_path, **kw):
    return runtime.start_side("s", cwd=_cwd(tmp_path), main="w1", caps={}, now=T0, **kw)


def _status(tmp_path, scope):
    return runtime.status(cwd=_cwd(tmp_path), scope=scope, now=T0, tick=False)


def _task(st, label):
    return next(t["instance"] for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)


def _report(tmp_path, scope, label, value=None):
    st = _status(tmp_path, scope)
    return runtime.report(_task(st, label), "ok", value, cwd=_cwd(tmp_path), scope=scope,
                          now=T0)


def _avail(tmp_path, scope):
    ch = _status(tmp_path, scope)["choices"][0]
    out = {o["name"]: True for o in ch["options"]}
    out.update({o["name"]: False for o in ch["unavailable"]})
    return out


def _select(tmp_path, scope, option, reason=None):
    st = _status(tmp_path, scope)
    node = st["choices"][0]["instance"]
    return runtime.select(node, option, reason, cwd=_cwd(tmp_path), scope=scope, now=T0)


# -- scopes -------------------------------------------------------------------


def test_scope_helpers():
    assert cstore.side_scope("w1", "stack") == "w1+stack"
    assert cstore.side_scope("w1+other", "stack") == "w1+stack"
    assert cstore.main_scope("w1+stack~d2") == "w1" == cstore.main_scope("w1")
    assert cstore.side_of("w1+stack~d2") == "stack" and cstore.side_of("w1~d1") is None
    assert cstore.normalize_scope("w1+stack") == "w1+stack"
    for bad in ("", "9x", "a b", "a+b"):
        with pytest.raises(cstore.StoreError):
            cstore.side_scope("w1", bad)


def test_side_definition_is_found_in_the_layers(tmp_path):
    assert runtime.load_definition("side-def", _cwd(tmp_path)).name == "side-def"


# -- starting -------------------------------------------------------------------


def test_start_side_needs_a_live_main_that_declares_it(tmp_path):
    with pytest.raises(runtime.FlowError, match="no cflow2 run in slot"):
        _start_side(tmp_path)
    _start_main(tmp_path)
    with pytest.raises(runtime.FlowError, match="declares no side run 'nope'"):
        runtime.start_side("nope", cwd=_cwd(tmp_path), main="w1", now=T0)
    with pytest.raises(runtime.FlowError, match="runs side-def"):
        _start_side(tmp_path, workflow="other")
    with pytest.raises(runtime.FlowError, match="not a session's main run"):
        runtime.start_side("s", cwd=_cwd(tmp_path), main="w1+s", now=T0)
    st = _start_side(tmp_path)
    assert st["scope"] == "w1+s" and st["driver"] == "w1"
    assert st["side"] == {"name": "s", "main": "w1",
                          "main_run": _status(tmp_path, "w1")["run"]}
    main = _status(tmp_path, "w1")
    assert main["sides"]["s"]["live"] is True and main["sides"]["s"]["declared"] == "side-def"
    with pytest.raises(runtime.FlowError, match="still"):
        _start_side(tmp_path)


def test_a_side_slot_is_not_started_directly(tmp_path):
    chart = frontend.compile_text(textwrap.dedent(SIDE))
    with pytest.raises(runtime.FlowError, match="side run's slot"):
        runtime.start(chart, cwd=_cwd(tmp_path), scope="w1+s", now=T0)


def test_start_side_refused_beside_a_finished_main(tmp_path):
    _start_main(tmp_path)
    runtime.abort("x", cwd=_cwd(tmp_path), scope="w1", now=T0)  # aborted: not live
    with pytest.raises(runtime.FlowError, match="side runs start only beside a live main"):
        _start_side(tmp_path)


def test_main_reports_an_unstarted_side(tmp_path):
    st = _start_main(tmp_path)
    assert st["sides"]["s"]["live"] is False and "flow_start" in st["sides"]["s"]["how"]
    st = _report(tmp_path, "w1", "begin")
    # side('s').live was false: the cond skipped ask-cut; the wait reads a
    # side run that is not there and ends absent
    assert st["status"] == "done" and st["outcome"] == "absent"


# -- signals between the runs ---------------------------------------------------


def test_cut_handshake_between_main_and_side(tmp_path):
    main_text = MAIN.replace("{if: \"side('s').live\", then: [ask-cut]}",
                             "{if: \"true\", then: [ask-cut]}")
    _start_main(tmp_path, main_text)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    # the side may not cut before the main asked for it
    avail = _avail(tmp_path, "w1+s")
    assert avail == {"cut": False, "frozen": False, "seal": True}
    _report(tmp_path, "w1", "ask-cut")
    avail = _avail(tmp_path, "w1+s")
    assert avail["cut"] is True
    _select(tmp_path, "w1+s", "cut")
    _report(tmp_path, "w1+s", "cut", {"tip": "abc"})
    # the side's push poked the main: its wait ended in the same call
    main = _status(tmp_path, "w1")
    assert [t["label"] for t in main["tasks"]] == ["merge"]
    s = RunStore(_cwd(tmp_path), "w1").load_state()
    assert s["cursors"]["side:s:signals#cut"] == 1
    assert s["cursor_epochs"]["side:s:signals#cut"] == \
        RunStore(_cwd(tmp_path), "w1+s").load_state()["collections"]["signals"]["epoch"]
    side_state = RunStore(_cwd(tmp_path), "w1+s").load_state()
    assert side_state["collections"]["signals"]["items"][0]["tip"] == "abc"
    assert side_state["cursors"]["main:signals#cut-wanted"] == 1
    # cut-wanted is consumed: the side cannot cut again
    avail = _avail(tmp_path, "w1+s")
    assert avail["cut"] is False


def test_named_consume_leaves_other_names_fresh(tmp_path):
    main_text = MAIN.replace("{if: \"side('s').live\", then: [ask-cut]}",
                             "{if: \"true\", then: [ask-cut, frozen]}")
    _start_main(tmp_path, main_text)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    _report(tmp_path, "w1", "ask-cut")
    _report(tmp_path, "w1", "frozen")
    _select(tmp_path, "w1+s", "frozen")
    _report(tmp_path, "w1+s", "note-frozen")
    avail = _avail(tmp_path, "w1+s")
    # frozen consumed by name; cut-wanted (pushed before it) is still fresh
    assert avail["frozen"] is False and avail["cut"] is True


def test_a_run_writes_only_its_own_collections(tmp_path):
    bad = MAIN.replace("push: {to: signals, item: {name: \"'frozen'\"}}",
                       "push: {to: 'side:s:signals', item: {name: \"'frozen'\"}}")
    chart = frontend.compile_text(textwrap.dedent(bad))
    assert "peer-write" in [f.code for f in check.errors(chart)]
    r = types.SimpleNamespace(store=RunStore("/x", "w1"))
    with pytest.raises(Exception, match="writes only its own"):
        runtime._self_name(r, "side:s:signals")


def test_peer_cursor_is_void_on_a_log_another_run_opened(tmp_path):
    main_text = MAIN.replace("{if: \"side('s').live\", then: [ask-cut]}",
                             "{if: \"true\", then: [ask-cut]}")
    _start_main(tmp_path, main_text)
    _start_side(tmp_path)
    cwd = _cwd(tmp_path)
    r = runtime.Run.load(RunStore(cwd, "w1"), T0)
    res = runtime._ColResolver(r, strict=False)
    peer_cs = RunStore(cwd, "w1+s").load_state()["collections"]["signals"]
    peer_cs["items"] = [{"seq": 1, "name": "cut"}]
    peer_cs["seq"] = 1
    r.state["cursors"] = {"side:s:signals": 1}
    r.state["cursor_epochs"] = {"side:s:signals": peer_cs["epoch"]}
    assert runtime._log_position(r, "side:s:signals", peer_cs, False, "cut") == 1
    r.state["cursor_epochs"] = {"side:s:signals": "another-run"}
    assert runtime._log_position(r, "side:s:signals", peer_cs, False, "cut") == 0
    assert res.exists("side:s:signals") and not res.exists("side:s:nope")
    assert not res.exists("side:zz:signals")


def test_strict_read_of_an_undeclared_peer_collection_is_an_error(tmp_path):
    _start_main(tmp_path)
    _start_side(tmp_path)
    r = runtime.Run.load(RunStore(_cwd(tmp_path), "w1"), T0)
    with pytest.raises(expr.ExprError, match="declares no collection 'nope'"):
        expr.evaluate("fresh('side:s:nope')", r.env(None))
    # a side run that is not there reads as empty in a guard
    assert expr.evaluate("count('side:other:signals') == 0", r.env(None)) is True
    assert expr.evaluate("side('other').live", r.env(None)) is False
    assert expr.evaluate("side('s').status", r.env(None)) == "running"


def test_until_holds_over_a_finished_peer(tmp_path):
    """A peer that finished still holds its log: a wait whose expression is
    true over it ends ok (absent only when it does not hold)."""
    main_text = MAIN.replace("{if: \"side('s').live\", then: [ask-cut]}",
                             "{if: \"true\", then: [ask-cut]}")
    _start_main(tmp_path, main_text)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    _report(tmp_path, "w1", "ask-cut")
    _select(tmp_path, "w1+s", "cut")
    # finish the side before the main's wait is ticked: cut it and seal
    cwd = _cwd(tmp_path)
    st = _status(tmp_path, "w1+s")
    runtime.report(_task(st, "cut"), "ok", {"tip": "t"}, cwd=cwd, scope="w1+s", now=T0)
    assert [t["label"] for t in _status(tmp_path, "w1")["tasks"]] == ["merge"]


def test_side_runs_inherit_their_log(tmp_path):
    _start_main(tmp_path)
    _start_side(tmp_path)
    cwd = _cwd(tmp_path)
    first = RunStore(cwd, "w1+s").load_state()["collections"]["signals"]["epoch"]
    runtime.abort("again", cwd=cwd, scope="w1+s", now=T0)
    _start_side(tmp_path)
    second = RunStore(cwd, "w1+s").load_state()
    assert second["collections"]["signals"]["epoch"] == first
    assert second["run_id"] != first


# -- ending with the session -----------------------------------------------------


def test_abort_sides(tmp_path):
    _start_main(tmp_path)
    _start_side(tmp_path)
    cwd = _cwd(tmp_path)
    assert runtime.abort_sides("w1", cwd, "session w1 ended", now=T0) == ["w1+s"]
    side = RunStore(cwd, "w1+s").load_state()
    assert side["status"] == "aborted" and side["reason"] == "session w1 ended"
    assert RunStore(cwd, "w1").load_state()["status"] == "running"
    assert runtime.abort_sides("w1", cwd, "again", now=T0) == []


def test_clock_ends_side_runs_of_an_exited_session(tmp_path):
    from claude_launcher.daemon import cflow2_clock

    _start_main(tmp_path)
    _start_side(tmp_path)
    cwd = _cwd(tmp_path)
    live = {}

    class Mgr:
        def get(self, name):
            if name not in live:
                raise KeyError(name)
            return live[name]

        def list(self):
            return list(live.values())

    clock = cflow2_clock.Cflow2Clock(Mgr(), heartbeat=False, threaded=False,
                                     probe_runner=None)
    sess = types.SimpleNamespace(sdef=types.SimpleNamespace(name="w1", cwd=cwd),
                                 paused_at=None, exited=True)
    paused = types.SimpleNamespace(sdef=types.SimpleNamespace(name="w1", cwd=cwd),
                                   paused_at=1.0, exited=True)
    clock.session_exited(paused)
    assert clock.end_sides(T0) == []
    # back already (respawn): the sides go on
    live["w1"] = types.SimpleNamespace(exited=False, sdef=sess.sdef)
    clock.session_exited(sess)
    assert clock.end_sides(T0) == []
    assert RunStore(cwd, "w1+s").load_state()["status"] == "running"
    del live["w1"]
    clock.session_exited(sess)
    assert clock.end_sides(T0) == [(os.path.realpath(cwd), "w1+s")]
    assert RunStore(cwd, "w1+s").load_state()["status"] == "aborted"


# -- MCP ------------------------------------------------------------------------------


def test_mcp_side_argument(tmp_path, monkeypatch):
    cwd = _cwd(tmp_path)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLAUNCH_SESSION", "w1")
    out = cflow2_mcp.call_tool("flow_start", {"workflow": "m"})
    assert out["scope"] == "w1"
    out = cflow2_mcp.call_tool("flow_start", {"side": "s"})
    assert out["scope"] == "w1+s" and out["driver"] == "w1"
    st = cflow2_mcp.call_tool("flow_status", {"side": "s"})
    assert st["scope"] == "w1+s" and [t["label"] for t in st["tasks"]] == ["open"]
    out = cflow2_mcp.call_tool("flow_report", {"side": "s", "node": "open"})
    assert out["choices"]
    with pytest.raises(runtime.FlowError, match="not both"):
        cflow2_mcp.call_tool("flow_status", {"side": "s", "scope": "w1"})
    view = cflow2_mcp.call_tool("flow_collection", {"action": "list", "side": "s"})
    assert "signals" in view["collections"]
    # another session drives neither
    monkeypatch.setenv("CLAUNCH_SESSION", "w2")
    with pytest.raises(runtime.FlowError, match="driven by w1"):
        cflow2_mcp.call_tool("flow_status", {"scope": "w1+s"})
    with pytest.raises(runtime.FlowError):
        cflow2_mcp.call_tool("flow_start", {"side": "s"})


# -- max_visits: 0 -----------------------------------------------------------------


def test_max_visits_zero_is_unlimited_with_a_warning(tmp_path):
    text = """
cflow: 2
name: v
max_visits: 0
nodes: {a: {prompt: a}}
body:
  loop: {while: "true", max: 400, do: a}
"""
    chart = frontend.compile_text(text)
    assert chart.meta["max_visits"] == 0
    assert [f.code for f in check.check(chart) if f.level == "warning"] == ["unbounded-visits"]
    cwd = _cwd(tmp_path)
    runtime.start(chart, cwd=cwd, scope="v", caps={}, now=T0)
    for _ in range(250):
        st = runtime.status(cwd=cwd, scope="v", now=T0, tick=False)
        runtime.report(st["tasks"][0]["instance"], "ok", cwd=cwd, scope="v", now=T0)
    st = runtime.status(cwd=cwd, scope="v", now=T0, tick=False)
    assert st["status"] == "running" and st["tasks"] and not st.get("visit_limits")


# -- expressions and checks ------------------------------------------------------------


def test_side_function_parses_with_a_path():
    class R:
        def side(self, name):
            return {"live": name == "stack", "status": "running"}

    env = {expr.COLLECTIONS_KEY: R()}
    assert expr.evaluate("side('stack').live", env) is True
    assert expr.evaluate("side('x')['live'] or side('stack').status == 'running'", env) is True
    assert expr.collection_calls("side('stack').live") == [("side", "stack")]
    with pytest.raises(expr.ExprError, match="side run name"):
        expr.evaluate("side(3).live", env)


def test_unknown_side_is_a_check_error():
    text = MAIN.replace("side('s').live", "side('t').live").replace(
        "fresh('side:s:signals', 'cut')", "fresh('side:u:signals', 'cut')")
    chart = frontend.compile_text(textwrap.dedent(text))
    found = [f for f in check.errors(chart) if f.code == "unknown-side"]
    # side('t') and the until's 'side:u:'; the consume names the declared 's'
    assert len(found) == 2, found


def test_bad_side_name_is_a_compile_error():
    with pytest.raises(frontend.FrontendError, match="bad side name"):
        frontend.compile_text("cflow: 2\nname: x\nsides: {'a+b': y}\nnodes: {a: {prompt: a}}\n"
                              "body: a\n")


def test_named_consume_compiles():
    chart = frontend.compile_text(textwrap.dedent(SIDE))
    cut = next(n for n in chart.nodes.values() if n.label == "cut")
    assert cut.get("do")[0] == {"op": "consume", "addr": "main:signals", "name": "cut-wanted",
                                "at": "leave", "when": None}
    with pytest.raises(frontend.FrontendError, match="needs a name string"):
        frontend.compile_text("cflow: 2\nname: x\nnodes: {a: {prompt: a}}\nbody:\n"
                              "  wait: {until: 'true', consume: [{from: s}]}\n")


# -- the packaged definitions ------------------------------------------------------------


def test_stack_v2_and_worker_wiring():
    stack = runtime.load_definition("stack-v2")
    assert stack.meta["max_visits"] == 0
    assert set(stack.meta["collections"]) == {"landing", "signals"}
    assert stack.meta["collections"]["landing"]["env"] == {"TARGET": "{session}-stack"}
    assert stack.meta["collections"]["landing"]["lifecycle"]["reset_at"] == ["cut"]
    assert [f.code for f in check.check(stack)] == ["unbounded-visits"]
    worker = runtime.load_definition("improv-worker-v2")
    assert worker.meta["sides"] == {"stack": "stack-v2"}
    assert "signals" in worker.meta["collections"]
    assert not check.errors(worker)
    wait = next(n for n in worker.nodes.values() if n.label == "cut-wait"
                or n.id.endswith("cut-wait"))
    assert wait.get("until") == "fresh('side:stack:signals', 'cut')"
    assert wait.get("consume") == [{"addr": "side:stack:signals", "name": "cut"}]
    assert wait.get("probe") is None


def test_stacked_pr_wiring():
    # claunch-yq80p.2 stage B: a child's PR base is the parent's remote
    # <session>-stack; the stack run keeps that branch and merges PR rows
    stack = runtime.load_definition("stack-v2")
    inputs = stack.meta["inputs"]
    assert {"remote", "repo"} <= set(inputs)
    assert not inputs["remote"].get("required") and not inputs["repo"].get("required")
    tasks = {n.label: n for n in stack.nodes.values() if n.get("prompt")}
    for name in ("open", "land", "cut", "restack"):
        assert "git.push" in tasks[name].get("effects"), name
    assert "--match-head-commit" in tasks["land"].get("prompt")
    assert "--ff-only" in tasks["land"].get("prompt")
    assert "${data.inputs.remote}" in tasks["open"].get("prompt")
    assert "--force-with-lease=<세션>-stack:<옛 tip>" in tasks["restack"].get("prompt")
    # the landing queue carries the PR fields the land task reads
    item = stack.meta["collections"]["landing"]["item"]
    assert {"pr_url", "pr_number", "base"} <= set(item)

    worker = runtime.load_definition("improv-worker-v2")
    wtasks = {n.label: n for n in worker.nodes.values() if n.get("prompt")}
    assert "--base <기준 브랜치>" in wtasks["remote-setup"].get("prompt")
    assert "remote: <원격>, repo: <저장소>" in wtasks["work"].get("prompt")
    assert "Stacked on <부모 세션>-stack" in wtasks["pr-open"].get("prompt")


ROUNDS = """
cflow: 2
name: m
sides: {s: side-def}
collections:
  signals: {kind: log, item: {name: str, tip: "str?"}, lifecycle: {inherit: true}}
nodes:
  ask-cut:
    prompt: A
    do:
      # a cut that arrived after the last round stopped waiting answers that
      # round's request: drop it before asking again (claunch-yq80p.2 X1)
      - {consume: {from: "side:s:signals", name: cut}, when: "$outcome == 'ok' and side('s').live"}
      - push: {to: signals, item: {name: "'cut-wanted'"}}
        when: "$outcome == 'ok'"
  merge: {prompt: M}
  begin: {prompt: B}
body:
  - begin
  - id: rounds
    loop:
      while: "true"
      max: 5
      do:
        - ask-cut
        - id: cut-wait
          wait:
            until: "fresh('side:s:signals', 'cut')"
            consume: [{from: "side:s:signals", name: cut}]
            seconds: 60
          on:
            timeout: []
        - merge
"""


def test_a_late_cut_does_not_answer_the_next_rounds_request(tmp_path):
    # review X1: round 1 stops waiting (timeout -> a person confirmed), the
    # side's cut for round 1 lands afterwards; round 2 must wait for its own
    _start_main(tmp_path, ROUNDS)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    _report(tmp_path, "w1", "ask-cut")
    st = runtime.status(cwd=_cwd(tmp_path), scope="w1", now=T0 + 120, tick=True)
    assert [t["label"] for t in st["tasks"]] == ["merge"]
    # the late cut for round 1
    _select(tmp_path, "w1+s", "cut")
    _report(tmp_path, "w1+s", "cut", {"tip": "r1"})
    _report(tmp_path, "w1", "merge")
    _report(tmp_path, "w1", "ask-cut")
    # round 2 is waiting; the round-1 cut did not satisfy it
    main = _status(tmp_path, "w1")
    assert main["tasks"] == [] and "cut-wait" in str(main.get("waits") or main)
    assert _avail(tmp_path, "w1+s")["cut"] is True
    _select(tmp_path, "w1+s", "cut")
    _report(tmp_path, "w1+s", "cut", {"tip": "r2"})
    assert [t["label"] for t in _status(tmp_path, "w1")["tasks"]] == ["merge"]


def test_clock_reads_a_side_slot_as_its_main_session():
    # review Y6: notices and session lookups for ``w1+s`` (and a nested run
    # under it) go to the session w1 that drives the slot
    from claude_launcher.daemon import cflow2_clock

    got = []
    w1 = types.SimpleNamespace(exited=False, queue_delivery=got.append)

    class Mgr:
        def get(self, name):
            if name != "w1":
                raise KeyError(name)
            return w1

        def list(self):
            return [w1]

    clock = cflow2_clock.Cflow2Clock(Mgr(), heartbeat=False, threaded=False,
                                     probe_runner=None)
    assert clock._live("w1+s") is w1 and clock._live("w1+s~d2") is w1
    assert clock._live("w2+s") is None
    clock._deliver("/x", "w1+s", "an ask for the side run")
    assert got == ["an ask for the side run"]


def test_poke_does_not_wait_for_a_busy_peer(tmp_path):
    # review Y7: a peer whose lock is held is left to the clock
    import time

    main_text = MAIN.replace("{if: \"side('s').live\", then: [ask-cut]}",
                             "{if: \"true\", then: [ask-cut]}")
    _start_main(tmp_path, main_text)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    _report(tmp_path, "w1", "ask-cut")
    _select(tmp_path, "w1+s", "cut")
    with RunStore(_cwd(tmp_path), "w1").lock():
        t = time.monotonic()
        _report(tmp_path, "w1+s", "cut", {"tip": "abc"})
        assert time.monotonic() - t < 2.0
    # the main still waits; its next tick sees the cut
    st = runtime.status(cwd=_cwd(tmp_path), scope="w1", now=T0, tick=True)
    assert [t["label"] for t in st["tasks"]] == ["merge"]


REBASE_MAIN = """
cflow: 2
name: m
sides: {s: side-rb}
collections:
  signals: {kind: log, item: {name: str, tip: "str?", from: "str?"}, lifecycle: {inherit: true}}
nodes:
  begin: {prompt: B}
  rebase:
    prompt: R
    returns: {ok: {tip: str, old_tip: "str?"}}
    do:
      - push: {to: signals, item: {name: "'rebased'", tip: "$result.rebase.value.tip",
                                   from: "$result.rebase.value.old_tip"}}
        when: >-
          $outcome == 'ok' and side('s').live and $result.rebase.value.old_tip != null
          and $result.rebase.value.old_tip != $result.rebase.value.tip
body:
  - begin
  - id: rounds
    loop:
      while: "true"
      max: 5
      do: [rebase]
"""

REBASE_SIDE = """
cflow: 2
name: side-rb
max_visits: 0
nodes:
  open: {prompt: O}
  rebase-notice:
    prompt: N
    do:
      - {consume: {from: "main:signals", name: rebased}}
body:
  - open
  - id: loop
    loop:
      while: "true"
      max: 50
      do:
        id: standby
        choose:
          options:
            rebase-notice:
              when: "fresh('main:signals', 'rebased')"
              then: rebase-notice
            seal:
              then: {break: loop}
"""


def test_a_rewritten_round_branch_reaches_the_stack_run(tmp_path):
    # claunch-yq80p.9: the main's rebase reports old_tip; the engine signals
    # 'rebased' only when it differs from the new tip
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    (v2 / "side-rb.yaml").write_text(textwrap.dedent(REBASE_SIDE), encoding="utf-8")
    _start_main(tmp_path, REBASE_MAIN)
    _start_side(tmp_path)
    _report(tmp_path, "w1", "begin")
    _report(tmp_path, "w1+s", "open")
    # no rewrite (old_tip left out, then equal): nothing for the stack run
    _report(tmp_path, "w1", "rebase", {"tip": "a1"})
    _report(tmp_path, "w1", "rebase", {"tip": "a1", "old_tip": "a1"})
    assert _avail(tmp_path, "w1+s")["rebase-notice"] is False
    _report(tmp_path, "w1", "rebase", {"tip": "b2", "old_tip": "a1"})
    assert _avail(tmp_path, "w1+s")["rebase-notice"] is True
    sig = RunStore(_cwd(tmp_path), "w1").load_state()["collections"]["signals"]["items"]
    assert [(i["name"], i["tip"], i["from"]) for i in sig] == [("rebased", "b2", "a1")]
    _select(tmp_path, "w1+s", "rebase-notice")
    _report(tmp_path, "w1+s", "rebase-notice")
    assert _avail(tmp_path, "w1+s")["rebase-notice"] is False


def test_relation_wiring():
    # claunch-yq80p.9: the worker declares relation (default stacked); its
    # landing request goes to parent: or leader: by that input
    worker = runtime.load_definition("improv-worker-v2")
    rel = worker.meta["inputs"]["relation"]
    assert rel["choices"] == ["stacked", "independent"] and rel["default"] == "stacked"
    req = next(n for n in worker.nodes.values() if n.label == "integration-request")
    pushes = [(t.get("when"), t["push"]["to"]) for t in req.get("triggers") if t.get("push")]
    assert pushes == [("$data.inputs.relation != 'independent'", "parent:landing"),
                      ("$data.inputs.relation == 'independent'", "leader:landing")]
    rebase = next(n for n in worker.nodes.values() if n.label == "rebase" and n.get("prompt"))
    assert [d.get("item", {}).get("name") for d in rebase.get("do")] == ["'rebased'"]
    stack = runtime.load_definition("stack-v2")
    choose = next(n for n in stack.nodes.values() if n.id.endswith("standby"))
    guards = {o["name"]: o.get("guard") for o in choose.get("options")}
    assert guards["rebase-notice"] == ("fresh('main:signals', 'rebased') and "
                                       "not fresh('main:signals', 'landed')")
    assert "not fresh('main:signals', 'rebased')" in guards["cut"]
    restack = next(n for n in stack.nodes.values() if n.label == "restack" and n.get("prompt"))
    assert [d.get("name") for d in restack.get("do") if d.get("op") == "consume"] == \
        ["landed", "rebased"]


STACK_MAIN = """
cflow: 2
name: m
sides: {stack: stack-v2}
collections:
  signals: {kind: log, item: {name: str, tip: "str?", from: "str?"}, lifecycle: {inherit: true}}
nodes:
  begin: {prompt: B}
  rb:
    prompt: R
    returns: {ok: {tip: str, old_tip: "str?"}}
    do:
      - push: {to: signals, item: {name: "'rebased'", tip: "$result.rb.value.tip",
                                   from: "$result.rb.value.old_tip"}}
        when: "$outcome == 'ok' and side('stack').live and $result.rb.value.old_tip != null"
  ld:
    prompt: L
    do:
      - push: {to: signals, item: {name: "'landed'"}}
  cw:
    prompt: C
    do:
      - push: {to: signals, item: {name: "'cut-wanted'"}}
  fin: {prompt: F}
body: [begin, rb, ld, cw, fin]
"""


def _stack_rep(tmp_path, label, outcome="ok", value=None, scope="w1+stack"):
    st = _status(tmp_path, scope)
    return runtime.report(_task(st, label), outcome, value, cwd=_cwd(tmp_path),
                          scope=scope, now=T0)


def _based(tmp_path):
    items = RunStore(_cwd(tmp_path), "w1+stack").load_state()["collections"]["signals"]["items"]
    return [(i["name"], i.get("tip")) for i in items]


def test_stack_base_point_and_a_resolved_restack_conflict(tmp_path):
    # claunch-yq80p.9 review B1-B3, on the packaged stack-v2: every move
    # records the stack's base point; a person's 'resolved' records it too
    # and clears the pre-landing rebased signal, so cut is not blocked
    _start_main(tmp_path, STACK_MAIN)
    runtime.start_side("stack", cwd=_cwd(tmp_path), main="w1", caps={}, now=T0,
                       inputs={"issue": "x-1", "base": "b0"})
    _report(tmp_path, "w1", "begin")
    _stack_rep(tmp_path, "open", value={"branch": "w1-stack", "worktree": "/w", "tip": "b0",
                                       "children": []})
    assert _based(tmp_path) == [("based", "b0")]
    # the round branch was rewritten: the stack moves onto the new round tip
    _report(tmp_path, "w1", "rb", {"tip": "t1", "old_tip": "t0"})
    assert _avail(tmp_path, "w1+stack")["rebase-notice"] is True
    _select(tmp_path, "w1+stack", "rebase-notice", "r")
    _stack_rep(tmp_path, "rebase-notice", value={"old_tip": "s0", "tip": "s1", "told": [],
                                                "onto": "t1"})
    assert _based(tmp_path)[-1] == ("based", "t1")
    # rewritten again before landing (left fresh), then the round lands
    _report(tmp_path, "w1", "ld")
    st = _status(tmp_path, "w1")
    assert [t["label"] for t in st["tasks"]] == ["cw"]
    # a second rebased that the side has not handled
    runtime.collection_op("push", "signals", cwd=_cwd(tmp_path), scope="w1",
                          principal="driver", session="w1",
                          item={"name": "rebased", "tip": "t2", "from": "t1"})
    avail = _avail(tmp_path, "w1+stack")
    assert avail["restack"] is True and avail["rebase-notice"] is False
    _select(tmp_path, "w1+stack", "restack", "r")
    _stack_rep(tmp_path, "restack", "conflict", {"at": "remote moved: abc"})
    _stack_rep(tmp_path, "ask-frame", value={"brief": {"problem": "p", "options": [
        {"name": "resolved", "summary": "s", "risk": "r", "outcome": "o",
         "reversibility": "reversible"},
        {"name": "skip", "summary": "s", "risk": "r", "outcome": "o",
         "reversibility": "reversible"}], "recommendation": "resolved", "why": "w"}})
    runtime.approve(None, decision="resolved", reason="fixed by hand",
                    cwd=_cwd(tmp_path), scope="w1+stack", now=T0)
    _stack_rep(tmp_path, "stack-based", value={"onto": "m9"})
    assert _based(tmp_path)[-1] == ("based", "m9")
    _report(tmp_path, "w1", "cw")
    avail = _avail(tmp_path, "w1+stack")
    assert avail["cut"] is True and avail["rebase-notice"] is False
