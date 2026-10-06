"""cflow v2 static checks (docs/cflow-v2.md section 8)."""

from __future__ import annotations

import textwrap

from claude_launcher.cflow2 import check, ir
from claude_launcher.cflow2.frontend import compile_text


def C(text: str) -> ir.Chart:
    return compile_text("cflow: 2\nname: t\n" + textwrap.dedent(text),
                        source="t.yaml", resolve_include=lambda n: None)


def codes(chart: ir.Chart, level: str = None):
    return [f.code for f in check.check(chart) if level is None or f.level == level]


def findings(chart: ir.Chart, code: str):
    return [f for f in check.check(chart) if f.code == code]


SIMPLE = """
nodes:
  a: {prompt: a}
  b: {prompt: b}
  c: {prompt: c}
"""


def test_clean_chart_has_no_findings():
    chart = C(SIMPLE + "body: [a, b, c]\n")
    assert check.check(chart) == []
    assert check.errors(chart) == []


# -- cfg -----------------------------------------------------------------------


def test_cfg_seq_cond_loop():
    chart = C(SIMPLE + """
body:
  - a
  - cond:
      - {if: "$data.x", then: b}
      - {else: []}
  - loop: {while: "$data.y", max: 3, do: c}
""")
    succ = check.cfg(chart)
    assert succ["START"] == ["a"]
    assert succ["a"] == ["END", "b", "c"]          # an empty else skips; loop may run 0 times
    assert succ["b"] == ["END", "c"]
    assert succ["c"] == ["END", "c"]
    assert succ["END"] == []
    # without an else, no true guard finishes the cond ``fail`` (the run
    # fails): only the guarded branch continues
    no_else = C(SIMPLE + """
body:
  - a
  - cond:
      - {if: "$data.x", then: b}
  - c
""")
    assert check.cfg(no_else)["a"] == ["b"]


def test_cfg_try_catch_finally_and_raise():
    chart = C("""
nodes:
  a: {prompt: a, returns: {ok: {}, blocked: {}}}
  b: {prompt: b}
  c: {prompt: c}
  d: {prompt: d}
body:
  - try: [a, {raise: oops}]
    catch: {blocked: b, oops: c}
    finally: d
""")
    succ = check.cfg(chart)
    # blocked -> catch; ok -> raise oops -> catch; fail/timeout -> the
    # finally d runs, then the run fails
    assert succ["a"] == ["b", "c", "d"]
    assert succ["b"] == ["d"] and succ["c"] == ["d"] and succ["d"] == ["END"]


def test_cfg_par_all_runs_every_branch_and_race_picks_one():
    base = SIMPLE + "body:\n  - par: {branches: {x: a, y: b}, settle: %s}\n  - c\n"
    succ = check.cfg(C(base % "all"))
    assert succ["START"] == ["a"] and succ["a"] == ["b"] and succ["b"] == ["c"]
    succ = check.cfg(C(base % "race"))
    assert succ["START"] == ["a", "b"] and succ["a"] == ["c"]


def test_cfg_break_continue_return_end():
    chart = C(SIMPLE + """
procedures:
  p: {body: [b, {return: ok}, c]}
body:
  - loop:
      while: "true"
      max: 5
      do: [a, {break: null}]
  - call: p
  - end
""")
    succ = check.cfg(chart)
    assert succ["a"] == ["body.1/b"]
    assert succ["body.1/b"] == ["END"]   # return leaves the scope, then `end`
    assert "body.1/c" in succ           # still a node, just unreachable
    assert succ["START"] == ["a", "body.1/b"]


# -- codes ---------------------------------------------------------------------


def test_unbounded_loop():
    chart = C(SIMPLE + "body:\n  loop: {while: 'true', max: 2, do: a}\n")
    assert "unbounded-loop" not in codes(chart)
    chart.node("body").props["max"] = None  # only a hand-made chart can get here
    assert "unbounded-loop" in codes(chart, "error")


def test_never_effect_invariant_targets():
    def chart_with(effect: str, target: str = "master") -> ir.Chart:
        return C(f"""
nodes:
  m: {{prompt: m, effects: ["{effect}"]}}
body: m
spec:
  invariants: {{nm: {{never_effect: git.merge, target: {target}}}}}
""")
    assert "never-effect" in codes(chart_with("git.merge"))
    assert "never-effect" in codes(chart_with("git.merge:master"))
    assert "never-effect" in codes(chart_with("git.merge(master)"))
    assert "never-effect" not in codes(chart_with("git.merge:feature"))
    assert "never-effect" not in codes(chart_with("git.commit"))


def test_never_effect_from_rule():
    chart = C("""
rules:
  no-push: {text: no push, effect: {deny: git.push}}
nodes:
  p: {prompt: p, effects: ["git.push:origin"]}
  q: {prompt: q, effects: [git.commit]}
body: [p, q]
""")
    found = findings(chart, "never-effect")
    assert [f.node for f in found] == ["p"]
    assert "rule no-push" in found[0].message


def test_must_pass_holds_and_counterexample():
    good = C(SIMPLE + """
body: [a, b, c]
spec:
  paths: {p: {to: end, must_pass: [b]}}
""")
    assert findings(good, "must-pass") == []
    bad = C(SIMPLE + """
body:
  - a
  - cond:
      - {if: "$data.skip", then: c}
      - {else: [b, c]}
spec:
  paths: {p: {to: end, must_pass: [b]}}
""")
    found = findings(bad, "must-pass")
    assert len(found) == 1 and found[0].level == "error"
    assert "START -> a -> c -> END" in found[0].message


def test_must_pass_to_a_task_and_unknown_labels():
    chart = C(SIMPLE + """
body:
  - choose:
      options:
        x: {then: [a, c]}
        y: {then: [b, c]}
spec:
  paths:
    p: {to: c, must_pass: [a]}
    q: {to: nowhere, must_pass: [a]}
    r: {to: end, must_pass: [ghost]}
""")
    msgs = [f.message for f in findings(chart, "must-pass")]
    assert any("START -> b -> c#2" in m for m in msgs)
    assert any("unknown target 'nowhere'" in m for m in msgs)
    assert any("unknown node 'ghost'" in m for m in msgs)


def test_no_end():
    looping = C(SIMPLE + """
body:
  retry:
    max: 3
    do: [a, {continue: null}]
""")
    assert "no-end" in codes(looping, "error")
    assert "no-end" not in codes(C(SIMPLE + "body: a\n"))


def test_par_write_overlap():
    def chart_with(pa: str, pb: str) -> ir.Chart:
        return C(f"""
nodes:
  x: {{prompt: x, effects: [fs.write], {pa}}}
  y: {{prompt: y, effects: [fs.write], {pb}}}
  r: {{prompt: r, effects: [fs.read]}}
body:
  par: {{branches: {{one: x, two: y, three: r}}}}
""")
    disjoint = chart_with('paths: ["src/a/**"]', 'paths: ["src/b/**"]')
    assert findings(disjoint, "par-write-overlap") == []
    overlap = chart_with('paths: ["src/**"]', 'paths: ["src/b/x.py"]')
    found = findings(overlap, "par-write-overlap")
    assert len(found) == 1 and found[0].level == "warning" and "one and two" in found[0].message
    undeclared = chart_with('paths: ["src/a/**"]', "needs: []")
    assert "without declared paths" in findings(undeclared, "par-write-overlap")[0].message
    read_only = C("""
nodes:
  x: {prompt: x, effects: [fs.read]}
  y: {prompt: y, effects: [test.run]}
body:
  par: {branches: {one: x, two: y}}
""")
    assert findings(read_only, "par-write-overlap") == []


def test_group_member_writes_warn():
    chart = C("""
nodes:
  lead: {prompt: l}
  w: {prompt: w, effects: [fs.write]}
procedures:
  member: {body: w}
body:
  group: {run: member, do: lead}
""")
    assert "par-write-overlap" in codes(chart, "warning")


def test_unknown_capability():
    good = C("nodes:\n  a: {prompt: a, needs: [headless.run]}\nbody: a\n")
    assert "unknown-capability" not in codes(good)
    bad = C("nodes:\n  a: {prompt: a, needs: [headless.teleport]}\nbody: a\n")
    assert "unknown-capability" in codes(bad, "error")


def test_unknown_rule():
    good = C("rules: {r: {text: R}}\nnodes:\n  a: {prompt: a, rules: [r]}\nbody: a\n")
    assert "unknown-rule" not in codes(good)
    bad = C("nodes:\n  a: {prompt: a, rules: [gone]}\nbody: a\n")
    assert findings(bad, "unknown-rule")[0].node == "a"


def test_delegated_ask():
    task = "nodes:\n  a: {prompt: a, ask: {prompt: ok}%s}\n"
    assert "delegated-ask" not in codes(C(task % "" + "body: a\n"))
    assert "delegated-ask" not in codes(C(task % ", executor: inline" + "body: a\n"))
    assert "delegated-ask" in codes(C(task % ", executor: [headless, inline]" + "body: a\n"))
    under = C(task % "" + "body:\n  seq: [a]\n  executor: subagent\n")
    assert "delegated-ask" in codes(under, "error")


def test_split_effect():
    assert check.split_effect("git.merge") == ("git.merge", None)
    assert check.split_effect("git.merge:master") == ("git.merge", "master")
    assert check.split_effect("git.merge(master)") == ("git.merge", "master")


# -- B3: non-ok outcomes reach ancestor ``on`` handlers and ``*`` catches -------


def test_must_pass_sees_a_failure_routed_by_an_ancestor_on_handler():
    chart = C(SIMPLE + """
body:
  seq: [a, b, c]
  on: {fail: end}
spec:
  paths: {p: {to: end, must_pass: [b]}}
""")
    found = findings(chart, "must-pass")
    assert len(found) == 1 and "START -> a -> END" in found[0].message


def test_must_pass_sees_a_wildcard_catch():
    chart = C(SIMPLE + """
body:
  - try: [a, b]
    catch: {"*": end}
  - c
spec:
  paths: {p: {to: end, must_pass: [b]}}
""")
    assert "START -> a -> END" in findings(chart, "must-pass")[0].message
    succ = check.cfg(chart)
    assert "END" in succ["a"]


def test_ask_outcomes_are_possible_outcomes():
    chart = C("""
nodes:
  g: {prompt: g, ask: {prompt: "go?"}}
  b: {prompt: b}
body:
  seq: [g, b]
  on: {declined: end}
spec:
  paths: {p: {to: end, must_pass: [b]}}
""")
    assert "START -> g -> END" in findings(chart, "must-pass")[0].message


# -- B4: signals and failures run every finally they leave ---------------------


def test_end_inside_try_runs_the_finally():
    chart = C(SIMPLE + """
body:
  try: [a, end]
  finally: c
spec:
  paths: {p: {to: end, must_pass: [c]}}
""")
    assert findings(chart, "must-pass") == []
    assert check.cfg(chart)["a"] == ["c"]


def test_finally_copy_does_not_leak_the_signal_to_normal_paths():
    # the end path runs c then ends; the normal path runs c then b. A shared
    # finally would also let the normal path end right after c (skipping b).
    chart = C(SIMPLE + """
body:
  - try:
      cond:
        - {if: "$data.stop", then: [a, end]}
        - {else: []}
    finally: c
  - b
spec:
  paths: {p: {to: end, must_pass: [b, c]}}
""")
    msgs = [f.message for f in findings(chart, "must-pass")]
    # the only path skipping b is the real one through a
    assert len(msgs) == 1 and "START -> a -> c -> END" in msgs[0]


def test_return_break_continue_run_the_finally():
    chart = C(SIMPLE + """
procedures:
  p:
    body:
      try: [a, {return: ok}]
      finally: b
body:
  - call: p
  - loop:
      while: "true"
      max: 3
      do:
        try: [{break: null}]
        finally: c
spec:
  paths:
    p1: {to: end, must_pass: [b]}
    p2: {to: end, must_pass: [c]}
""")
    succ = check.cfg(chart)
    assert succ["body.0/a"] == ["body.0/b"]        # return runs the finally
    assert not any("p1" in f.message for f in findings(chart, "must-pass"))
    # the loop may run zero times (p2 fails for that reason only), and a
    # break inside it runs c before leaving
    assert "c" in succ["body.0/b"]
    assert [f.message for f in findings(chart, "must-pass")] == [
        "path p2: START -> body.0/a -> body.0/b -> END reaches end without passing c"]


def test_uncaught_failure_runs_nested_finallys_in_order():
    chart = C("""
nodes:
  a: {prompt: a}
  b: {prompt: b}
  c: {prompt: c}
  d: {prompt: d}
body:
  try:
    try: a
    finally: b
  catch: {fail: d}
  finally: c
""")
    succ = check.cfg(chart)
    assert "b" in succ["a"]           # a fails: the inner finally first
    # after the inner finally (in the failing copy) the outer catch runs
    assert "d" in succ["b"] and "c" in succ["d"]


def test_improv_worker_settle_before_end_holds_with_an_end_in_the_round_body():
    import yaml as _yaml
    from claude_launcher.cflow2 import layers
    from claude_launcher.cflow2.frontend import _load_yaml, _plain

    path = layers.find("improv-worker-v2")
    doc = _plain(_load_yaml(path.read_text(encoding="utf-8"), path.name))
    body = doc["body"]["try"]["loop"]["do"]
    body.insert(1, "end")
    text = _yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    chart = compile_text(text, source=str(path), resolve_include=layers.resolver())
    assert any(n.type == "control" and n.get("op") == "end" and n.parent == "rounds.do"
               for n in chart.nodes.values())
    assert [f for f in findings(chart, "must-pass") if "settle-before-end" in f.message] == []
    # and the property is not vacuous: without the finally it fails
    doc["body"] = {"seq": [doc["body"]["try"], doc["body"]["finally"]]}
    chart = compile_text(_yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         source=str(path), resolve_include=layers.resolver())
    assert [f for f in findings(chart, "must-pass") if "settle-before-end" in f.message]


# -- A2: no person's gate inside a delegation -----------------------------------


def test_delegated_user_gate():
    tree = """
nodes:
  a: {prompt: a}
  b: {prompt: b}
body:
  seq:
    - a
    - choose:
        chooser: %s
        options: {x: {then: b}, y: {then: []}}
  executor: %s
"""
    assert "delegated-user-gate" in codes(C(tree % ("user", "subagent")), "error")
    assert "delegated-user-gate" in codes(C(tree % ("user", "[headless, inline]")), "error")
    assert "delegated-user-gate" not in codes(C(tree % ("user", "inline")))
    assert "delegated-user-gate" not in codes(C(tree % ("agent", "subagent")))


# -- C4: effects-undeclared ------------------------------------------------------


def test_effects_undeclared_only_where_effects_are_forbidden():
    base = """
nodes:
  a: {prompt: a}
  b: {prompt: b, effects: []}
  c: {prompt: c, effects: [fs.read]}
body: [a, b, c]
"""
    assert "effects-undeclared" not in codes(C(base))
    inv = C(base + "spec:\n  invariants: {x: {never_effect: git.push}}\n")
    found = findings(inv, "effects-undeclared")
    assert [f.node for f in found] == ["a"] and found[0].level == "warning"
    rule = C(base + "rules:\n  r: {text: R, effect: {deny: git.push}}\n")
    assert [f.node for f in findings(rule, "effects-undeclared")] == ["a"]


def test_bundled_definitions_declare_every_effect():
    from claude_launcher.cflow2 import layers

    for name in ("improv-worker-v2", "improv-common", "sample-v2"):
        assert findings(layers.load(name), "effects-undeclared") == [], name


# -- ir.subtree_is_flat ------------------------------------------------------------


def test_subtree_is_flat():
    chart = C(SIMPLE + """
body:
  - id: flat
    seq: [a, b]
    bind: x
    on: {fail: c}
  - id: nested
    seq: [a, {id: inner, seq: [b], bind: y}]
  - id: ctl
    seq: [a, {cond: [{if: "true", then: b}]}]
  - id: ens
    seq: [c]
spec:
  ensures: {"c#2.ok": "true"}
""")
    assert ir.subtree_is_flat(chart, "flat")      # the root's own on/bind run outside
    assert ir.subtree_is_flat(chart, "a")
    assert not ir.subtree_is_flat(chart, "nested")
    assert not ir.subtree_is_flat(chart, "ctl")
    assert not ir.subtree_is_flat(chart, "ens")
