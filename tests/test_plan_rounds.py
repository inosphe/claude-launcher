"""Plan rounds (plan/store.py, claunch-r3j1o.1): ``new_round`` seals the
current plan and opens the round's own, idempotent on (run, round); a sealed
round's decisions never answer the next round; what the user still owes is
carried over with its round; ``clear`` seals too."""

import pytest

from claude_launcher.plan import store as P

OPTS = [{"label": "A", "effect": "fast", "risk": "untested", "recommended": True},
        {"label": "B", "effect": "safe", "risk": "slow"}]
STEPS = [{"id": "s1", "title": "read code", "status": "done"},
         {"id": "d1", "kind": "decision", "title": "storage", "options": OPTS}]


@pytest.fixture
def store(tmp_path):
    return P.PlanStore(tmp_path / "plan.sqlite3")


def write(store, steps=STEPS, closure=None, title="Ship X"):
    rev = (store.get("s1") or {}).get("revision", 0)
    return store.set("s1", "s1", rev, title=title, steps=steps, closure=closure or [])


def node(plan, nid):
    return P._index(plan)[nid]


def test_the_first_round_opens_an_empty_plan_of_that_round(store):
    res = store.new_round("s1", "s1", run="r1", round=1, issue="c-1")
    assert res["opened"] is True and res["sealed"] is None and res["carried"] == []
    p = res["plan"]
    assert (p["run"], p["round"], p["issue"], p["steps"], p["revision"]) == ("r1", 1, "c-1", [], 1)
    assert store.rounds("s1") == {"session": "s1", "rounds": [],
                                  "current": {"run": "r1", "round": 1, "key": "r1/1", "title": "round 1",
                                              "revision": 1}}


def test_the_same_round_again_changes_nothing(store):
    store.new_round("s1", "s1", run="r1", round=1)
    p = write(store)
    again = store.new_round("s1", "s1", run="r1", round=1)
    assert again["opened"] is False and "already round r1/1" in again["why"]
    assert store.get("s1")["revision"] == p["revision"]
    assert store.rounds("s1")["rounds"] == []


def test_a_new_round_seals_the_old_one_and_keeps_its_results(store):
    store.new_round("s1", "s1", run="r1", round=1)
    write(store)
    decided = store.decide(P.USER, "s1", "d1", 1, choice="B")["plan"]
    assert decided["run"] == "r1" and decided["round"] == 1   # decide keeps the round
    res = store.new_round("s1", "s1", run="r1", round=2)
    assert res["opened"] is True and res["sealed"] == {"run": "r1", "round": 1, "key": "r1/1"}
    cur = res["plan"]
    assert cur["round"] == 2 and cur["steps"] == [] and cur["revision"] == decided["revision"] + 1

    listed = store.rounds("s1")
    assert [r["key"] for r in listed["rounds"]] == ["r1/1"]
    assert listed["rounds"][0]["steps"] == 1 and "blockers" not in listed["rounds"][0]
    old = store.get_round("s1", "r1/1")
    assert old["sealed"] is True and old["round"] == 1
    assert node(old, "d1")["result"]["choice"] == "B"


def test_a_decision_of_the_old_round_does_not_answer_the_new_one(store):
    """The same id and the same text in the next round still wait on the user."""
    store.new_round("s1", "s1", run="r1", round=1)
    write(store)
    store.decide(P.USER, "s1", "d1", 1, choice="A")
    store.new_round("s1", "s1", run="r1", round=2)
    p = write(store)
    assert node(p, "d1")["result"] is None and node(p, "d1")["rev"] == 1
    assert p["summary"]["waiting"] == 1


def test_a_stale_write_from_the_old_round_is_refused(store):
    store.new_round("s1", "s1", run="r1", round=1)
    p = write(store)
    store.new_round("s1", "s1", run="r1", round=2)
    with pytest.raises(P.Conflict):
        store.set("s1", "s1", p["revision"], title="late", steps=STEPS)


def test_what_the_user_still_owes_is_carried_with_its_round(store):
    store.new_round("s1", "s1", run="r1", round=1)
    write(store, steps=[{"id": "s1", "title": "x", "status": "done"}], closure=[
        {"id": "v-open", "kind": "verify", "title": "check A"},
        {"id": "v-fail", "kind": "verify", "title": "check B"},
        {"id": "v-pass", "kind": "verify", "title": "check C"},
        {"id": "f-open", "kind": "followup", "title": "later D"},
        {"id": "f-drop", "kind": "followup", "title": "later E"},
        {"id": "f-kept", "kind": "followup", "title": "later F"},
        {"id": "f-done", "kind": "followup", "title": "later G"},
    ])
    store.decide(P.USER, "s1", "v-fail", 1, verdict="failed", note="broken")
    store.decide(P.USER, "s1", "v-pass", 1, verdict="passed")
    store.decide(P.USER, "s1", "f-drop", 1, action="drop")
    store.decide(P.USER, "s1", "f-kept", 1, action="here")
    store.decide(P.USER, "s1", "f-done", 1, action="issue")
    store.patch("s1", "s1", "f-done", status="done")

    res = store.new_round("s1", "s1", run="r1", round=2)
    assert res["carried"] == ["v-open", "v-fail", "f-open", "f-kept"]
    closure = {n["id"]: n for n in res["plan"]["closure"]}
    assert all(n["from"] == {"run": "r1", "round": 1} for n in closure.values())
    assert closure["v-fail"]["result"]["verdict"] == "failed"
    # the unchecked verify and the undisposed followup wait on the user in round 2
    assert res["plan"]["summary"]["waiting"] == 2


def test_a_plain_write_keeps_a_carried_node_until_it_is_settled(store):
    store.new_round("s1", "s1", run="r1", round=1)
    write(store, closure=[{"id": "f1", "kind": "followup", "title": "later"}])
    store.new_round("s1", "s1", run="r1", round=2)
    p = write(store, steps=[{"id": "n1", "title": "new"}])
    assert [n["id"] for n in p["closure"]] == ["f1"] and p["closure"][0]["from"]["round"] == 1
    # rewritten by the session with the same id and text: the mark stays
    p = write(store, steps=[{"id": "n1", "title": "new"}],
              closure=[{"id": "f1", "kind": "followup", "title": "later"}])
    assert p["closure"][0]["from"] == {"run": "r1", "round": 1}
    store.decide(P.USER, "s1", "f1", 1, action="drop")
    p = write(store, steps=[{"id": "n1", "title": "new"}])
    assert p["closure"] == []


def test_a_late_call_for_a_sealed_round_does_not_overwrite_the_next(store):
    store.new_round("s1", "s1", run="r1", round=1)
    store.new_round("s1", "s1", run="r1", round=2)
    write(store)
    late = store.new_round("s1", "s1", run="r1", round=1)
    assert late["opened"] is False and "sealed already" in late["why"]
    assert store.get("s1")["round"] == 2 and store.get("s1")["steps"]


def test_a_plan_written_before_rounds_is_sealed_as_unknown(store):
    write(store)
    assert "run" not in store.get("s1")
    res = store.new_round("s1", "s1", run="r1", round=1)
    assert res["sealed"] == {"run": "", "round": None, "key": "/unknown"}
    old = store.get_round("s1", "/unknown")
    assert old["title"] == "Ship X" and old["round"] is None


def test_clear_seals_the_plan_and_the_round_can_open_again(store):
    store.new_round("s1", "s1", run="r1", round=1)
    p = write(store)
    out = store.clear("s1", "s1", p["revision"])
    assert out["sealed"]["key"] == "r1/1" and store.get("s1") is None
    again = store.new_round("s1", "s1", run="r1", round=1)
    assert again["opened"] is True and again["plan"]["revision"] == 1
    assert store.get_round("s1", "r1/1")["steps"]


def test_only_the_session_opens_its_rounds(store):
    with pytest.raises(P.Forbidden):
        store.new_round(P.USER, "s1", run="r1", round=1)
    with pytest.raises(P.Forbidden):
        store.new_round("s2", "s1", run="r1", round=1)
    with pytest.raises(P.PlanError):
        store.new_round("s1", "s1", run="", round=1)
    with pytest.raises(P.PlanError):
        store.new_round("s1", "s1", run="r1", round=True)
    with pytest.raises(P.PlanError):
        store.get_round("s1", "no-slash")
    assert store.get_round("s1", "r1/9") is None


def test_patch_keeps_the_round(store):
    store.new_round("s1", "s1", run="r1", round=3)
    write(store)
    out = store.patch("s1", "s1", "s1", status="doing")["plan"]
    assert (out["run"], out["round"]) == ("r1", 3)
