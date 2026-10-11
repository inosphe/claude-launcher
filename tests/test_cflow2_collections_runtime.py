"""cflow v2 collections at run time: state, node effects, triggers, wait
until, external operations, MCP and CLI (docs/cflow-v2.md 2.7, 2.8, 4.5)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from claude_launcher.cflow2 import expr, frontend, mcp as cflow2_mcp, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

COLS = """
collections:
  landing:
    kind: queue
    key: issue
    item: {issue: str, tip: "str?"}
    status:
      values: [requested, waiting, deferred, landed, rejected]
      initial: requested
      settled: [landed, rejected]
    access: {push: [driver, person, daemon], set: [driver, person], set.landed: [daemon],
             remove: [driver, person, daemon]}
    env: {TARGET: master}
    watch:
      every: 60
      probe: [git, merge-base, --is-ancestor, "$CFLOW_ITEM_TIP", "$CFLOW_TARGET"]
      on: {0: {set: {status: landed}}, 3: {remove: true}}
    lifecycle: {reset_at: [finish], drop: settled, remap: {deferred: requested}, inherit: true}
  signals:
    kind: log
    item: {name: str}
    lifecycle: {inherit: true}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def _chart(body: str, cols: str = COLS):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(cols)
                                 + textwrap.dedent(body))


def _start(tmp_path, body, cols=COLS, driver="drv", force=False, now=T0):
    return runtime.start(_chart(body, cols), cwd=str(tmp_path), scope="t", caps={},
                         driver=driver, now=now, force=force)


def _kw(tmp_path, now=T0):
    return {"cwd": str(tmp_path), "scope": "t", "now": now}


def _state(tmp_path):
    return RunStore(str(tmp_path), "t").load_state()


def _events(tmp_path, name):
    return [e for e in RunStore(str(tmp_path), "t").journal() if e["event"] == name]


def _inst(st, label):
    return next(t["instance"] for t in st["tasks"] if t["label"] == label)


TWO = """
nodes:
  a: {prompt: "queue ${col.landing}"}
  b: {prompt: B}
body: {seq: [a, b]}
"""


# 1. state


def test_state_initialised_from_chart(tmp_path):
    st = _start(tmp_path, TWO)
    s = _state(tmp_path)
    # epoch: the run that opened the collection (a peer's cursor, doc 2.9)
    assert s["collections"] == {"landing": {"kind": "queue", "items": [], "seq": 0,
                                            "epoch": s["run_id"]},
                                "signals": {"kind": "log", "items": [], "seq": 0,
                                            "epoch": s["run_id"]}}
    assert s["cursors"] == {} and s["triggers"] == []
    assert st["collections"]["landing"]["count"] == 0
    assert st["pending_triggers"] == 0


def test_inherit_across_two_runs_and_reset_at_finish(tmp_path):
    _start(tmp_path, TWO)
    for issue in ("i1", "i2", "i3"):
        runtime.collection_op("push", "landing", principal="driver", session="drv",
                              item={"issue": issue}, **_kw(tmp_path))
    runtime.collection_op("set", "landing", principal="driver", key="i2",
                          fields={"status": "deferred"}, **_kw(tmp_path))
    runtime.collection_op("set", "landing", principal="daemon", key="i3",
                          fields={"status": "landed"}, **_kw(tmp_path))
    runtime.collection_op("push", "signals", principal="driver", item={"name": "cut"},
                          **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    runtime.report(_inst(st, "b"), **_kw(tmp_path))
    s = _state(tmp_path)
    assert s["status"] == "done"
    # reset at finish: settled dropped, deferred remapped
    assert [(i["issue"], i["status"]) for i in s["collections"]["landing"]["items"]] == \
        [("i1", "requested"), ("i2", "requested")]
    assert _events(tmp_path, "collection_reset")[0]["dropped"] == ["i3"]
    # a normal new start after finish inherits
    st = _start(tmp_path, TWO)
    s = _state(tmp_path)
    assert [i["issue"] for i in s["collections"]["landing"]["items"]] == ["i1", "i2"]
    assert s["collections"]["signals"]["seq"] == 1
    inh = _events(tmp_path, "collection_inherited")
    assert {e["collection"] for e in inh} == {"landing", "signals"}
    # a forced replace inherits too (the aborted run is reset at finish first)
    runtime.collection_op("push", "landing", principal="driver", item={"issue": "i9"},
                          **_kw(tmp_path))
    _start(tmp_path, TWO, force=True)
    s = _state(tmp_path)
    assert [i["issue"] for i in s["collections"]["landing"]["items"]] == ["i1", "i2", "i9"]


def test_other_workflow_does_not_inherit(tmp_path):
    _start(tmp_path, TWO)
    runtime.collection_op("push", "landing", principal="driver", item={"issue": "i1"},
                          **_kw(tmp_path))
    runtime.abort("x", **_kw(tmp_path))
    chart = frontend.compile_text("cflow: 2\nname: other\n" + textwrap.dedent(COLS)
                                  + textwrap.dedent(TWO))
    runtime.start(chart, cwd=str(tmp_path), scope="t", caps={}, driver="drv", now=T0)
    assert _state(tmp_path)["collections"]["landing"]["items"] == []


def test_inherit_carries_cursor(tmp_path):
    body = """
    nodes:
      a: {prompt: A, do: [{consume: signals}]}
    body: {seq: [a]}
    """
    _start(tmp_path, body)
    runtime.collection_op("push", "signals", principal="driver", item={"name": "x"},
                          **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    assert _state(tmp_path)["cursors"] == {"signals": 1}
    _start(tmp_path, body)
    assert _state(tmp_path)["cursors"] == {"signals": 1}


# 2. node effects


def test_do_effects_enter_and_leave_with_outcome(tmp_path):
    body = """
    nodes:
      a:
        prompt: A
        returns: {ok: {}, rework: {}}
        do:
          - push: {to: signals, item: {name: "'entered'"}}
            at: enter
          - push: {to: landing, item: {issue: "$params.id"}}
            when: "$outcome == 'ok'"
          - push: {to: signals, item: {name: "$outcome"}}
    params: {id: {default: "i7"}}
    body: {seq: [a]}
    """
    st = _start(tmp_path, body)
    assert [i["name"] for i in st["collections"]["signals"]["items"]] == ["entered"]
    runtime.report(_inst(st, "a"), "rework", **_kw(tmp_path))
    s = _state(tmp_path)
    assert [i["name"] for i in s["collections"]["signals"]["items"]] == ["entered", "rework"]
    assert s["collections"]["landing"]["items"] == []
    assert _events(tmp_path, "collection_pushed")[0]["by"] == "drv"


def test_failing_effect_fails_the_node(tmp_path):
    body = """
    nodes:
      a:
        prompt: A
        do:
          - push: {to: signals, item: {name: "'one'"}}
          - set: {in: landing, key: "'nope'", fields: {status: "'waiting'"}}
    body: {seq: [a]}
    """
    st = _start(tmp_path, body)
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    s = _state(tmp_path)
    assert s["status"] == "failed"
    assert "no record" in s["value"]["reason"]
    # all or nothing: the first push was rolled back
    assert s["collections"]["signals"]["items"] == []


BAD_SET = "{set: {in: landing, key: \"'nope'\", fields: {status: \"'waiting'\"}}}"


@pytest.mark.parametrize("shape", ["on", "catch"])
def test_failing_leave_effect_goes_to_the_fail_handlers(tmp_path, shape):
    unit = f"{{id: unit, seq: [a], do: [{BAD_SET}]}}"
    if shape == "on":
        body = f"seq:\n    - {{id: unit, seq: [a], do: [{BAD_SET}], on: {{fail: h}}}}\n    - b"
    else:
        body = f"seq:\n    - try: {unit}\n      catch: {{fail: h}}\n    - b"
    st = _start(tmp_path, f"""
nodes:
  a: {{prompt: A}}
  b: {{prompt: B}}
  h: {{prompt: H}}
body:
  {body}
""")
    st = runtime.report(_inst(st, "a"), **_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["h"]
    reason = _state(tmp_path)["result"]["unit"]["value"]["reason"]
    assert "no record" in reason
    # the effect ran once: the handler's end does not apply it again
    st = runtime.report(_inst(st, "h"), **_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["b"]
    assert len([e for e in RunStore(str(tmp_path), "t").journal()
                if e["event"] == "left" and e["node"] == "unit"]) == 1


def test_driver_effect_refused_by_access(tmp_path):
    body = """
    nodes:
      a:
        prompt: A
        do:
          - push: {to: landing, item: {issue: "'i1'"}}
            at: enter
          - set: {in: landing, key: "'i1'", fields: {status: "'landed'"}}
    body: {seq: [a]}
    """
    st = _start(tmp_path, body)
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    s = _state(tmp_path)
    assert s["outcome"] == "fail" and "access denied" in s["value"]["reason"]


def test_reset_at_node_leave(tmp_path):
    cols = COLS.replace("reset_at: [finish]", "reset_at: [a]")
    _start(tmp_path, TWO, cols=cols)
    runtime.collection_op("push", "landing", principal="driver", item={"issue": "i1"},
                          **_kw(tmp_path))
    runtime.collection_op("set", "landing", principal="daemon", key="i1",
                          fields={"status": "landed"}, **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    assert _state(tmp_path)["collections"]["landing"]["items"] == []
    assert _events(tmp_path, "collection_reset")[0]["at"] == "a"


# 3. triggers


def test_triggers_recorded_and_claimed_once(tmp_path):
    body = """
    nodes:
      a:
        prompt: A
        triggers:
          - briefing
          - {do: checks, at: leave}
          - at: leave
            push: {to: "parent:landing", each: "$facts.in_review",
                   item: {issue: "$each", tip: "$facts.head"}}
    body: {loop: {max: 3, until: "$visits.a >= 2", do: a}}
    """
    st = _start(tmp_path, body)
    assert st["pending_triggers"] == 1
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    claimed = runtime.claim_triggers(str(tmp_path), "t")
    assert [(t["do"], t["at"]) for t in claimed] == [
        ("briefing", "enter"), ("checks", "leave"), ("push", "leave"),
        ("briefing", "enter"), ("checks", "leave"), ("push", "leave")]
    assert len({t["id"] for t in claimed}) == 6
    assert claimed[0]["node"] == "a" and claimed[0]["instance"].startswith("a@")
    assert claimed[2]["push"]["to"] == "parent:landing"
    assert runtime.claim_triggers(str(tmp_path), "t") == []
    assert len(_events(tmp_path, "trigger_queued")) == 6
    assert runtime.complete_trigger(str(tmp_path), "t", claimed[0]["id"], True, "ok")
    assert runtime.complete_trigger(str(tmp_path), "t", claimed[1]["id"], False, "no checks")
    assert _events(tmp_path, "trigger_done")[0]["trigger"] == claimed[0]["id"]
    assert _events(tmp_path, "trigger_skipped")[0]["reason"] == "no checks"


# 4. wait until


UNTIL = """
nodes:
  b: {prompt: B}
body:
  seq:
    - wait: {until: "fresh('signals', 'cut') and count('landing', 'requested') >= 1",
             seconds: 100, consume: [signals]}
    - b
"""


def test_wait_until_ok_on_mutation_and_consumes(tmp_path):
    st = _start(tmp_path, UNTIL)
    assert st["waits"]
    runtime.collection_op("push", "signals", principal="driver", item={"name": "cut"},
                          **_kw(tmp_path))
    st = runtime.collection_op("push", "landing", principal="driver", item={"issue": "i"},
                               **_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["b"]
    s = _state(tmp_path)
    assert s["cursors"] == {"signals": 1}
    assert _events(tmp_path, "cursor_moved")[0]["after"] == 1


def test_wait_until_timeout(tmp_path):
    _start(tmp_path, """
    nodes:
      b: {prompt: B}
    body:
      try:
        wait: {until: "fresh('signals')", seconds: 100}
      catch: {timeout: b}
    """)
    st = runtime.status(**_kw(tmp_path, T0 + 50))
    assert st["waits"]
    st = runtime.status(**_kw(tmp_path, T0 + 101))
    assert [t["label"] for t in st["tasks"]] == ["b"]


def test_wait_until_absent_for_other_run(tmp_path):
    chart = _chart("""
    nodes:
      b: {prompt: B}
    body:
      try:
        wait: {until: "count('parent:landing') > 0", seconds: 100}
      catch: {absent: b}
    """)
    # the checker refuses another run's address; the runtime still reads it
    # as absent if it gets there
    runtime.start(chart, cwd=str(tmp_path), scope="t", caps={}, driver="drv", now=T0)
    st = runtime.status(**_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["b"]


# 5. expressions


class _Res:
    def items(self, addr, status=None):
        recs = [{"n": 1, "status": "a"}, {"n": 2, "status": "b"}]
        return [r for r in recs if status is None or r["status"] == status]

    def fresh(self, addr, name=None):
        return name in (None, "cut")

    def exists(self, addr):
        return addr == "x"


def test_expr_collection_functions():
    env = {expr.COLLECTIONS_KEY: _Res()}
    assert expr.evaluate("count('x')", env) == 2
    assert expr.evaluate("count('x', 'b')", env) == 1
    assert len(expr.evaluate("items('x', 'a')", env)) == 1
    assert expr.evaluate("fresh('x', 'cut') and not fresh('x', 'other')", env) is True
    assert expr.evaluate("exists('x') and not exists('y')", env) is True
    assert expr.evaluate("$__collections__", env) is None
    with pytest.raises(expr.ExprError):
        expr.evaluate("count('x')", {})
    with pytest.raises(expr.ExprError):
        expr.parse("exists('x', 'y')")
    with pytest.raises(expr.ExprError):
        expr.evaluate("count(3)", env)
    assert expr.collection_calls("count('a') > 0 and fresh($data.x)") == [
        ("count", "a"), ("fresh", None)]
    assert expr.references("items($data.addr)") == {"data"}


def test_guard_and_prompt_read_collections(tmp_path):
    body = """
    nodes:
      a: {prompt: "queue ${col.landing}"}
      b: {prompt: B}
      c: {prompt: C}
    body:
      seq:
        - a
        - cond:
            - if: "count('landing') == 1 and exists('landing')"
              then: b
            - else: c
    """
    _start(tmp_path, body)
    runtime.collection_op("push", "landing", principal="driver", item={"issue": "i1"},
                          **_kw(tmp_path))
    st = runtime.status(**_kw(tmp_path))
    assert '"issue": "i1"' in st["tasks"][0]["prompt"]
    st = runtime.report(_inst(st, "a"), **_kw(tmp_path))
    assert [t["label"] for t in st["tasks"]] == ["b"]


# 6. external operations


def test_collection_op_access_and_view(tmp_path):
    _start(tmp_path, TWO)
    st = runtime.collection_op("push", "landing", principal="person", session="",
                               item={"issue": "i1", "tip": "abc"}, **_kw(tmp_path))
    assert st["applied"]["key"] == "i1" and st["applied"]["by"] == "person"
    with pytest.raises(runtime.FlowError, match="access denied"):
        runtime.collection_op("set", "landing", principal="driver", key="i1",
                              fields={"status": "landed"}, **_kw(tmp_path))
    with pytest.raises(runtime.FlowError, match="unknown field"):
        runtime.collection_op("push", "landing", principal="driver",
                              item={"issue": "i2", "x": 1}, **_kw(tmp_path))
    view = runtime.collection_view(str(tmp_path), "t")
    assert view["collections"]["landing"]["statuses"] == {"requested": 1}
    assert view["pending_triggers"] == 0


def test_watch_due_and_apply(tmp_path):
    _start(tmp_path, TWO)
    for issue in ("i1", "i2"):
        runtime.collection_op("push", "landing", principal="driver",
                              item={"issue": issue, "tip": "t-" + issue}, **_kw(tmp_path))
    due = runtime.watch_due(str(tmp_path), "t", T0)
    assert [d["key"] for d in due] == ["i1", "i2"]
    d = due[0]
    assert d["env"]["CFLOW_ITEM_TIP"] == "t-i1" and d["env"]["CFLOW_TARGET"] == "master"
    assert d["env"]["CFLOW_SESSION"] == "drv" and d["timeout"] == 30
    assert d["probe"][0] == "git"
    assert runtime.watch_due(str(tmp_path), "t", T0 + 30) == []  # every 60
    out = runtime.watch_apply(str(tmp_path), "t", "landing", "i1", 0, now=T0 + 31)
    assert out["applied"] and out["fields"] == {"status": "landed"}
    assert runtime.watch_apply(str(tmp_path), "t", "landing", "i2", 1)["applied"] is False
    assert runtime.watch_apply(str(tmp_path), "t", "landing", "i2", 3)["applied"] is True
    # settled and removed records are no longer probed
    assert runtime.watch_due(str(tmp_path), "t", T0 + 120) == []
    w = _events(tmp_path, "collection_watch")
    assert [(e["key"], e["code"]) for e in w] == [("i1", 0), ("i2", 3)]


def test_watch_env_names_and_session(tmp_path):
    cols = COLS.replace("env: {TARGET: master}", "env: {TARGET: master, base: \"{session}-x\"}")
    _start(tmp_path, TWO, cols=cols)
    runtime.collection_op("push", "landing", principal="driver", item={"issue": "i1"},
                          **_kw(tmp_path))
    env = runtime.watch_due(str(tmp_path), "t", T0)[0]["env"]
    assert env["CFLOW_BASE"] == "drv-x" and env["CFLOW_TARGET"] == "master"
    assert env["CFLOW_ITEM_ISSUE"] == "i1" and "CFLOW_ITEM_SEQ" not in env


def test_holders(tmp_path):
    _start(tmp_path, TWO, driver="lead")
    assert runtime.holders("landing", "lead", [str(tmp_path)]) == \
        [(str(tmp_path.resolve()), "t")]
    assert runtime.holders("landing", "other", [str(tmp_path)]) == []
    assert runtime.holders("nope", "lead", [str(tmp_path)]) == []
    runtime.abort("x", **_kw(tmp_path))
    assert runtime.holders("landing", "lead", [str(tmp_path)]) == []


NESTED = """
nodes:
  a: {prompt: A}
  ship: {prompt: ship}
  hold: {prompt: hold}
body:
  seq:
    - id: unit
      executor: [subagent, inline]
      do:
        - push: {to: signals, item: {name: "'in'"}}
          at: enter
        - push: {to: signals, item: {name: "'out'"}}
      triggers: [briefing]
      seq:
        - a
        - choose: {options: {go: {then: ship}, wait: {then: hold}}}
"""


def test_nested_delegation_run_holds_no_collections(tmp_path):
    from claude_launcher.cflow2 import executors

    executors.register(None)
    st = runtime.start(_chart(NESTED), cwd=str(tmp_path), scope="t", driver="lead", now=T0,
                       caps={"subagent.spawn": True, "mcp.claunch": True})
    (dl,) = st["delegations"]
    nested = dl["nested"]
    ns = RunStore(nested["cwd"], nested["scope"]).load_state()
    assert "collections" not in ns and not ns.get("triggers")
    # only the delegating run holds 'landing' for its driver
    assert runtime.holders("landing", "lead", [str(tmp_path)]) == \
        [(str(tmp_path.resolve()), "t")]
    kw = {"cwd": nested["cwd"], "scope": nested["scope"], "now": T0}
    runtime.report("a", **kw)
    runtime.select(None, "go", **kw)
    runtime.report("ship", **kw)
    runtime.status(**_kw(tmp_path))
    # the root's do: and triggers ran once, in the delegating run
    s = _state(tmp_path)
    assert [i["name"] for i in s["collections"]["signals"]["items"]] == ["in", "out"]
    assert [t["do"] for t in s["triggers"]] == ["briefing"]


def test_acting_session_of_a_nested_run(tmp_path):
    parent = RunStore(str(tmp_path), "p")
    parent.write_chart(_chart(TWO))
    parent.save_state({"delegations": {"d1": {"executor": "session", "assignee": None,
                                              "child": "kid", "status": "running"}}})
    link = {"cwd": str(tmp_path), "scope": "p", "delegation": "d1"}
    assert runtime.acting_session({"driver": "", "parent": link}) == "kid"
    assert runtime.acting_session({"driver": "drv", "parent": link}) == "drv"
    assert runtime.acting_session({"driver": ""}) == ""
    assert runtime.acting_session({"driver": "", "parent": {**link, "delegation": "x"}}) == ""


# 7. MCP and CLI


def test_mcp_flow_collection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "t")
    runtime.start(_chart(TWO), cwd=str(tmp_path), scope="t", caps={}, driver="t")
    out = cflow2_mcp.call_tool("flow_collection", {"action": "push", "collection": "landing",
                                                   "item": {"issue": "i1"}})
    assert out["applied"]["principal"] == "driver"
    with pytest.raises(runtime.FlowError, match="access denied"):
        cflow2_mcp.call_tool("flow_collection", {"action": "set", "collection": "landing",
                                                 "key": "i1", "fields": {"status": "landed"}})
    out = cflow2_mcp.call_tool("flow_collection", {"action": "list", "collection": "landing"})
    assert list(out["collections"]) == ["landing"]
    assert out["collections"]["landing"]["items"][0]["status"] == "requested"
    # only the driver
    monkeypatch.setenv("CLAUNCH_SESSION", "someone")
    with pytest.raises(runtime.FlowError, match="driven by t"):
        cflow2_mcp.call_tool("flow_collection", {"action": "list", "scope": "t"})
    assert "flow_collection" in {t["name"] for t in cflow2_mcp.TOOLS}


def _cli(cwd, *args, session=None):
    env = dict(os.environ)
    # A subprocess does not inherit pytest's pythonpath setting. Test this
    # checkout, including its storage format, rather than an editable install.
    from pathlib import Path
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("CLAUNCH_SESSION", None)
    if session:
        env["CLAUNCH_SESSION"] = session
    return subprocess.run([sys.executable, "-c", "import sys; from claude_launcher.cli import "
                           "main; sys.exit(main())", "cflow2", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=60)


def test_cli_collection_person_path(tmp_path):
    runtime.start(_chart(TWO), cwd=str(tmp_path), scope="t", caps={}, driver="drv")
    p = _cli(tmp_path, "collection", "push", "landing", '{"issue": "12"}', "--scope", "t")
    assert p.returncode == 0, p.stderr
    p = _cli(tmp_path, "collection", "set", "landing", "12", "status=rejected", "--scope", "t")
    assert p.returncode == 0, p.stderr
    p = _cli(tmp_path, "collection", "ls", "--scope", "t")
    assert p.returncode == 0 and "rejected" in p.stdout
    ev = _events(tmp_path, "collection_set")[0]
    assert ev["principal"] == "person" and ev["key"] == "12"
    # the driving session's own shell is refused
    p = _cli(tmp_path, "collection", "remove", "landing", "12", "--scope", "t", session="drv")
    assert p.returncode == 1 and "person's answer" in p.stderr
    # reading stays open there
    assert _cli(tmp_path, "collection", "ls", "--scope", "t", session="drv").returncode == 0


# PR landing (claunch-yq80p.5): a trigger push reads $data as it was when the
# node changed, and a leave effect reads the node's own result.


def test_trigger_push_carries_the_data_it_reads(tmp_path):
    body = """
    data: {landing: {medium: pr, pr_url: "https://h/o/r/pull/7"}, other: {big: 1}}
    nodes:
      a:
        prompt: A
        triggers:
          - at: leave
            push: {to: "parent:landing", each: "$facts.in_review",
                   item: {issue: "$each", tip: "$facts.head",
                          pr_url: "$data.landing.pr_url"}}
    body: a
    """
    st = _start(tmp_path, body)
    runtime.report(_inst(st, "a"), **_kw(tmp_path))
    (trig,) = runtime.claim_triggers(str(tmp_path), "t")
    # only the keys the expressions read, as they were at the node change
    assert trig["data"] == {"landing": {"medium": "pr", "pr_url": "https://h/o/r/pull/7"}}

    from claude_launcher.daemon import cflow2_triggers
    items = cflow2_triggers.expand_push(trig["push"], {"in_review": ["i1"], "head": "abc"},
                                        trig["data"])
    assert items == [{"issue": "i1", "tip": "abc", "pr_url": "https://h/o/r/pull/7"}]
    # a trigger without $data reads records nothing extra
    assert runtime._push_data_keys({"each": "$facts.in_review", "item": {"x": "$each"}}) == []


def test_a_leave_effect_reads_the_nodes_own_result(tmp_path):
    cols = """
    collections:
      prs:
        kind: queue
        key: number
        item: {number: int, url: str}
        status: {values: [open, merged], initial: open, settled: [merged]}
    """
    body = """
    nodes:
      o:
        prompt: O
        returns: {ok: {number: int, url: str}, failed: {reason: str}}
        do:
          - push: {to: prs, item: {number: "$result['o'].value.number",
                                   url: "$result['o'].value.url"}}
            when: "$outcome == 'ok'"
    body: o
    """
    st = _start(tmp_path, body, cols)
    runtime.report(_inst(st, "o"), value={"number": 7, "url": "https://h/o/r/pull/7"},
                   **_kw(tmp_path))
    items = _state(tmp_path)["collections"]["prs"]["items"]
    assert [(i["number"], i["url"], i["status"]) for i in items] == [
        (7, "https://h/o/r/pull/7", "open")]
