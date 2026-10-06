"""cflow v2 frontend: surface YAML -> statechart IR (docs/cflow-v2.md 1-3)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from claude_launcher.cflow2 import check, ir, layers
from claude_launcher.cflow2.frontend import FrontendError, compile_file, compile_text

TASKS = """
nodes:
  a: {title: A, prompt: do a}
  b: {title: B, prompt: do b}
  c: {title: C, prompt: do c}
"""


def C(body: str, extra: str = "", *, resolve=None, source: str = "t.yaml") -> ir.Chart:
    extra = textwrap.dedent(extra)
    tasks = "" if extra.startswith("nodes:") or "\nnodes:" in extra else TASKS
    text = "cflow: 2\nname: t\n" + extra + tasks + textwrap.dedent(body)
    return compile_text(text, source=source, resolve_include=resolve or (lambda name: None))


def err(body: str, extra: str = "", **kw) -> str:
    with pytest.raises(FrontendError) as exc:
        C(body, extra, **kw)
    return str(exc.value)


# -- header and keys ---------------------------------------------------------


def test_requires_cflow_2():
    with pytest.raises(FrontendError, match="cflow: 2"):
        compile_text("name: x\nbody: end\n", resolve_include=lambda n: None)
    with pytest.raises(FrontendError, match="cflow: 2"):
        compile_text("cflow: 1\nname: x\nbody: end\n", resolve_include=lambda n: None)


def test_unknown_top_key_has_line_and_allowed_list():
    msg = err("body: a\nbodyy: 3\n")
    assert "t.yaml:" in msg and "bodyy" in msg and "allowed:" in msg
    line = int(msg.split("t.yaml:")[1].split(":")[0])
    text = "cflow: 2\nname: t\n" + TASKS + "body: a\nbodyy: 3\n"
    assert text.splitlines()[line - 1].startswith("bodyy")


def test_unknown_task_key_and_combinator_key():
    with pytest.raises(FrontendError, match=r"t.yaml:\d+: unknown key 'promt' in task 'x'"):
        compile_text("cflow: 2\nname: t\nnodes:\n  x: {promt: hi}\nbody: x\n",
                     source="t.yaml", resolve_include=lambda n: None)
    assert "unknown key 'max'" in err("body:\n  par: {branches: {x: a}, max: 2}\n")
    assert "unknown key 'foo'" in err("body:\n  seq: [a]\n  foo: 1\n")


def test_meta_and_top_level_fields():
    chart = C("body: a\n", """
        description: d
        default_role: worker
        filter_roles: {type: whitelist, roles: [worker]}
        priority: 5
        params: {goal: {type: str, required: true}}
        returns: {ok: {summary: str}}
        data: {count: 0}
    """)
    assert chart.meta == {"default_role": "worker",
                          "filter_roles": {"type": "whitelist", "roles": ["worker"]},
                          "default_child_cflow": None, "priority": 5}
    assert chart.params == {"goal": {"type": "str", "required": True}}
    assert chart.returns == {"ok": {"summary": "str"}}
    assert chart.data == {"count": 0}
    assert chart.description == "d"
    assert type(chart.data) is dict


def test_on_and_yes_no_stay_strings():
    chart = C("body:\n  seq: [a]\n  on: {no: b}\n")
    assert list(chart.node("body").on) == ["no"]


def test_kind_validation():
    assert "kind" in err("body: a\n", "kind: thing\n")


# -- tasks -------------------------------------------------------------------


def test_task_fields_and_defaults():
    chart = compile_text(textwrap.dedent("""
        cflow: 2
        name: t
        nodes:
          work:
            title: W
            prompt: hello ${data.x}
            done_when: done
            verify: "pytest -q"
            executor: [headless, inline]
            needs: [headless.run]
            returns: {ok: {files: list, note: "str?"}, fail: {reason: str}}
            effects: [fs.write]
            paths: ["src/**"]
            budget: {seconds: 60, usd: 1.5}
            strict_budget: true
            ask: {prompt: "go?", from: [{role: leader}]}
          bare: {prompt: p}
        body: [work, bare]
    """), source="t.yaml", resolve_include=lambda n: None)
    w = chart.node("work")
    assert w.type == ir.T_TASK and w.label == "work" and w.parent == "body"
    assert w.executor == ["headless", "inline"] and w.needs == ["headless.run"]
    assert w.budget == {"seconds": 60, "usd": 1.5}
    assert w.get("prompt") == "hello ${data.x}"  # value refs untouched
    assert w.get("returns") == {"ok": {"files": "list", "note": "str?"}, "fail": {"reason": "str"}}
    assert w.get("ask")["prompt"] == "go?"
    assert w.get("text_id") == ir.text_id("W", "hello ${data.x}", "done")
    assert w.src.startswith("t.yaml:")
    b = chart.node("bare")
    assert b.get("title") == "bare" and b.executor == [] and b.get("returns") == {"ok": {}}


def test_task_validation_errors():
    assert "unknown type" in err("body: x\n", "nodes:\n  x: {prompt: p, returns: {ok: {f: strr}}}\n")
    assert "unknown executor" in err("body: x\n", "nodes:\n  x: {prompt: p, executor: robot}\n")
    assert "ask needs a prompt" in err("body: x\n", "nodes:\n  x: {prompt: p, ask: {from: []}}\n")
    assert "unknown task 'zz'" in err("body: zz\n")


def test_fragments_expand_nested_and_text_id_ignores_values():
    chart = C("body: x\n", """
        fragments:
          inner: "INNER"
          outer: "outer ${frag:inner}"
        nodes:
          x: {title: X, prompt: "start ${frag:outer} ${params.goal}"}
    """)
    x = chart.node("x")
    assert x.get("prompt") == "start outer INNER ${params.goal}"
    assert x.get("text_id") == ir.text_id("X", "start outer INNER ${params.goal}", "")


def test_fragment_cycle_and_unknown():
    msg = err("body: x\n", """
        fragments:
          f1: "${frag:f2}"
          f2: "${frag:f1}"
        nodes:
          x: {prompt: "${frag:f1}"}
    """)
    assert "fragment cycle" in msg
    assert "unknown fragment 'nope'" in err("body: x\n", "nodes:\n  x: {prompt: '${frag:nope}'}\n")


def test_rules_delivery_and_own_rules():
    chart = C("body: [a, b, c]\n", """
        rules:
          r-all: {text: ALL, deliver: all}
          r-b: {text: ONLY B, why: because, deliver: [b], effect: {deny: git.push}}
          r-own: {text: OWN}
    """)
    assert chart.rules["r-b"] == {"text": "ONLY B", "why": "because", "deliver": ["b"],
                                  "effect": {"deny": "git.push"}}
    a, b = chart.node("a"), chart.node("b")
    assert a.get("rules") == ["r-all"]
    assert a.get("prompt").endswith("do a\n\n── 규칙 ──\nALL\n")
    assert b.get("rules") == ["r-all", "r-b"]
    assert "ONLY B" in b.get("prompt") and "because" not in b.get("prompt")
    own = compile_text(textwrap.dedent("""
        cflow: 2
        name: t
        rules:
          r-own: {text: OWN}
          r-all: {text: ALL, deliver: all}
        nodes:
          x: {prompt: p, rules: [r-own, missing]}
        body: x
    """), resolve_include=lambda n: None).node("x")
    assert own.get("rules") == ["r-own", "missing", "r-all"]
    assert own.get("prompt") == "p\n\n── 규칙 ──\nOWN\nALL\n"


# -- combinators -------------------------------------------------------------


def test_seq_ids_deterministic_and_repeated_tasks():
    text = "body:\n  seq: [a, {seq: [b, a]}, a]\n"
    c1, c2 = C(text), C(text)
    assert c1.to_dict() == c2.to_dict()
    root = c1.node(c1.root)
    assert root.id == "body" and root.type == ir.T_SEQ and root.get("pass_prev") is False
    assert root.get("children") == ["a", "body.1", "a#3"]
    assert c1.node("body.1").get("children") == ["b", "a#2"]
    assert c1.node("a#2").parent == "body.1" and c1.node("a#2").label == "a"


def test_list_body_is_seq_and_waterfall():
    chart = C("body: [a, {waterfall: [b, c]}]\n")
    assert chart.node("body").type == ir.T_SEQ
    wf = chart.node("body.1")
    assert wf.type == ir.T_SEQ and wf.get("pass_prev") is True and wf.get("children") == ["b", "c"]


def test_cond():
    chart = C("""
body:
  cond:
    - {if: "$data.n > 1", then: a}
    - {if: "$data.n == 1", then: [b]}
    - {else: c}
""")
    node = chart.node("body")
    assert node.type == ir.T_COND
    assert [br["guard"] for br in node.get("branches")] == ["$data.n > 1", "$data.n == 1", None]
    assert [br["child"] for br in node.get("branches")] == ["a", "body.if1", "c"]
    assert "bad expression" in err("body:\n  cond:\n    - {if: '$data.n >', then: a}\n")
    assert "else must be the last" in err("body:\n  cond:\n    - {else: a}\n    - {if: 'true', then: b}\n")


def test_choose():
    chart = C("""
body:
  id: pick
  choose:
    prompt: which?
    chooser: user
    require_reason: true
    options:
      left: {description: L, then: a}
      right: {description: R, then: [b, c], when: "$data.ok"}
""")
    node = chart.node("pick")
    assert node.type == ir.T_CHOOSE and node.label == "pick"
    assert node.get("chooser") == "user" and node.get("require_reason") is True
    assert node.get("options") == [
        {"name": "left", "description": "L", "guard": None, "child": "a"},
        {"name": "right", "description": "R", "guard": "$data.ok", "child": "pick.right"},
    ]
    assert "chooser" in err("body:\n  choose: {chooser: bot, options: {x: {then: a}}}\n")


def test_foreach_loop_retry():
    chart = C("""
body:
  - foreach: {in: "$data.items", as: it, do: a, par: true}
  - loop: {while: "$data.n < 3", max: 5, do: b}
  - loop: {until: "$data.done", max: 2, do: b, between: c, every: 10}
  - retry: {do: a, max: 3, every: 5, until: "$prev.ok", between: c, carry: resume}
  - foreach: {in: "$data.items", do: c, max: 7}
""")
    fe = chart.node("body.0")
    assert fe.type == ir.T_LOOP and fe.get("mode") == "foreach"
    assert (fe.get("items"), fe.get("as_name"), fe.get("parallel"), fe.get("max")) == ("$data.items", "it", True, 1000)
    wl = chart.node("body.1")
    assert (wl.get("mode"), wl.get("guard"), wl.get("max"), wl.get("child")) == ("while", "$data.n < 3", 5, "b")
    ul = chart.node("body.2")
    assert (ul.get("mode"), ul.get("between"), ul.get("every")) == ("until", "c", 10)
    rt = chart.node("body.3")
    assert (rt.get("mode"), rt.get("guard"), rt.get("max"), rt.get("carry"), rt.get("every")) == (
        "retry", "$prev.ok", 3, "resume", 5)
    assert chart.node("body.4").get("max") == 7 and chart.node("body.4").get("as_name") == "item"
    assert "loop needs max" in err("body:\n  loop: {while: 'true', do: a}\n")
    assert "retry needs max" in err("body:\n  retry: {do: a}\n")
    assert "exactly one of while / until" in err("body:\n  loop: {max: 2, do: a}\n")


def test_par_settle():
    chart = C("body:\n  par: {branches: {x: a, y: [b, c]}, settle: 'quorum:1'}\n")
    node = chart.node("body")
    assert node.type == ir.T_PAR and node.get("branches") == {"x": "a", "y": "body.y"}
    assert node.get("settle") == "quorum:1"
    assert C("body:\n  par: {branches: {x: a}}\n").node("body").get("settle") == "all"
    assert "settle" in err("body:\n  par: {branches: {x: a}, settle: most}\n")


def test_timeout_try_wait():
    chart = C("""
body:
  - timeout: {do: a, seconds: 30, else: b}
  - try: [a, {raise: oops}]
    catch: {oops: b, fail: c}
    finally: c
  - wait: {probe: "git rev-parse master", poll: 30, describe: master moves}
  - wait: {seconds: 5}
""")
    to = chart.node("body.0")
    assert to.type == ir.T_TIMEOUT and to.get("seconds") == 30 and to.get("else_child") == "b"
    tr = chart.node("body.1")
    assert tr.type == ir.T_TRY and tr.get("child") == "body.1.try"
    assert tr.get("catch") == {"oops": "b#2", "fail": "c"} and tr.get("finally_child") == "c#2"
    raise_node = chart.node("body.1.try.1")
    assert raise_node.get("op") == "raise" and raise_node.get("outcome") == "oops"
    w = chart.node("body.2")
    assert w.type == ir.T_WAIT and w.get("probe") == "git rev-parse master" and w.get("poll") == 30
    assert chart.node("body.3").get("seconds") == 5
    assert "wait needs probe or seconds" in err("body:\n  wait: {describe: x}\n")


def test_common_keys_and_on():
    chart = C("""
body:
  seq: [a, b]
  id: main
  executor: subagent
  needs: [subagent.spawn]
  budget: {seconds: 100}
  bind: out
  on:
    fail: end
    blocked: [c, {return: blocked, value: {why: "$prev.reason", n: 3}}]
""")
    node = chart.node("main")
    assert node.executor == ["subagent"] and node.needs == ["subagent.spawn"]
    assert node.budget == {"seconds": 100} and node.bind == "out"
    assert node.on == {"fail": "main.on.fail", "blocked": "main.on.blocked"}
    assert chart.node("main.on.fail").get("op") == "end"
    ret = chart.node("main.on.blocked.1")
    assert ret.get("op") == "return" and ret.get("outcome") == "blocked"
    assert ret.get("value") == {"why": {"expr": "$prev.reason"}, "n": {"lit": 3}}
    assert chart.delegation_points() == [node]


def test_duplicate_author_id():
    assert "duplicate id 'x'" in err("body:\n  - {id: x, seq: [a]}\n  - {id: x, seq: [b]}\n")


# -- control -----------------------------------------------------------------


def test_continue_break_targets():
    chart = C("""
body:
  loop:
    while: "true"
    max: 3
    do:
      - a
      - {break: null}
      - loop:
          until: "false"
          max: 2
          do: [b, {continue: outer}, {break: inner}]
        id: inner
  id: outer
""")
    assert chart.node("outer.do.1").get("target") == "outer"
    assert chart.node("inner.do.1").get("target") == "outer"
    assert chart.node("inner.do.2").get("target") == "inner"
    assert "continue outside any loop" in err("body: [a, {continue: null}]\n")
    assert "not an enclosing loop" in err(
        "body:\n  loop: {while: 'true', max: 2, do: [a, {break: elsewhere}]}\n")


def test_raise_needs_catching_try():
    assert "no enclosing try catches it" in err("body: [a, {raise: oops}]\n")
    assert "no enclosing try" in err("body:\n  try: [a, {raise: oops}]\n  catch: {other: b}\n")


def test_end_and_return_controls():
    chart = C("body: [a, end]\n")
    assert chart.node("body.1").type == ir.T_CONTROL and chart.node("body.1").get("op") == "end"
    assert "unknown key 'with'" in err("body: [{return: ok, with: {}}]\n")


# -- call / procedures / include ---------------------------------------------

PROCS = """
procedures:
  check:
    params: {who: {type: str, required: true}, n: {type: int, default: 1}}
    returns: {ok: {}}
    nodes:
      inner: {title: I, prompt: inner}
    body: [inner, a]
"""


def test_call_inlines_with_prefixed_ids():
    chart = C("body:\n  - call: check\n    with: {who: \"$params.me\", n: 2}\n    bind: res\n"
              "  - {id: again, call: check, with: {who: x}}\n", PROCS)
    scope = chart.node("body.0")
    assert scope.type == ir.T_SCOPE and scope.get("proc") == "check" and scope.bind == "res"
    assert scope.get("with_") == {"who": {"expr": "$params.me"}, "n": {"lit": 2}}
    body = chart.node(scope.get("child"))
    assert body.id == "body.0.body" and body.get("children") == ["body.0/inner", "body.0/a"]
    assert chart.node("again.body").get("children") == ["again/inner", "again/a"]
    assert chart.node("again/inner").label == "inner"


def test_call_param_validation():
    assert "missing required parameter 'who'" in err("body:\n  call: check\n", PROCS)
    assert "unknown parameter 'zz'" in err("body:\n  call: check\n  with: {who: x, zz: 1}\n", PROCS)
    assert "unknown procedure 'nope'" in err("body:\n  call: nope\n")


def test_recursion_is_an_error():
    msg = err("body:\n  call: p1\n", """
        procedures:
          p1: {body: [a, {call: p2}]}
          p2: {body: [b, {call: p1}]}
    """)
    assert "recursive call: p1 -> p2 -> p1" in msg
    assert "recursive call: me -> me" in err("body:\n  call: me\n", """
        procedures:
          me: {body: {call: me}}
    """)


def test_raise_in_procedure_warns_and_caught_by_caller():
    extra = """
        procedures:
          p: {body: [a, {raise: oops}]}
    """
    chart = C("body:\n  call: p\n", extra)
    assert any("raise 'oops'" in w for w in chart.warnings)
    chart = C("body:\n  try: {call: p}\n  catch: {oops: b}\n", extra)
    assert chart.warnings == []


def test_loop_targets_do_not_cross_call():
    extra = """
        procedures:
          p: {body: [a, {break: null}]}
    """
    assert "break outside any loop" in err(
        "body:\n  loop: {while: 'true', max: 2, do: {call: p}}\n", extra)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_include_merges_and_conflicts(tmp_path):
    _write(tmp_path / "common.yaml", """
        cflow: 2
        name: common
        fragments: {hello: "HELLO"}
        rules: {shared: {text: SHARED, deliver: all}}
        nodes:
          ptask: {prompt: "proc ${frag:hello}"}
        procedures:
          greet: {body: ptask}
    """)
    resolve = lambda n: tmp_path / f"{n}.yaml" if (tmp_path / f"{n}.yaml").is_file() else None
    chart = C("body: [x, {call: greet}]\n",
              "include: [common]\nnodes:\n  x: {prompt: '${frag:hello}'}\n",
              resolve=resolve)
    assert chart.node("x").get("prompt").startswith("HELLO")
    assert chart.node("body.1/ptask").get("prompt").startswith("proc HELLO")
    assert chart.node("body.1/ptask").src.startswith("common.yaml:")
    assert "shared" in chart.rules
    msg = err("body: a\n", "include: [common]\nfragments: {hello: other}\n", resolve=resolve)
    assert "already defined" in msg
    assert "no v2 definition" in err("body: a\n", "include: [missing]\n", resolve=resolve)


def test_include_cycle(tmp_path):
    _write(tmp_path / "x1.yaml", "cflow: 2\nname: x1\ninclude: [x2]\n")
    _write(tmp_path / "x2.yaml", "cflow: 2\nname: x2\ninclude: [x1]\n")
    resolve = lambda n: tmp_path / f"{n}.yaml"
    with pytest.raises(FrontendError, match="include cycle"):
        C("body: a\n", "include: [x1]\n", resolve=resolve)


def test_procedure_file_via_resolver(tmp_path):
    _write(tmp_path / "land.yaml", """
        cflow: 2
        name: land
        kind: procedure
        params: {branch: {type: str}}
        nodes:
          merge-it: {prompt: merge, effects: ["git.merge:feature"]}
        body: merge-it
    """)
    resolve = lambda n: tmp_path / f"{n}.yaml" if (tmp_path / f"{n}.yaml").is_file() else None
    chart = C("body:\n  call: land\n  with: {branch: b1}\n", resolve=resolve)
    assert chart.node("body/merge-it").get("effects") == ["git.merge:feature"]
    # a workflow file is not callable
    _write(tmp_path / "wf.yaml", "cflow: 2\nname: wf\nnodes: {z: {prompt: z}}\nbody: z\n")
    assert "not a procedure" in err("body:\n  call: wf\n", resolve=resolve)


def test_group():
    chart = C("body:\n  id: g\n  group: {run: check, do: a, limit: 2}\n", PROCS)
    g = chart.node("g")
    assert g.type == ir.T_GROUP and g.get("name") == "g" and g.get("limit") == 2
    assert g.get("settle") == "allSettled" and g.get("child") == "a"
    member = chart.node(g.get("member"))
    assert member.type == ir.T_SCOPE and member.id == "g.member" and member.get("proc") == "check"
    assert chart.node("g.member/inner").parent == "g.member.body"


# -- spec ----------------------------------------------------------------------


def test_spec_shape():
    chart = C("body: a\n", """
        spec:
          invariants: {nm: {never_effect: git.merge, target: master}}
          paths: {p: {to: end, must_pass: [a]}}
          ensures: {a.ok: "$result.a.value != null"}
          monitors:
            m1: {always: "$probe.x == 1", probe: {x: "echo 1"}, every: 30, on_violation: halt}
            m2: {when: {left: a}, within: 60, expect: {entered: [b]}}
    """)
    assert chart.spec["paths"]["p"] == {"to": "end", "must_pass": ["a"]}
    assert "unknown key 'whatever'" in err("body: a\n", "spec: {whatever: 1}\n")
    assert "must_pass" in err("body: a\n", "spec: {paths: {p: {to: end}}}\n")
    assert "bad expression" in err("body: a\n", "spec: {ensures: {a.ok: '== =='}}\n")
    assert "on_violation" in err("body: a\n", "spec: {monitors: {m: {always: 'true', on_violation: boom}}}\n")


# -- round trip, file entry, layers -------------------------------------------


def test_json_round_trip():
    chart = C("""
body:
  - try: {par: {branches: {x: a, y: b}}}
    catch: {fail: c}
  - {choose: {options: {o: {then: a}}}}
""")
    again = ir.Chart.from_json(chart.to_json())
    assert again == chart
    assert again.to_dict() == chart.to_dict()


def test_compile_file_uses_sibling_include(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "defs" / "frag.yaml", "cflow: 2\nname: frag\nfragments: {f: FFF}\n")
    path = _write(tmp_path / "defs" / "main.yaml", """
        cflow: 2
        name: main
        include: [frag]
        nodes: {x: {prompt: "${frag:f}"}}
        body: x
    """)
    chart = compile_file(path)
    assert chart.node("x").get("prompt") == "FFF" and chart.source == str(path)
    assert chart.node("x").src == "main.yaml:5"


def test_layers_priority_listing_and_errors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    from claude_launcher import config
    monkeypatch.setattr(config, "launcher_home", lambda: home)
    project = tmp_path / "proj"
    good = "cflow: 2\nname: {n}\ndescription: {d}\nnodes: {{x: {{prompt: p}}}}\nbody: x\n"
    _write(home / "workflows" / "v2" / "dup.yaml", good.format(n="dup", d="global"))
    mine = _write(project / ".claunch" / "workflows" / "v2" / "dup.yaml", good.format(n="dup", d="project"))
    _write(project / ".claunch" / "workflows" / "v2" / "broken.yaml", "name: broken\nbody: x\n")
    assert [layer for layer, _ in layers.search_dirs(str(project))] == ["project", "global", "package"]
    listed = {d.name: d for d in layers.list_definitions(str(project))}
    assert listed["dup"].path == mine and listed["dup"].layer == "project"
    assert listed["dup"].shadows == (home / "workflows" / "v2" / "dup.yaml",)
    assert listed["dup"].description == "project" and listed["dup"].error is None
    assert "cflow: 2" in (listed["broken"].error or "")
    assert "sample-v2" in listed and listed["sample-v2"].layer == "package"
    assert layers.find("dup", str(project)) == mine
    assert layers.find("nothing", str(project)) is None
    assert layers.load("dup", str(project)).description == "project"
    with pytest.raises(FrontendError, match="no v2 definition"):
        layers.load("nothing", str(project))


def test_sample_v2_compiles_without_check_errors():
    chart = compile_file(layers.package_dir() / "sample-v2.yaml",
                         resolve_include=lambda n: None)
    assert chart.name == "sample-v2"
    types = {n.type for n in chart.walk()}
    assert {"seq", "choose", "loop", "try", "par", "scope", "task"} <= types
    assert check.errors(chart) == []
    assert ir.Chart.from_json(chart.to_json()) == chart
