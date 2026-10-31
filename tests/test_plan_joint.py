"""Joint plan trees (plan/joint.py, claunch-7jzp8.1): spawn links first,
then hierarchical issue ids, then a shared issue; a plan-less spawn parent
still drawn; cycles dropped; groups of one kept."""

from claude_launcher.plan import joint as J


def entry(issue=None, parent=None, waiting=0, closable=False, title="t"):
    return {"title": title, "issue": issue, "context": {"parent": parent} if parent else {},
            "waiting": waiting, "closable": closable}


def tree(group):
    return [(m["session"], m["parent"], m["via"], m["depth"]) for m in group["members"]]


def test_issue_parent():
    assert J.issue_parent("claunch-x.2") == "claunch-x"
    assert J.issue_parent("claunch-x.2.1") == "claunch-x.2"
    assert J.issue_parent("claunch-x") is None
    assert J.issue_parent("v1.beta") is None
    assert J.issue_parent(None) is None


def test_spawn_then_issue_then_same():
    rail = {"lead": entry("claunch-x", waiting=1),
            "w1": entry("claunch-x.1", waiting=2),           # issue child, no spawn parent
            "w2": entry("claunch-x.3", closable=True),       # spawned by w1 (live record)
            "w3": entry("claunch-x.1", closable=True),       # same issue as w1
            "lone": entry("claunch-y", closable=True)}
    live = {"w2": "w1"}
    gs = J.groups(rail, lambda n: live.get(n, "" if n in rail else None))
    assert [g["root"] for g in gs] == ["lead", "lone"]
    assert tree(gs[0]) == [("lead", None, None, 0), ("w1", "lead", "issue", 1),
                           ("w2", "w1", "spawn", 2), ("w3", "w1", "same", 2)]
    assert (gs[0]["sessions"], gs[0]["waiting"], gs[0]["closable"]) == (4, 3, False)
    assert gs[1]["sessions"] == 1 and gs[1]["closable"] is True
    assert J.group_of(rail, lambda n: live.get(n), "w3")["root"] == "lead"
    assert J.group_of(rail, lambda n: None, "nobody") is None


def test_a_planless_spawn_parent_is_drawn_and_context_is_the_fallback():
    rail = {"c1": entry(parent="boss"), "c2": entry(parent="boss")}
    g, = J.groups(rail, lambda n: None)                        # nobody live: the plans' context
    assert tree(g) == [("boss", None, None, 0), ("c1", "boss", "spawn", 1), ("c2", "boss", "spawn", 1)]
    assert g["members"][0]["has_plan"] is False and g["sessions"] == 2
    # a live record without a parent overrides the plan's stale context
    gs = J.groups(rail, lambda n: "" if n == "c1" else None)
    assert sorted(g["root"] for g in gs) == ["boss", "c1"]


def test_a_cycle_is_broken():
    rail = {"a": entry(), "b": entry()}
    live = {"a": "b", "b": "a"}
    g, = J.groups(rail, live.get)
    assert {m["session"] for m in g["members"]} == {"a", "b"}
    assert sum(1 for m in g["members"] if m["parent"] is None) == 1
