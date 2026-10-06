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
                           "default": None, "choices": None, "placeholder": ""}
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
])
def test_schema_errors_are_compile_errors(decl, msg):
    with pytest.raises(FrontendError, match=msg):
        C(f"inputs:\n  {decl}\nnodes: {{a: {{prompt: a}}}}\nbody: a\n")


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
