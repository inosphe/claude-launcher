"""cflow v2 question types, ask.brief/format/choices, the reframe answer, the
shared renderer and the ask-user procedure (docs/cflow-v2.md 2.10)."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import asks, check, executors, frontend, layers, question, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0
SCOPE = "w1"


@pytest.fixture(autouse=True)
def _no_dispatcher(monkeypatch):
    executors.register(None)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    yield
    executors.register(None)


QTYPE = """
question_types:
  pick:
    description: 고르기
    options: options
    recommendation: rec
    fields:
      problem: {type: text, required: true, label: 문제, help: 단순화한 문제}
      options:
        type: list
        required: true
        min: 2
        label: 선택지
        item:
          name: {type: name, required: true, label: 이름}
          summary: {type: str, required: true, label: 내용}
          rev: {type: enum, choices: [reversible, irreversible], required: true, label: 가역성}
      rec: {type: ref, of: options, required: true, label: 권장안}
      why: {type: text, label: 이유}
"""


def _compile(src, resolve=None):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src),
                                 resolve_include=resolve)


def _decl(src=QTYPE):
    return _compile(src + "nodes: {a: {prompt: a}}\nbody: a\n").meta["question_types"]["pick"]


BRIEF = {
    "problem": "범위를 정한다",
    "options": [
        {"name": "narrow", "summary": "좁게", "rev": "reversible"},
        {"name": "full", "summary": "넓게", "rev": "irreversible"},
    ],
    "rec": "NARROW",
    "why": "되돌릴 수 있다",
}


class Drv:
    def __init__(self, tmp_path, chart, params=None):
        self.cwd = str(tmp_path)
        self.scope = SCOPE
        self.st = runtime.start(chart, cwd=self.cwd, scope=SCOPE, params=params, caps={},
                                now=T0)

    def kw(self):
        return {"cwd": self.cwd, "scope": self.scope, "now": T0}

    def status(self):
        self.st = runtime.status(**self.kw())
        return self.st

    def report(self, ref, outcome="ok", value=None):
        self.st = runtime.report(ref, outcome, value, **self.kw())
        return self.st

    def events(self, name):
        return [e for e in runtime.journal(cwd=self.cwd, scope=self.scope)
                if e["event"] == name]

    def state(self):
        return RunStore(self.cwd, self.scope).load_state()


# ---------------------------------------------------------------------------
# declarations and validation


def test_declaration_is_normalized_in_order():
    d = _decl()
    assert [f["name"] for f in d["fields"]] == ["problem", "options", "rec", "why"]
    assert d["options"] == "options" and d["recommendation"] == "rec"
    item = d["fields"][1]["item"]
    assert [i["name"] for i in item] == ["name", "summary", "rev"]


@pytest.mark.parametrize("bad, msg", [
    ({"fields": {"a": {"type": "nope"}}, "options": "a"}, "unknown type"),
    ({"fields": {"a": {"type": "enum"}}, "options": "a"}, "needs choices"),
    ({"fields": {"a": {"type": "list"}}, "options": "a"}, "needs item"),
    ({"fields": {"a": {"type": "str"}}, "options": "a"}, "options must name a list"),
    ({"fields": {"a": {"type": "list", "item": {"x": "str"}}}, "options": "a"}, "'name' field"),
    ({"fields": {"a": {"type": "list", "item": {"name": "name"}}, "r": {"type": "ref", "of": "z"}},
      "options": "a"}, "not a list field"),
    ({"fields": {"a": {"type": "list", "item": {"name": "name"}}, "b": "str"}, "options": "a",
      "recommendation": "b"}, "recommendation must name a ref"),
])
def test_bad_declarations_are_refused(bad, msg):
    with pytest.raises(question.QuestionError, match=msg):
        question.normalize("q", bad)


def test_type_name_cannot_shadow_a_base_type():
    with pytest.raises(question.QuestionError, match="base type"):
        question.normalize("str", {"fields": {}})


def test_duplicate_question_type_is_a_compile_error(tmp_path):
    (tmp_path / "lib.yaml").write_text("cflow: 2\nname: lib\nkind: library\n" + QTYPE,
                                         encoding="utf-8")
    with pytest.raises(frontend.FrontendError, match="already defined"):
        _compile("include: [lib]\n" + QTYPE + "nodes: {a: {prompt: a}}\nbody: a\n",
                 resolve=lambda n: tmp_path / f"{n}.yaml")


def test_validate_accepts_and_normalizes():
    norm, errors = question.validate(BRIEF, _decl())
    assert errors == []
    assert norm["rec"] == "narrow"  # the option's own spelling


@pytest.mark.parametrize("change, msg", [
    ({"problem": ""}, "problem: required"),
    ({"options": BRIEF["options"][:1]}, "at least 2"),
    ({"rec": "other"}, "not one of the options names"),
    ({"extra": 1}, "extra: not a field"),
    ({"options": [dict(BRIEF["options"][0]), dict(BRIEF["options"][0])]}, "repeats item 0"),
    ({"options": [dict(BRIEF["options"][0], rev="maybe"), BRIEF["options"][1]]},
     r"options\[0\].rev: must be one of"),
    ({"options": [dict(BRIEF["options"][0], name="reframe"), BRIEF["options"][1]]},
     "not a usable option name"),
    ({"options": [dict(BRIEF["options"][0], summary=None), BRIEF["options"][1]]},
     r"options\[0\].summary: required"),
])
def test_validate_reports_field_paths(change, msg):
    _norm, errors = question.validate(dict(BRIEF, **change), _decl())
    assert any(__import__("re").search(msg, e) for e in errors), errors


# ---------------------------------------------------------------------------
# renderers


def test_render_text_follows_the_declaration():
    norm, _ = question.validate(BRIEF, _decl())
    text = question.render_text(norm, _decl())
    lines = text.splitlines()
    assert lines[0] == "문제: 범위를 정한다"
    assert "- narrow  (권장): 좁게" in lines and "    가역성: reversible" in lines
    assert "권장안: narrow" in lines and lines[-1] == "이유: 되돌릴 수 있다"
    assert text.index("문제") < text.index("선택지") < text.index("권장안") < text.index("이유")


def test_to_json_sections_and_recommended():
    norm, _ = question.validate(BRIEF, _decl())
    j = question.to_json(norm, _decl())
    assert j["format"] == "pick" and j["options"] == ["narrow", "full"]
    assert j["recommended"] == "narrow"
    assert [s["kind"] for s in j["sections"]] == ["text", "table", "choice", "text"]
    table = j["sections"][1]["value"]
    assert [c["field"] for c in table["columns"]] == ["summary", "rev"]
    assert [r["recommended"] for r in table["rows"]] == [True, False]


def test_guide_lists_every_field_and_a_skeleton():
    g = question.guide(_decl())
    assert "- 문제(problem, text, 필수): 단순화한 문제" in g
    assert "  - 가역성(rev, enum, 필수) [값: reversible | irreversible]" in g
    assert '"options": [' in g and "최소 2개" in g


def test_adding_a_field_reaches_validation_render_and_guide():
    src = QTYPE.replace("      why: {type: text, label: 이유}",
                        "      why: {type: text, label: 이유}\n"
                        "      cost: {type: str, required: true, label: 비용, help: 드는 시간}")
    d = _decl(src)
    _n, errors = question.validate(BRIEF, d)
    assert errors == ["cost: required"]
    norm, _ = question.validate(dict(BRIEF, cost="1h"), d)
    assert question.render_text(norm, d).endswith("비용: 1h")
    assert "비용(cost, str, 필수): 드는 시간" in question.guide(d)


# ---------------------------------------------------------------------------
# frontend and check


ASKB = QTYPE + """
nodes:
  frame:
    prompt: frame
    returns: {ok: {brief: "question:pick"}}
  pose:
    prompt: pose
    ask: {brief: "${result['frame'].value.brief}", format: pick, otherwise: human}
body: [frame, pose]
"""


@pytest.mark.parametrize("ask, msg", [
    ("{format: pick}", "ask-format-without-brief"),
    ("{brief: '${x}'}", "ask-format-without-brief"),
    ("{brief: '${x}', format: pick, options: {a: {}}}", "ask-brief-with-options"),
    ("{brief: 'see ${x}', format: pick}", "ask-brief-ref"),
    ("{brief: '${x}', format: pick, otherwise: 'self:a'}", "ask-choices-mismatch"),
    ("{brief: '${x}', format: pick, choices: [a, b], otherwise: 'self:c'}", "ask-choices-mismatch"),
    ("{prompt: p, choices: [a]}", "ask-choices-mismatch"),
    ("{brief: '${x}', format: pick, choices: [a, reframe]}", "bad ask.choices"),
])
def test_brief_ask_compile_errors(ask, msg):
    with pytest.raises(frontend.FrontendError, match=msg):
        _compile(QTYPE + f"nodes:\n  a: {{prompt: a, ask: {ask}}}\nbody: a\n")


def test_unknown_question_type_is_a_check_finding():
    c = _compile(ASKB.replace("format: pick", "format: nope")
                 .replace("question:pick", "question:gone"))
    codes = [(f.code, f.message) for f in check.check(c)]
    assert sum(code == "unknown-question-type" for code, _ in codes) == 2


def test_brief_ask_outcomes_include_reframe():
    c = _compile(ASKB)
    pose = next(n for n in c.walk() if n.label == "pose")
    assert {"ok", "reframe"} <= check._possible_outcomes(pose)


def test_library_compiles_but_cannot_be_started_or_called(tmp_path):
    lib = "cflow: 2\nname: lib\nkind: library\n" + QTYPE
    (tmp_path / "lib.yaml").write_text(lib, encoding="utf-8")
    chart = frontend.compile_text(lib)
    assert chart.kind == "library" and "pick" in chart.meta["question_types"]
    with pytest.raises(runtime.FlowError, match="only a workflow"):
        runtime.start(chart, cwd=str(tmp_path), scope="x", caps={}, now=T0)
    with pytest.raises(frontend.FrontendError, match="included, not called"):
        _compile("body: {call: lib}\n", resolve=lambda n: tmp_path / f"{n}.yaml")
    with pytest.raises(frontend.FrontendError, match="a library"):
        frontend.compile_text(lib + "body: end\n")


# ---------------------------------------------------------------------------
# runtime: brief ask


def test_report_is_checked_against_the_question_type(tmp_path):
    d = Drv(tmp_path, _compile(ASKB))
    with pytest.raises(runtime.FlowError, match=r"brief.*\(pick\): problem: required"):
        d.report("frame", value={"brief": dict(BRIEF, problem="")})
    d.report("frame", value={"brief": BRIEF})
    (a,) = d.status()["asks"]
    assert a["format"] == "pick" and a["brief"]["rec"] == "narrow"
    assert [o["name"] for o in a["options"]] == ["narrow", "full", "reframe"]
    assert "문제: 범위를 정한다" in a["question_text"] and "문제: 범위를 정한다" in a["prompt"]


def test_answer_finishes_ok_with_the_decision_and_brief(tmp_path):
    d = Drv(tmp_path, _compile(ASKB))
    d.report("frame", value={"brief": BRIEF})
    runtime.approve("pose", decision="FULL", reason="r", **d.kw())
    res = d.state()["result"]["pose"]
    assert res["outcome"] == "ok" and res["value"]["decision"] == "full"
    assert res["value"]["brief"]["problem"] == "범위를 정한다" and d.status()["status"] == "done"


def test_reframe_needs_a_reason_and_finishes_reframe(tmp_path):
    src = ASKB.replace("body: [frame, pose]",
                       "body:\n  try: [frame, pose]\n  catch: {reframe: end}")
    d = Drv(tmp_path, _compile(src))
    d.report("frame", value={"brief": BRIEF})
    # an agent's or a responder's reframe needs one; a person's never does
    with pytest.raises(runtime.FlowError, match="needs a reason"):
        runtime._ask_need_reason({"brief": BRIEF}, "reframe", None)
    runtime._ask_need_reason({"brief": BRIEF}, "reframe", None, by_user=True)
    runtime.approve("pose", decision="reframe", reason="옵션이 빠졌다", **d.kw())
    res = d.state()["result"]["pose"]
    assert res["outcome"] == "reframe" and res["value"]["reason"] == "옵션이 빠졌다"
    assert "decision" not in res["value"]


def test_invalid_brief_at_open_fails_the_task(tmp_path):
    src = ASKB.replace('returns: {ok: {brief: "question:pick"}}', "returns: {ok: {brief: dict}}")
    d = Drv(tmp_path, _compile(src))
    d.report("frame", value={"brief": {"problem": "x"}})
    res = d.state()["result"]["pose"]
    assert res["outcome"] == "fail" and "options: required" in res["value"]["reason"]
    assert d.events("ask_brief_invalid")[0]["format"] == "pick"


def test_fixed_choices_must_match_the_brief(tmp_path):
    src = ASKB.replace("format: pick,", "format: pick, choices: [narrow, wide],")
    d = Drv(tmp_path, _compile(src))
    d.report("frame", value={"brief": BRIEF})
    res = d.state()["result"]["pose"]
    assert res["outcome"] == "fail" and "exactly the fixed choices" in res["value"]["reason"]


def test_decide_message_carries_the_rendered_question(tmp_path):
    d = Drv(tmp_path, _compile(ASKB))
    d.report("frame", value={"brief": BRIEF})
    rec = next(iter(d.state()["asks"].values()))
    text = asks.question_text(rec, workflow="t", sender="w1", cwd=d.cwd, scope=d.scope)
    assert "- narrow  (권장): 좁게" in text and "  - reframe:" in text


# ---------------------------------------------------------------------------
# ask-user, end to end through the package library


CALLER = """
include: [ask-common]
nodes:
  narrow: {prompt: narrow}
  full: {prompt: full}
body:
  - call: ask-user
    with: {topic: 범위, choices: [narrow, full]}
    bind: choice
  - cond:
      - {if: "$data.choice.decision == 'narrow'", then: narrow}
      - {else: full}
"""

DECISION = {
    "problem": "범위를 정한다",
    "options": [
        {"name": "narrow", "summary": "좁게", "risk": "작음", "outcome": "빨리 끝남",
         "reversibility": "reversible", "reverse_how": "다음 회차에 넓힌다"},
        {"name": "full", "summary": "넓게", "risk": "큼", "outcome": "한 번에 끝남",
         "reversibility": "costly"},
    ],
    "recommendation": "narrow",
    "why": "되돌릴 수 있다",
}


def _caller(tmp_path):
    return _compile(CALLER, resolve=layers.resolver(str(tmp_path)))


def test_ask_user_frames_asks_and_branches(tmp_path):
    d = Drv(tmp_path, _caller(tmp_path))
    st = d.status()
    (t,) = st["tasks"]
    assert t["label"] == "ask-frame"
    assert "결정할 것: 범위" in t["prompt"] and "- 문제(problem, text, 필수)" in t["prompt"]
    assert '["narrow", "full"]' in t["prompt"]
    d.report("ask-frame", value={"brief": DECISION})
    (a,) = d.status()["asks"]
    assert a["resolution"] == "human" and a["format"] == "decision"
    runtime.approve(None, decision="narrow", reason="ok", **d.kw())
    st = d.status()
    assert [x["label"] for x in st["tasks"]] == ["narrow"]
    choice = d.state()["data"]["choice"]
    assert choice["decision"] == "narrow" and choice["by"] == "user"
    assert choice["brief"]["recommendation"] == "narrow"


def test_ask_user_reframes_up_to_three_times(tmp_path):
    d = Drv(tmp_path, _caller(tmp_path))
    d.report("ask-frame", value={"brief": DECISION})
    for n in range(2):
        runtime.approve(None, decision="reframe", reason=f"why {n}", **d.kw())
        (t,) = d.status()["tasks"]
        assert t["label"] == "ask-reframe" and f"이유: why {n}" in t["prompt"]
        d.report("ask-reframe", value={"brief": DECISION})
    runtime.approve(None, decision="reframe", reason="last", **d.kw())
    st = d.status()
    # the procedure finished 'reframe'; nothing handles it, so the run fails
    assert st["status"] == "failed"
    res = [v for k, v in d.state()["result"].items() if v.get("outcome") == "reframe"]
    assert any((v.get("value") or {}).get("reason") == "last" for v in res)


def test_ask_user_rejects_choices_that_do_not_match(tmp_path):
    d = Drv(tmp_path, _caller(tmp_path))
    bad = dict(DECISION, options=[dict(DECISION["options"][0], name="tiny"),
                                  DECISION["options"][1]], recommendation="tiny")
    d.report("ask-frame", value={"brief": bad})
    assert d.status()["status"] == "failed"
    assert "exactly the fixed choices" in d.events("ask_brief_invalid")[0]["errors"][0]


def test_require_reason_refuses_an_agents_bare_answer_but_not_a_persons(tmp_path):
    src = ASKB.replace("format: pick,", "format: pick, require_reason: true,")
    d = Drv(tmp_path, _compile(src))
    d.report("frame", value={"brief": BRIEF})
    (a,) = d.status()["asks"]
    assert a["require_reason"] is True and a["question"]["recommended"] == "narrow"
    (rec,) = d.state()["asks"].values()
    with pytest.raises(runtime.FlowError, match="needs a reason"):
        runtime._ask_need_reason(rec, "narrow", "")
    runtime.approve("pose", decision="narrow", **d.kw())  # the person's channel
    assert d.state()["result"]["pose"]["value"]["decision"] == "narrow"


def test_require_reason_is_for_brief_asks_only():
    with pytest.raises(frontend.FrontendError, match="require_reason applies"):
        _compile(QTYPE + "nodes:\n  a: {prompt: a, ask: {prompt: p, require_reason: true}}\n"
                 "body: a\n")


def test_ask_user_passes_require_reason(tmp_path):
    src = CALLER.replace("choices: [narrow, full]}", "choices: [narrow, full], require_reason: true}")
    d = Drv(tmp_path, _compile(src, resolve=layers.resolver(str(tmp_path))))
    d.report("ask-frame", value={"brief": DECISION})
    assert [x["require_reason"] for x in d.state()["asks"].values()] == [True]
    runtime.approve(None, decision="full", **d.kw())  # a person may still omit it
