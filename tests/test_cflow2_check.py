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
  - loop: {while: "$data.y", max: 3, do: c}
""")
    succ = check.cfg(chart)
    assert succ["START"] == ["a"]
    assert succ["a"] == ["END", "b", "c"]          # cond without else may skip; loop may run 0 times
    assert succ["b"] == ["END", "c"]
    assert succ["c"] == ["END", "c"]
    assert succ["END"] == []


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
    assert succ["a"] == ["b", "c"]  # blocked -> catch; ok -> raise oops -> catch
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
