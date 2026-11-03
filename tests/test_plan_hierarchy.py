"""Hierarchical decisions, the session's default pick, ruled-out options and
the change block (claunch-vmcg7, th-1c6013).

* A decision's child may hang ``under`` one of its options; it counts only
  while that option is the decision's choice (``summary.inactive`` names the
  others), and the user cannot decide on it before then.
* ``default`` on an option answers the decision as the session (``by:
  agent``): it waits on nobody, so the start gate passes, and the user may
  still pick another option.
* ``rejected: <reason>`` keeps an option the session ruled out on view; it
  cannot be picked, recommended or defaulted, nor carry children.
* Changing a decision that already had an answer reaches the session as a
  CHANGED block with the before and after options.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import plan_waiting as gate  # noqa: E402

from claude_launcher.daemon import plan_api  # noqa: E402
from claude_launcher.plan import store as P  # noqa: E402

A = {"label": "A", "effect": "fast", "risk": "untested"}
B = {"label": "B", "effect": "safe", "risk": "slow"}
C = {"label": "C", "effect": "cheap", "risk": "brittle"}


@pytest.fixture
def store(tmp_path):
    return P.PlanStore(tmp_path / "plan.sqlite3")


def _write(store, rev, steps):
    return store.set("s1", "s1", rev, title="round", steps=steps)


def _tree():
    return [{"id": "d1", "kind": "decision", "title": "storage",
             "options": [{**A, "recommended": True, "default": True}, B,
                         {**C, "rejected": "no backup path"}],
             "children": [
                 {"id": "s-common", "title": "write it"},
                 {"id": "d2", "kind": "decision", "title": "index", "under": "A", "options": [A, B]},
                 {"id": "d3", "kind": "decision", "title": "migrate", "under": "B", "options": [A, B],
                  "children": [{"id": "s-deep", "title": "copy rows", "under": None}]},
             ]}]


# -- default, under, inactive ---------------------------------------------- #
def test_the_default_answers_the_decision_and_only_the_open_branch_counts(store):
    p = _write(store, 0, _tree())
    d1 = p["steps"][0]
    assert d1["result"]["choice"] == "A" and d1["result"]["by"] == "agent"
    s = p["summary"]
    assert s["auto"] == 1
    assert s["waiting_decisions"] == 1          # d2, under the chosen A; d1 waits on nobody
    assert set(s["inactive"]) == {"d3", "s-deep"}
    assert gate.verdict(s, "start") == gate.WAITING


def test_a_default_alone_clears_the_start_gate(store):
    p = _write(store, 0, [{"id": "d1", "kind": "decision", "title": "x",
                           "options": [{**A, "default": True}, B]}])
    assert p["summary"]["waiting_decisions"] == 0
    assert gate.verdict(p["summary"], "start") == gate.CLEAR


def test_overriding_the_default_switches_the_open_branch(store):
    _write(store, 0, _tree())
    res = store.decide(P.USER, "s1", "d1", 1, choice="B")
    assert res["previous"]["by"] == "agent" and res["previous"]["choice"] == "A"
    s = res["plan"]["summary"]
    assert s["auto"] == 0 and set(s["inactive"]) == {"d2"}
    assert s["waiting_decisions"] == 1          # now d3
    # a rewrite with the same words keeps the user's pick over the default
    again = _write(store, res["plan"]["revision"], _tree())
    assert again["steps"][0]["result"]["choice"] == "B"


def test_the_user_cannot_pick_a_rejected_option_or_decide_off_the_path(store):
    _write(store, 0, _tree())
    with pytest.raises(P.PlanError, match="ruled out"):
        store.decide(P.USER, "s1", "d1", 1, choice="C")
    with pytest.raises(P.PlanError, match="not the path taken"):
        store.decide(P.USER, "s1", "d3", 1, choice="A")
    store.decide(P.USER, "s1", "d2", 1, choice="B")


# -- validation ------------------------------------------------------------ #
@pytest.mark.parametrize("options, why", [
    ([{**A, "rejected": True}, B, C], "reason"),
    ([{**A, "default": True}, {**B, "default": True}], "default"),
    ([{**A, "rejected": "x", "default": True}, B], "rejected"),
    ([{**A, "rejected": "x", "recommended": True}, B, C], "rejected"),
    ([A, {**B, "rejected": "x"}], "pick"),
])
def test_option_fields_are_checked(store, options, why):
    with pytest.raises(P.PlanError, match=why):
        _write(store, 0, [{"id": "d1", "kind": "decision", "title": "x", "options": options}])


def test_one_pickable_option_is_enough_with_a_default(store):
    p = _write(store, 0, [{"id": "d1", "kind": "decision", "title": "x",
                           "options": [{**A, "default": True}, {**B, "rejected": "too slow"}]}])
    assert p["steps"][0]["result"]["choice"] == "A"
    assert p["steps"][0]["options"][0]["recommended"] is True   # default implies recommended


@pytest.mark.parametrize("parent, under, why", [
    ({"id": "s0", "title": "plain step"}, "A", "decision it sits in"),
    ({"id": "d0", "kind": "decision", "title": "x", "options": [A, B]}, "Z", "not an option"),
    ({"id": "d0", "kind": "decision", "title": "x", "options": [A, B, {**C, "rejected": "no"}]}, "C", "rejected"),
])
def test_under_must_name_a_live_option_of_the_parent_decision(store, parent, under, why):
    with pytest.raises(P.PlanError, match=why):
        _write(store, 0, [{**parent, "children": [{"id": "k", "title": "child", "under": under}]}])


def test_five_levels_deep_and_no_more(store):
    def chain(n):
        node = {"id": f"n{n}", "title": f"level {n}"}
        if n < 5:
            node["children"] = [chain(n + 1)]
        return node
    _write(store, 0, [chain(1)])
    too = chain(1)
    tip = too
    while tip.get("children"):
        tip = tip["children"][0]
    tip["children"] = [{"id": "n6", "title": "level 6"}]
    with pytest.raises(P.PlanError, match="5 levels"):
        _write(store, 1, [too])


def test_an_option_patch_cannot_strand_a_child_under_it(store):
    _write(store, 0, _tree())
    with pytest.raises(P.PlanError):
        store.patch("s1", "s1", "d1", options=[{**A, "default": True}, C])            # B dropped, d3 under it
    with pytest.raises(P.PlanError):
        store.patch("s1", "s1", "d1", options=[{**A, "default": True}, {**B, "rejected": "x"}, C])


# -- the block the session receives --------------------------------------- #
def test_a_changed_choice_carries_before_and_after(store):
    _write(store, 0, _tree())
    res = store.decide(P.USER, "s1", "d1", 1, choice="B", note="safer")
    text = plan_api.decided_text("s1", res["node"], res["previous"])
    assert "CHANGED" in text and '"A" -> "B"' in text and "your default" in text
    assert "before" in text and "after" in text and "plan_set" in text
    assert "> safer" in text

    res2 = store.decide(P.USER, "s1", "d1", res["node"]["rev"], choice="A")
    text2 = plan_api.decided_text("s1", res2["node"], res2["previous"])
    assert '"B" -> "A"' in text2 and "their earlier choice" in text2


def test_confirming_the_default_says_nothing_changes(store):
    _write(store, 0, _tree())
    res = store.decide(P.USER, "s1", "d1", 1, choice="A")
    text = plan_api.decided_text("s1", res["node"], res["previous"])
    assert "CHANGED" not in text and "confirmed your default" in text


def test_a_first_choice_is_no_change(store):
    _write(store, 0, [{"id": "d1", "kind": "decision", "title": "x", "options": [A, B]}])
    res = store.decide(P.USER, "s1", "d1", 1, choice="B")
    assert res["previous"] is None
    assert "CHANGED" not in plan_api.decided_text("s1", res["node"], res["previous"])
