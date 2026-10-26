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
    assert {"knowledge-recall", "knowledge-record"} <= labels


def _run(tmp_path, monkeypatch, values):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    doc = _doc()
    pre = _find(doc["procedures"], "knowledge-pre") or _find(doc["body"], "knowledge-pre")
    post = _find(doc["procedures"], "knowledge-post") or _find(doc["body"], "knowledge-post")
    doc["body"] = ["category-tag", pre[0][pre[1]], "category-refresh", post[0][post[1]]]
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
                 "knowledge-record": {"recorded": False, "ids": [], "note": ""}}.get(task["label"], {})
        st = runtime.report(task["instance"], "ok", value, **kw)
    return seen


@pytest.mark.parametrize("values, expected", [
    ({}, ["category-tag", "category-refresh", "knowledge-record"]),
    ({"knowledge_recall": True}, ["category-tag", "knowledge-recall", "category-refresh", "knowledge-record"]),
    ({"knowledge_recall": True, "knowledge_record": False}, ["category-tag", "knowledge-recall", "category-refresh"]),
    ({"knowledge_record": False}, ["category-tag", "category-refresh"]),
])
def test_switches_route_the_two_nodes(tmp_path, monkeypatch, values, expected):
    assert _run(tmp_path, monkeypatch, values) == expected
