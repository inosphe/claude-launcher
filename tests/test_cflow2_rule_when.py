"""Conditional rules and input change impact (claunch-cetzf.1, docs 2.5/2.6).

A rule with ``when:`` is not baked into prompts at compile time. The runtime
appends its text under the rules header on every read (status, delegate
brief, ask view) while the expression holds, so a person's input change shows
up in the next status. ``inputs.dependencies`` maps each input to the rules
and nodes that read it; ``set_input`` reports which conditional rules turned
on or off (``input_impact``) and the nudges carry that block.
"""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import check, frontend, inputs, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

DEF = """
inputs:
  on_main: {type: bool, default: false, set_by: [user]}
  idle: {type: bool, default: false, set_by: [user]}
rules:
  always:
    text: ALWAYS-RULE
    deliver: [work]
  off-main:
    when: "$data.inputs.on_main != true"
    text: OFF-MAIN-RULE
    deliver: [work]
  on-main:
    when: "$data.inputs.on_main == true"
    text: ON-MAIN-RULE
    deliver: [work]
nodes:
  work: {prompt: "work here"}
body: work
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def _chart(text: str = DEF):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(text))


def _start(tmp_path, values=None):
    return runtime.start(_chart(), cwd=str(tmp_path), scope="t", caps={}, driver="drv",
                         inputs=dict(values or {}), now=T0)


def _person(tmp_path, name, value):
    return runtime.set_input(name, value, caller=runtime.CALLER_USER, cwd=str(tmp_path),
                             scope="t", now=T0)


def _task(st):
    (task,) = st["tasks"]
    return task


# -- compile -----------------------------------------------------------------

def test_a_conditional_rule_is_kept_on_the_node_but_not_baked():
    chart = _chart()
    node = next(n for n in chart.walk() if n.type == "task")
    prompt = node.get("prompt")
    assert "ALWAYS-RULE" in prompt
    assert "OFF-MAIN-RULE" not in prompt and "ON-MAIN-RULE" not in prompt
    assert node.get("rules") == ["always", "off-main", "on-main"]
    assert chart.rules["off-main"]["when"] == "$data.inputs.on_main != true"
    assert "when" not in chart.rules["always"]


def test_a_bad_when_expression_is_a_compile_error():
    bad = DEF.replace('"$data.inputs.on_main != true"', '"$data.inputs.on_main !="')
    with pytest.raises(frontend.FrontendError, match="when"):
        _chart(bad)


# -- runtime rendering ---------------------------------------------------------

def test_status_appends_the_rules_whose_condition_holds(tmp_path):
    st = _start(tmp_path)
    task = _task(st)
    assert "ALWAYS-RULE" in task["prompt"]
    assert "OFF-MAIN-RULE" in task["prompt"]
    assert "ON-MAIN-RULE" not in task["prompt"]
    # one rules header: the conditional text joins the baked block
    assert task["prompt"].count(runtime.RULES_HEADER) == 1
    view = {r["id"]: r for r in task["rules"]}
    assert "active" not in view["always"]
    assert view["off-main"]["active"] is True
    assert view["on-main"]["active"] is False


def test_a_persons_input_change_flips_the_rules_in_the_next_status(tmp_path):
    _start(tmp_path)
    out = _person(tmp_path, "on_main", True)
    task = _task(out)
    assert "ON-MAIN-RULE" in task["prompt"]
    assert "OFF-MAIN-RULE" not in task["prompt"]
    impact = out["input_impact"]
    flips = {r["id"]: (r["was"], r["now"]) for r in impact["rules"]}
    assert flips == {"off-main": (True, False), "on-main": (False, True)}
    assert [t["node"] for t in impact["tasks"]] == [task["node"]]
    journal = [e for e in RunStore(str(tmp_path), "t").journal() if e["event"] == "input_impact"]
    assert journal and journal[-1]["name"] == "on_main"
    # a later status (a reminder, a rebrief) reads the same value
    again = runtime.status(cwd=str(tmp_path), scope="t", now=T0, tick=False)
    assert "ON-MAIN-RULE" in _task(again)["prompt"]


def test_a_change_nothing_reads_has_an_empty_impact(tmp_path):
    _start(tmp_path)
    out = _person(tmp_path, "idle", True)
    assert out["input_impact"]["rules"] == [] and out["input_impact"]["tasks"] == []
    assert inputs.format_impact(out["input_impact"]) == ""


def test_rule_lookup_reports_the_condition():
    out = runtime.rule("on-main", chart=_chart())
    assert out["when"] == "$data.inputs.on_main == true"
    assert "when" not in runtime.rule("always", chart=_chart())


def test_an_unevaluable_condition_shows_the_rule():
    assert runtime.rule_active({"when": "$data.nope.deeper > 3"}, {"data": {}}) is True
    assert runtime.rule_active({}, {}) is True


# -- dependency graph and impact block -----------------------------------------

def test_dependencies_name_the_rules_and_nodes_reading_an_input():
    deps = inputs.dependencies(_chart())
    assert deps["on_main"]["conditional"] == ["off-main", "on-main"]
    assert deps["on_main"]["nodes"] == ["work"]
    assert deps["idle"] == {"rules": [], "conditional": [], "nodes": []}


def test_check_warns_about_a_user_input_nothing_reads():
    codes = {(f.code, f.message.split("'")[1]) for f in check.check(_chart())
             if f.code == "set-by-unread"}
    assert codes == {("set-by-unread", "idle")}


def test_the_impact_block_lists_turned_on_rule_text():
    block = inputs.format_impact({
        "input": "x",
        "rules": [{"id": "a", "when": "$data.inputs.x", "was": False, "now": True,
                   "text": "LINE1\nLINE2"},
                  {"id": "b", "when": "not $data.inputs.x", "was": True, "now": False,
                   "text": "GONE"}],
        "tasks": [{"instance": "work@1", "node": "work"}]})
    assert "규칙 a: 켜짐" in block and "    LINE2" in block
    assert "규칙 b: 꺼짐" in block and "GONE" not in block
    assert "work(work@1)" in block


def test_a_conditional_deny_is_a_warning():
    text = DEF.replace('    text: ON-MAIN-RULE\n',
                       '    text: ON-MAIN-RULE\n    effect: {deny: git.commit}\n')
    text = text.replace('work: {prompt: "work here"}',
                        'work: {prompt: "work here", effects: [git.commit]}')
    found = {f.code: f for f in check.check(_chart(text))}
    assert "never-effect" not in found
    assert found["never-effect-when"].level == check.WARNING
