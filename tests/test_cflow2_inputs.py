"""cflow v2 typed start inputs (docs/cflow-v2.md 2.6, cflow2/inputs.py)."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import check, inputs, ir, layers
from claude_launcher.cflow2.frontend import FrontendError, compile_text


def C(text: str) -> ir.Chart:
    return compile_text("cflow: 2\nname: t\n" + textwrap.dedent(text),
                        source="t.yaml", resolve_include=lambda n: None)


DECL = """
inputs:
  goal: {type: text, required: true, label: 목표}
  issue: {type: string, placeholder: claunch-xxxx}
  rounds: {type: int, default: 3}
  dry: {type: bool}
  mode: {type: enum, choices: [full, quick], default: full}
  scope: {type: list}
  invariants: {type: invariants}
  acceptance: {type: acceptance}
nodes:
  a: {prompt: "목표: ${inputs.goal}", effects: []}
  b: {prompt: b, effects: []}
body: [a, b]
"""


def test_schema_lands_in_meta():
    chart = C(DECL)
    sch = chart.meta["inputs"]
    assert list(sch) == ["goal", "issue", "rounds", "dry", "mode", "scope", "invariants", "acceptance"]
    assert sch["goal"] == {"type": "text", "label": "목표", "help": "", "required": True,
                           "default": None, "choices": None, "placeholder": "",
                           "precheck": None, "set_by": [], "options": None,
                           "group": None, "wide": False}
    assert sch["rounds"]["default"] == 3 and sch["mode"]["choices"] == ["full", "quick"]
    assert C("nodes: {a: {prompt: a}}\nbody: a\n").meta["inputs"] == {}


@pytest.mark.parametrize("decl, msg", [
    ("x: {type: float}", "unknown type"),
    ("x: {type: int, default: two}", "default 'two' is not an integer"),
    ("x: {type: bool, default: 'yes'}", "is not true or false"),
    ("x: {type: enum}", "needs choices"),
    ("x: {type: enum, choices: [a], default: b}", "is not one of a"),
    ("x: {type: string, choices: [a]}", "enum only"),
    ("1x: {type: string}", "a name is letters"),
    ("x: {type: string, colour: red}", "unknown key 'colour'"),
    ("x: {type: list, default: a}", "is not a list"),
    ("x: {type: invariants, default: [{probe: true}]}", "non-empty text"),
    ("x: {type: bool, precheck: docker}", "unknown precheck 'docker'"),
    ("x: {type: string, group: ''}", "group must be a non-empty name"),
    ("x: {type: string, group: 3}", "group must be a non-empty name"),
    ("x: {type: string, wide: yes}", "wide must be true or false"),
])
def test_schema_errors_are_compile_errors(decl, msg):
    with pytest.raises(FrontendError, match=msg):
        C(f"inputs:\n  {decl}\nnodes: {{a: {{prompt: a}}}}\nbody: a\n")


def test_group_and_wide_shape_the_form_only():
    """`group` and `wide` are form layout (docs 2.6): the schema carries them
    to the form, and the values they sit beside validate as before."""
    chart = C("""
    inputs:
      a: {type: string, group: Block}
      b: {type: string, group: " Block "}
      c: {type: bool, group: Block, wide: true}
      d: {type: string}
    nodes: {x: {prompt: x}}
    body: x
    """)
    sch = chart.meta["inputs"]
    assert [sch[n]["group"] for n in "abcd"] == ["Block", "Block", "Block", None]
    assert [sch[n]["wide"] for n in "abcd"] == [False, False, True, False]
    values, errors = inputs.normalize(sch, {"a": "x", "d": "y"})
    assert errors == {} and values["c"] is False


def test_normalize_coerces_defaults_and_reports_per_field():
    sch = C(DECL).meta["inputs"]
    values, errors = inputs.normalize(sch, {
        "goal": "고친다\n두 줄", "rounds": "5", "dry": "yes", "scope": "src/a\nsrc/b",
        "invariants": [{"text": "master는 그대로", "probe": "git diff --quiet master"},
                       {"text": "", "probe": ""}],
        "acceptance": "테스트가 green이다\n문서가 갱신됐다",
    })
    assert errors == {}
    assert values["rounds"] == 5 and values["dry"] is True and values["mode"] == "full"
    assert values["scope"] == ["src/a", "src/b"] and values["issue"] is None
    assert values["invariants"] == [{"text": "master는 그대로", "probe": "git diff --quiet master"}]
    assert [a["text"] for a in values["acceptance"]] == ["테스트가 green이다", "문서가 갱신됐다"]
    assert inputs.normalize(sch, {"goal": "g", "scope": "a, b"})[0]["scope"] == ["a", "b"]

    _, errors = inputs.normalize(sch, {"goal": "   ", "rounds": "many", "mode": "slow",
                                       "bogus": 1})
    assert set(errors) == {"goal", "rounds", "mode", "bogus"}
    assert "required" in errors["goal"] and "unknown input" in errors["bogus"]
    _, errors = inputs.normalize(sch, {"goal": None})
    assert "required" in errors["goal"]
    _, errors = inputs.normalize(sch, {"goal": "g", "issue": "a\nb"})
    assert "one line" in errors["issue"]
    with pytest.raises(inputs.InputsError) as info:
        inputs.check(sch, {})
    assert info.value.errors == {"goal": "목표 is required"}


def test_apply_folds_values_rules_monitors_and_acceptance():
    chart = C(DECL)
    values = inputs.check(chart.meta["inputs"], {
        "goal": "G",
        "invariants": [{"text": "master unmoved", "probe": "test \"$(git rev-parse master)\" = abc"},
                       {"text": "no new deps"}],
        "acceptance": [{"text": "tests green", "probe": "pytest -q"}, {"text": "docs"}],
    })
    out = inputs.apply(chart, values)
    assert "inputs" not in chart.data and "input-invariant-1" not in chart.rules  # copy
    assert out.data["inputs"]["goal"] == "G"
    assert out.rules["input-invariant-1"]["why"] == "user-declared invariant"
    assert out.rules["input-invariant-2"]["deliver"] == "all"
    mon = out.spec["monitors"]["input-invariant-1"]
    assert mon == {"always": "$probe.input_invariant_1 != null",
                   "probe": {"input_invariant_1": "test \"$(git rev-parse master)\" = abc"},
                   "every": 300, "on_violation": "halt"}
    assert "input-invariant-2" not in out.spec["monitors"]
    assert out.spec["acceptance"] == [
        {"id": "input-acceptance-1", "input": "acceptance", "text": "tests green",
         "probe": "pytest -q"},
        {"id": "input-acceptance-2", "input": "acceptance", "text": "docs", "probe": None}]
    for nid in ("a", "b"):
        node = out.node(nid)
        prompt = node.get("prompt")
        assert prompt.count("── 규칙 ──") == 1
        assert "master unmoved" in prompt and "no new deps" in prompt and "tests green" in prompt
        assert node.get("rules")[-3:] == ["input-invariant-1", "input-invariant-2",
                                          "input-acceptance"]
        assert node.get("text_id") != chart.node(nid).get("text_id")
    # the probe is kept as typed: nothing is substituted into it
    assert "$(git rev-parse master)" in mon["probe"]["input_invariant_1"]
    assert ir.Chart.from_json(out.to_json()).to_dict() == out.to_dict()
    assert check.errors(out) == []
    with pytest.raises(inputs.InputsError, match="already applied"):
        inputs.apply(out, values)


def test_apply_without_check_items_only_sets_data():
    chart = C(DECL)
    out = inputs.apply(chart, inputs.check(chart.meta["inputs"], {"goal": "G"}))
    assert out.rules == chart.rules and "monitors" not in out.spec
    assert out.node("a").get("prompt") == chart.node("a").get("prompt")


def test_bundled_definitions_declare_inputs():
    worker = layers.load("improv-worker-v2").meta["inputs"]
    assert {"goal", "issue", "scope", "invariants", "acceptance", "max_rounds"} <= set(worker)
    assert worker["invariants"]["type"] == "invariants"
    assert worker["acceptance"]["type"] == "acceptance"
    assert layers.load("sample-v2").meta["inputs"]


def test_worker_v2_groups_the_approval_reviewer_routing_and_pr_inputs():
    """claunch-qke6g: the fields of one concern share a block, so the operator
    reads them together and their rows line up. claunch-kzun2: the coding
    approval switch is a concern of its own, so it gets a block to itself."""
    sch = layers.load("improv-worker-v2").meta["inputs"]
    assert {n: d["group"] for n, d in sch.items() if d["group"]} == {
        "require_coding_approval": "코딩 승인",
        "reviewer_profile": "리뷰어", "reviewer_harness": "리뷰어",
        "session_routing": "라우팅", "delegate_profile": "라우팅", "delegate_model": "라우팅",
        "force_pr": "PR", "force_no_pr": "PR",
        # claunch-269q1: the knowledge recall/record switches share a block.
        "knowledge_recall": "지식 기록", "knowledge_record": "지식 기록"}
    # A block of one: it does not share a grid with the inputs declared around
    # it, so the switch no longer pairs with the round limit above it.
    assert [n for n, d in sch.items() if d["group"] == "코딩 승인"] == [
        "require_coding_approval"]
    assert [n for n, d in sch.items() if d["wide"]] == ["session_routing"]


# --------------------------------------------------------------------------- #
# input_constraints: asserts across inputs (claunch-yq80p.5)
# --------------------------------------------------------------------------- #
PAIR = """
inputs:
  a: {type: bool, precheck: gh}
  b: {type: bool}
  n: {type: int}
input_constraints:
  - assert: not ($inputs.a and $inputs.b)
    fields: [a, b]
    message: a and b exclude each other
  - $inputs.n == null or $inputs.n > 0
nodes: {w: {prompt: w}}
body: w
"""


def test_constraints_compile_into_meta():
    chart = C(PAIR)
    assert chart.meta["inputs"]["a"]["precheck"] == "gh"
    assert chart.meta["input_constraints"] == [
        {"assert": "not ($inputs.a and $inputs.b)", "fields": ["a", "b"],
         "message": "a and b exclude each other"},
        {"assert": "$inputs.n == null or $inputs.n > 0", "fields": ["n"],
         "message": "constraint not met: $inputs.n == null or $inputs.n > 0"},
    ]
    assert "input_constraints" not in C("nodes: {a: {prompt: a}}\nbody: a\n").meta


@pytest.mark.parametrize("item, msg", [
    ("$data.x", "may read only \\$inputs, not \\$data"),
    ("$inputs.zz", "\\$inputs.zz is not a declared input"),
    ("exists('self:q')", "may not call collection functions"),
    ("not (", "does not parse"),
    ("{assert: $inputs.a, fields: [zz]}", "field 'zz' is not a declared input"),
    ("{assert: $inputs.a, colour: red}", "unknown key 'colour'"),
    ("{fields: [a]}", "assert must be a non-empty expression"),
])
def test_constraint_errors_are_compile_errors(item, msg):
    text = ("inputs: {a: {type: bool}}\ninput_constraints:\n  - " + item
            + "\nnodes: {w: {prompt: w}}\nbody: w\n")
    with pytest.raises(FrontendError, match=msg):
        C(text)


def test_normalize_reports_a_violated_constraint_on_its_fields():
    chart = C(PAIR)
    sch, cons = chart.meta["inputs"], chart.meta["input_constraints"]
    values, errors = inputs.normalize(sch, {"a": True, "b": "yes"}, cons)
    assert errors == {"a": "a and b exclude each other", "b": "a and b exclude each other"}
    assert inputs.normalize(sch, {"a": True}, cons)[1] == {}
    assert inputs.normalize(sch, {"n": "0"}, cons)[1] == {
        "n": "constraint not met: $inputs.n == null or $inputs.n > 0"}
    # a field that failed its own check keeps that message; the constraint
    # reading it is not evaluated
    assert inputs.normalize(sch, {"n": "x"}, cons)[1] == {"n": "n is not an integer"}
    with pytest.raises(inputs.InputsError, match="exclude each other"):
        inputs.check(sch, {"a": True, "b": True}, cons)


def test_a_constraint_without_fields_reports_under_the_general_key():
    cons = [{"assert": "1 == 2", "fields": [], "message": "never"}]
    assert inputs.normalize({}, {}, cons)[1] == {inputs.CONSTRAINT_ERROR_KEY: "never"}


def test_worker_v2_declares_the_reviewers_profile_and_harness():
    """claunch-gqgcz: reviewer-ensure spawns its reviewer with these; empty
    means the reviewer inherits the worker's profile and harness."""
    chart = layers.load("improv-worker-v2")
    sch = chart.meta["inputs"]
    for name in ("reviewer_profile", "reviewer_harness"):
        assert sch[name]["type"] == "string" and sch[name]["default"] == ""
        assert sch[name]["set_by"] == ["user"]  # claunch-gqgcz.1
    values, errors = inputs.normalize(sch, {"reviewer_harness": "codex"},
                                      chart.meta["input_constraints"])
    assert errors == {} and values["reviewer_profile"] == ""
    [prompt] = [n.get("prompt") for nid, n in chart.nodes.items()
                if nid.rsplit("/", 1)[-1] == "reviewer-ensure"]
    assert "${data.inputs.reviewer_profile}" in prompt
    assert "${data.inputs.reviewer_harness}" in prompt
    assert "`:<harness>`" in prompt


def test_worker_v2_refuses_force_pr_with_force_no_pr():
    chart = layers.load("improv-worker-v2")
    sch, cons = chart.meta["inputs"], chart.meta["input_constraints"]
    assert sch["force_pr"]["type"] == "bool" and sch["force_pr"]["precheck"] == "gh"
    assert sch["force_no_pr"]["type"] == "bool"
    _, errors = inputs.normalize(sch, {"force_pr": True, "force_no_pr": True}, cons)
    assert set(errors) == {"force_pr", "force_no_pr"}
    for ok in ({}, {"force_pr": True}, {"force_no_pr": True}):
        assert inputs.normalize(sch, ok, cons)[1] == {}
