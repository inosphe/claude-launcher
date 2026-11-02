"""improv-worker-v2's knowledge nodes (claunch-269q1).

knowledge-recall stands after category-tag and runs only when the start input
knowledge_recall is true (default false: the A/B arm without recall);
knowledge-record stands after category-refresh and runs unless
knowledge_record is false (default true). These tests take the two cond
blocks out of the real definition and drive them through the runtime, so a
change to either guard is measured, not restated.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import claude_launcher
from claude_launcher.cflow2 import frontend, inputs, layers, runtime

V2 = Path(claude_launcher.__file__).parent / "workflows" / "v2" / "improv-worker-v2.yaml"


def _doc():
    return yaml.load(V2.read_text(encoding="utf-8"), Loader=frontend._Loader)


def _find(tree, ident):
    """The list holding the combinator with this id, and its index there."""
    if isinstance(tree, list):
        for i, item in enumerate(tree):
            if isinstance(item, dict) and item.get("id") == ident:
                return tree, i
            found = _find(item, ident)
            if found:
                return found
    elif isinstance(tree, dict):
        for value in tree.values():
            found = _find(value, ident)
            if found:
                return found
    return None


def test_inputs_are_human_switches_recall_off_record_on():
    sch = layers.load("improv-worker-v2").meta["inputs"]
    assert sch["knowledge_recall"]["type"] == sch["knowledge_record"]["type"] == "bool"
    assert sch["knowledge_recall"]["default"] is False
    assert sch["knowledge_record"]["default"] is True
    for name in ("knowledge_recall", "knowledge_record"):
        assert sch[name]["set_by"] == ["user"], name


def test_nodes_stand_right_after_the_category_nodes():
    doc = _doc()
    for ident, before, task in (("knowledge-pre", "category-tag", "knowledge-recall"),
                                ("knowledge-post", "category-refresh", "knowledge-record")):
        found = _find(doc["procedures"], ident) or _find(doc["body"], ident)
        assert found, ident
        seq, i = found
        assert seq[i - 1] == before
        assert seq[i]["cond"][0]["then"] == [task]
    labels = {n.label for n in layers.load("improv-worker-v2").nodes.values()}
    assert {"knowledge-recall", "knowledge-record", "glossary-draft"} <= labels
    # claunch-ar8jn: the glossary drafts stand right after the knowledge
    # records and before the round report.
    seq, i = _find(doc["procedures"], "glossary-post") or _find(doc["body"], "glossary-post")
    assert seq[i - 1]["id"] == "knowledge-post" and seq[i + 1] == "round-report"
    assert seq[i]["cond"][0]["then"] == ["glossary-draft"]


def test_wrapup_nodes_draft_instead_of_writing_the_stores():
    """claunch-ar8jn: category, knowledge and glossary entries go to the
    session's wrap-up drafts; the stores are written by the user's confirm."""
    nodes = _doc()["nodes"]
    frag = _doc()["fragments"]["category-tag"]
    assert "wrapup_draft {kind: category" in frag and "직접 부르지 않는다" in frag
    assert "즉시 반영이다" not in frag
    assert "wrapup_draft {kind: knowledge" in nodes["knowledge-record"]["prompt"]
    assert "직접 부르지 않는다" in nodes["knowledge-record"]["prompt"]
    assert "wrapup_draft {kind: glossary" in nodes["glossary-draft"]["prompt"]
    sch = layers.load("improv-worker-v2").meta["inputs"]
    assert sch["glossary_draft"]["default"] is True and sch["glossary_draft"]["set_by"] == ["user"]


def test_rounds_retroactively_fix_the_drafts_earlier_rounds_left():
    """claunch-oqn6t: the three draft nodes carry one shared retro-fix
    procedure, so a later round corrects or withdraws an earlier round's
    still-open draft instead of only appending a new one."""
    doc = _doc()
    frag = doc["fragments"]["retro-fix"]
    for phrase in ('wrapup_list {settled: true}', 'wrapup_update {id, revision, payload}',
                   'wrapup_withdraw {id, revision}', 'OPEN = ("draft", "failed")'):
        assert phrase in frag, phrase
    for name in ("category-refresh", "knowledge-record", "glossary-draft"):
        assert "${frag:retro-fix}" in doc["nodes"][name]["prompt"], name
    # The reference alone proves nothing about what a session reads: the
    # compiled prompt must carry the procedure's text.
    compiled = {n.label: n for n in layers.load("improv-worker-v2").nodes.values()}
    for name in ("category-refresh", "knowledge-record", "glossary-draft"):
        prompt = compiled[name].props["prompt"]
        assert "소급 점검: 앞 회차가 남긴 임시본" in prompt, name
        assert "wrapup_withdraw {id, revision}" in prompt, name


def _run(tmp_path, monkeypatch, values):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = _doc()
    pre = _find(doc["procedures"], "knowledge-pre") or _find(doc["body"], "knowledge-pre")
    post = _find(doc["procedures"], "knowledge-post") or _find(doc["body"], "knowledge-post")
    gloss = _find(doc["procedures"], "glossary-post") or _find(doc["body"], "glossary-post")
    doc["body"] = ["category-tag", pre[0][pre[1]], "category-refresh", post[0][post[1]], gloss[0][gloss[1]]]
    doc.pop("spec", None)
    for name in ("category-tag", "category-refresh"):
        doc["nodes"][name] = {"prompt": name}
    # The recall prompt reads the intake's goal; the fixture has no intake.
    doc["nodes"]["knowledge-recall"]["prompt"] = "recall"
    chart = frontend.compile_text(json.dumps(doc, ensure_ascii=False),
                                  resolve_include=layers.resolver())
    chart = inputs.apply(chart, values)
    kw = {"cwd": str(tmp_path), "scope": "knowledge", "now": 1.0}
    st = runtime.start(chart, caps={}, **kw)
    seen = []
    while st["status"] != "done":
        [task] = st["tasks"]
        seen.append(task["label"])
        value = {"knowledge-recall": {"consulted": True, "query": "q", "records": [], "note": ""},
                 "knowledge-record": {"recorded": False, "ids": [], "note": ""},
                 "glossary-draft": {"drafted": False, "ids": [], "pending": 0, "note": ""}}.get(task["label"], {})
        st = runtime.report(task["instance"], "ok", value, **kw)
    return seen


@pytest.mark.parametrize("values, expected", [
    ({}, ["category-tag", "category-refresh", "knowledge-record", "glossary-draft"]),
    ({"knowledge_recall": True}, ["category-tag", "knowledge-recall", "category-refresh", "knowledge-record",
                                  "glossary-draft"]),
    ({"knowledge_recall": True, "knowledge_record": False}, ["category-tag", "knowledge-recall", "category-refresh",
                                                             "glossary-draft"]),
    ({"knowledge_record": False}, ["category-tag", "category-refresh", "glossary-draft"]),
    ({"glossary_draft": False}, ["category-tag", "category-refresh", "knowledge-record"]),
])
def test_switches_route_the_two_nodes(tmp_path, monkeypatch, values, expected):
    assert _run(tmp_path, monkeypatch, values) == expected
