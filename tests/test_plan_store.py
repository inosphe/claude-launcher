"""Session plan store (plan/store.py, claunch-7jzp8): the two
compare-and-swaps, results carried by node id across rewrites and set aside
when what they were given for changes, and the summary that says whether a
session may close."""

import pytest

from claude_launcher.plan import store as P

OPTS = [{"label": "A", "effect": "fast", "risk": "untested", "recommended": True},
        {"label": "B", "effect": "safe", "risk": "slow"}]


def plan(store, rev=0, **kw):
    doc = dict(title="Ship X", goal="X in one round",
               steps=[{"id": "s1", "title": "read code", "status": "done"},
                      {"id": "d1", "kind": "decision", "title": "storage", "options": OPTS,
                       "children": [{"id": "s2", "title": "write store"}]}],
               closure=[{"id": "v1", "kind": "verify", "title": "open the pane by hand"},
                        {"id": "f1", "kind": "followup", "title": "joint tree"}])
    doc.update(kw)
    return store.set("s1", "s1", rev, **doc, context={"project": "claunch"})


@pytest.fixture
def store(tmp_path):
    return P.PlanStore(tmp_path / "plan.sqlite3")


def test_set_shapes_the_plan_and_summary(store):
    p = plan(store)
    assert p["revision"] == 1 and p["context"] == {"project": "claunch"}
    d1 = p["steps"][1]
    assert d1["rev"] == 1 and d1["result"] is None and d1["children"][0]["status"] == "todo"
    assert p["closure"][1]["status"] == "open"
    s = p["summary"]
    assert (s["steps"], s["done"], s["decisions"], s["verify"], s["followups"]) == (2, 1, 1, 1, 1)
    assert s["waiting"] == 3 and s["closable"] is False
    assert {b["id"] for b in s["blockers"]} == {"d1", "s2", "v1", "f1"}


def test_only_the_owner_writes_and_only_the_user_decides(store):
    with pytest.raises(P.Forbidden):
        store.set("s2", "s1", 0, title="t")
    with pytest.raises(P.Forbidden):
        store.set(P.USER, "s1", 0, title="t")
    plan(store)
    with pytest.raises(P.Forbidden):
        store.decide("s1", "s1", "d1", 1, choice="A")
    with pytest.raises(P.Forbidden):
        store.patch("s2", "s1", "s1", status="todo")


def test_whole_plan_write_is_refused_over_a_decision_it_has_not_read(store):
    plan(store)
    store.decide(P.USER, "s1", "d1", 1, choice="B", note="safer")
    with pytest.raises(P.Conflict) as exc:
        plan(store, rev=1)
    assert exc.value.current["revision"] == 2
    again = plan(store, rev=2)          # read, rewritten with the same ids
    assert again["steps"][1]["result"]["choice"] == "B"


def test_a_step_status_does_not_refuse_the_users_click(store):
    plan(store)
    store.patch("s1", "s1", "s2", status="doing")
    res = store.decide(P.USER, "s1", "d1", 1, choice="A")
    assert res["node"]["result"]["choice"] == "A"
    assert res["plan"]["summary"]["current"] == "write store"
    # the children survive a patch on their parent
    after = store.patch("s1", "s1", "d1", detail="more")["plan"]
    assert after["steps"][1]["children"][0]["id"] == "s2"


def test_rewording_sets_the_result_aside_and_asks_again(store):
    plan(store)
    store.decide(P.USER, "s1", "d1", 1, choice="A")
    out = store.patch("s1", "s1", "d1", options=[*OPTS, {"label": "C", "effect": "e", "risk": "r"}])
    node = out["node"]
    assert node["rev"] == 2 and node["result"] is None and node["previous"]["choice"] == "A"
    with pytest.raises(P.Conflict):
        store.decide(P.USER, "s1", "d1", 1, choice="A")
    assert store.decide(P.USER, "s1", "d1", 2, choice="C")["node"]["result"]["choice"] == "C"


def test_decisions_are_checked_per_kind(store):
    plan(store)
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "d1", 1, choice="Z")
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "v1", 1, verdict="failed")       # needs a note
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "s1", 1, choice="A")             # a step is the session's
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "f1", 1, verdict="passed")       # a followup takes an action


def test_closable_when_every_blocker_is_settled(store):
    plan(store)
    store.decide(P.USER, "s1", "d1", 1, choice="A")
    store.patch("s1", "s1", "s2", status="done")
    store.decide(P.USER, "s1", "v1", 1, verdict="failed", note="button missing")
    p = store.get("s1")
    assert [b["who"] for b in p["summary"]["blockers"]] == ["session", "user"]
    store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    store.decide(P.USER, "s1", "f1", 1, action="issue")
    p = store.get("s1")
    assert p["summary"]["blockers"][0]["why"] == "followup kept (issue) and not done"
    p = store.patch("s1", "s1", "f1", status="done")["plan"]
    assert p["summary"]["closable"] is True and p["summary"]["waiting"] == 0
    rail = store.rail()["s1"]
    assert rail["closable"] is True and "blockers" not in rail and rail["context"]["project"] == "claunch"


def test_validation(store):
    with pytest.raises(P.PlanError):
        store.set("s1", "s1", 0, title="t", steps=[{"kind": "decision", "title": "x", "options": OPTS[:1]}])
    with pytest.raises(P.PlanError):
        store.set("s1", "s1", 0, title="t", steps=[{"id": "a", "title": "x"}, {"id": "a", "title": "y"}])
    with pytest.raises(P.PlanError):
        store.set("s1", "s1", 0, title="t", closure=[{"kind": "step", "title": "x"}])
    with pytest.raises(P.PlanError):
        store.set("s1", "s1", 0, title="t", steps=[{"title": "x", "status": "finished"}])
    minted = store.set("s1", "s1", 0, title="t", steps=[{"title": "x"}])
    assert minted["steps"][0]["id"].startswith("n-")


def test_clear_and_feedback(store):
    plan(store)
    fb = store.feedback(P.USER, "s1", text="too big", node="s2")
    assert fb["node"]["id"] == "s2"
    with pytest.raises(P.Conflict):
        store.clear("s1", "s1", 5)
    store.clear("s1", "s1", 1)
    assert store.get("s1") is None
