"""What each plan gate waits on, and which results a rewrite keeps
(claunch-jw1v2, th-2a4fcd, th-a068b8, th-62379d).

* The start gates in front of ``work`` wait only on unchosen decisions
  (``summary.waiting_decisions``, ``plan-waiting --scope start``): a verify
  item left from the round before checks work that already landed, and the
  settle closure gate is the one that waits on it.
* A passed or deferred check and a disposed followup keep their result when
  the session only rewords them (s6451: a detail edit put a passed check
  back in front of the user). Asking again is ``patch(reopen=<reason>)``.
  A failed check still asks again when reworded.
* ``deferred`` (보류) waits on nobody and keeps nothing open.
* A followup the session wrote ``done`` itself waits on nobody; a ``note``
  never waits and never keeps the session open.
  The user may still pick ``here`` or ``issue`` on a note (claunch-i0whx);
  the pick gates nothing.
* A stop is told once per distinct set of items (``--notify``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import plan_waiting as gate  # noqa: E402  (the tools/ entry, as a gate runs it)

from claude_launcher.daemon import paths  # noqa: E402
from claude_launcher.plan import store as P  # noqa: E402

OPTS = [{"label": "A", "effect": "a", "risk": "r", "recommended": True},
        {"label": "B", "effect": "b", "risk": "r"}]
ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "src" / "claude_launcher" / "workflows" / "v2" / "improv-worker-v2.yaml"


@pytest.fixture
def store(tmp_path):
    return P.PlanStore(tmp_path / "plan.sqlite3")


def _write(store, rev, *, steps=(), closure=()):
    return store.set("s1", "s1", rev, title="round", steps=list(steps), closure=list(closure))


VERIFY = {"id": "v1", "kind": "verify", "title": "open the pane", "detail": "click it"}
DECISION = {"id": "d1", "kind": "decision", "title": "which way", "options": OPTS}


# -- the start scope -------------------------------------------------------- #
def test_a_left_over_verify_does_not_count_for_the_start(store):
    p = _write(store, 0, closure=[VERIFY])
    s = p["summary"]
    assert s["waiting"] == 1 and s["waiting_decisions"] == 0
    assert gate.verdict(s, "start") == gate.CLEAR
    assert gate.verdict(s, "settle") == gate.WAITING
    assert gate.verdict(s) == gate.WAITING   # the default is the old answer


def test_an_unchosen_decision_still_holds_the_start(store):
    s = _write(store, 0, steps=[DECISION], closure=[VERIFY])["summary"]
    assert s["waiting_decisions"] == 1
    assert gate.verdict(s, "start") == gate.WAITING
    assert gate._what_waits(s, "start") == ["d1 which way"]


def test_a_row_without_the_new_count_is_read_from_its_blockers():
    rows = [{"id": "v1", "kind": "verify", "title": "t", "why": "not checked yet", "who": "user"}]
    assert gate.waiting_count({"waiting": 1, "blockers": rows}, "start") == 0
    rows.append({"id": "d1", "kind": "decision", "title": "t", "why": "decision not taken", "who": "user"})
    assert gate.waiting_count({"waiting": 2, "blockers": rows}, "start") == 1
    # no count and no blockers: it has not shown the decisions are taken
    assert gate.verdict({"waiting": 1}, "start") == gate.CANNOT_TELL


def test_main_takes_the_scope(capsys):
    db = P.PlanStore(paths.daemon_dir() / gate.DB_NAME)
    db.set("s1", "s1", 0, title="round", closure=[VERIFY])
    assert gate.main(["--session", "s1", "--scope", "start"]) == gate.CLEAR
    assert "1 closure item(s) wait for the settle gate" in capsys.readouterr().out
    assert gate.main(["--session", "s1"]) == gate.WAITING


# -- results a rewrite keeps ------------------------------------------------ #
def test_a_passed_check_survives_a_reworded_detail(store):
    _write(store, 0, closure=[VERIFY])
    store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    p = _write(store, 2, closure=[{**VERIFY, "detail": "click it -- checked last round"}])
    v1 = p["closure"][0]
    assert v1["result"]["verdict"] == "passed" and v1["rev"] == 1
    assert p["summary"]["waiting"] == 0
    p = store.patch("s1", "s1", "v1", title="open the pane (round 1)")["plan"]
    assert p["closure"][0]["result"]["verdict"] == "passed"


def test_the_rewrite_that_kept_a_result_is_recorded(store):
    _write(store, 0, closure=[VERIFY])
    store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    _write(store, 2, closure=[{**VERIFY, "detail": "changed"}])
    with store._connect() as db:
        rows = [json.loads(r["data"]) for r in db.execute(
            "SELECT data FROM events WHERE session = 's1' AND kind = 'set' ORDER BY seq")]
    assert rows[-1].get("reworded_kept") == ["v1"]


def test_a_failed_check_asks_again_when_reworded(store):
    _write(store, 0, closure=[VERIFY])
    store.decide(P.USER, "s1", "v1", 1, verdict="failed", note="broken")
    p = store.patch("s1", "s1", "v1", detail="click it -- fixed")["plan"]
    v1 = p["closure"][0]
    assert v1["result"] is None and v1["rev"] == 2 and v1["previous"]["verdict"] == "failed"


def test_a_disposed_followup_survives_a_reword(store):
    _write(store, 0, closure=[{"id": "f1", "kind": "followup", "title": "more"}])
    store.decide(P.USER, "s1", "f1", 1, action="issue")
    p = store.patch("s1", "s1", "f1", detail="filed claunch-xyz", status="done")["plan"]
    f1 = p["closure"][0]
    assert f1["result"]["action"] == "issue" and f1["status"] == "done"
    assert p["summary"]["closable"] is True


def test_reopen_asks_again_with_the_reason(store):
    _write(store, 0, closure=[VERIFY])
    store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    p = store.patch("s1", "s1", "v1", reopen="the pane code changed")["plan"]
    v1 = p["closure"][0]
    assert v1["result"] is None and v1["rev"] == 2
    assert v1["previous"]["verdict"] == "passed" and v1["reopened"]["reason"] == "the pane code changed"
    assert p["summary"]["waiting"] == 1
    # the old rev no longer decides; the new one does and clears the reason
    with pytest.raises(P.Conflict):
        store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    out = store.decide(P.USER, "s1", "v1", 2, verdict="passed")["node"]
    assert "reopened" not in out


def test_reopen_needs_a_settled_closure_node_and_a_reason(store):
    _write(store, 0, steps=[DECISION], closure=[VERIFY])
    with pytest.raises(P.PlanError):
        store.patch("s1", "s1", "v1", reopen="x")          # nothing checked yet
    with pytest.raises(P.PlanError):
        store.patch("s1", "s1", "d1", reopen="x")          # a decision rewords instead
    store.decide(P.USER, "s1", "v1", 1, verdict="passed")
    with pytest.raises(P.PlanError):
        store.patch("s1", "s1", "v1", reopen="")


# -- deferred, session-done followups, notes -------------------------------- #
def test_a_deferred_check_waits_on_nobody(store):
    _write(store, 0, closure=[VERIFY])
    p = store.decide(P.USER, "s1", "v1", 1, verdict="deferred")["plan"]
    s = p["summary"]
    assert s["waiting"] == 0 and s["closable"] is True and s["deferred"] == 1
    # the session writes the issue id into it; the result stays
    p = store.patch("s1", "s1", "v1", detail="click it -- claunch-89jtx")["plan"]
    assert p["closure"][0]["result"]["verdict"] == "deferred"


def test_a_followup_the_session_closed_itself_waits_on_nobody(store):
    p = _write(store, 0, closure=[{"id": "f1", "kind": "followup", "title": "this round's work",
                                    "status": "done"}])
    assert p["summary"]["waiting"] == 0 and p["summary"]["closable"] is True
    p = _write(store, 1, closure=[{"id": "f1", "kind": "followup", "title": "this round's work"}])
    assert p["summary"]["waiting"] == 1


def test_a_note_never_waits_and_takes_no_decision(store):
    p = _write(store, 0, closure=[{"id": "n1", "kind": "note", "title": "could also cache X"}])
    s = p["summary"]
    assert s["notes"] == 1 and s["waiting"] == 0 and s["closable"] is True
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "n1", 1, verdict="passed")
    with pytest.raises(P.PlanError):
        _write(store, 1, closure=[{"id": "n1", "kind": "note", "title": "t", "status": "done"}])


@pytest.mark.parametrize("action", ["here", "issue"])
def test_a_note_takes_an_optional_pick_that_gates_nothing(store, action):
    """claunch-i0whx (th-3e4e9e): the user may take a note into this round
    or hand it over as an issue; picking does not make it wait or block."""
    _write(store, 0, closure=[{"id": "n1", "kind": "note", "title": "could also cache X"}])
    p = store.decide(P.USER, "s1", "n1", 1, action=action)["plan"]
    assert p["closure"][0]["result"]["action"] == action
    s = p["summary"]
    assert s["waiting"] == 0 and s["closable"] is True and s["blockers"] == []
    # the session writes what became of it into the detail; the pick stays
    p = store.patch("s1", "s1", "n1", detail="filed as claunch-x")["plan"]
    assert p["closure"][0]["result"]["action"] == action and p["summary"]["closable"] is True


def test_a_note_has_no_drop_and_takes_only_an_action(store):
    _write(store, 0, closure=[{"id": "n1", "kind": "note", "title": "could also cache X"}])
    with pytest.raises(P.PlanError, match="here, issue"):
        store.decide(P.USER, "s1", "n1", 1, action="drop")
    with pytest.raises(P.PlanError):
        store.decide(P.USER, "s1", "n1", 1, choice="A")


# -- the stop notice --------------------------------------------------------- #
class _Client:
    def __init__(self, fail=False):
        self.posts = []
        self.fail = fail

    def post(self, path, body=None, **_kw):
        if self.fail:
            raise RuntimeError("daemon down")
        self.posts.append((path, body))
        if path == "/api/threads":
            return {"thread": {"id": "th-1"}}
        return {"message": {}}


def _summary(*ids):
    rows = [{"id": i, "kind": "decision", "title": i, "why": "decision not taken", "who": "user"} for i in ids]
    return {"waiting": len(rows), "waiting_decisions": len(rows), "blockers": rows}


def test_a_stop_is_told_once_per_set_of_items(tmp_path):
    state = tmp_path / "notice.json"
    c = _Client()
    assert gate.notify_stall("s1", _summary("d1"), gate.WAITING, "start", client=c, state_path=state) == "posted"
    assert c.posts[0][0] == "/api/threads" and c.posts[0][1]["title"] == gate.NOTICE_TITLE
    assert "`d1`" in c.posts[0][1]["text"]
    assert gate.notify_stall("s1", _summary("d1"), gate.WAITING, "start", client=c, state_path=state) == "same"
    assert len(c.posts) == 1
    # a new item is a new stop: the same thread is used
    assert gate.notify_stall("s1", _summary("d1", "d2"), gate.WAITING, "start", client=c,
                             state_path=state) == "posted"
    assert c.posts[1][0] == "/api/threads/th-1/messages"
    # clear forgets it, so the next stop is told again
    assert gate.notify_stall("s1", _summary(), gate.CLEAR, "start", client=c, state_path=state) == "cleared"
    assert gate.notify_stall("s1", _summary("d1"), gate.WAITING, "start", client=c, state_path=state) == "posted"


def test_a_notice_that_cannot_be_sent_changes_nothing(tmp_path):
    state = tmp_path / "notice.json"
    out = gate.notify_stall("s1", _summary("d1"), gate.WAITING, "start", client=_Client(fail=True),
                            state_path=state)
    assert out.startswith("failed:") and not state.exists()
    assert gate.notify_stall("s1", None, gate.CANNOT_TELL, "start", client=_Client(), state_path=state) == "skip"


# -- the workflow ------------------------------------------------------------ #
def test_the_start_gates_wait_on_decisions_and_the_settle_gate_on_everything():
    text = WORKER.read_text(encoding="utf-8")
    probes = [line.strip() for line in text.splitlines() if "probe: [claunch, gate, plan-waiting" in line]
    assert len(probes) == 4
    start = [p for p in probes if "--scope, start" in p]
    assert len(start) == 2
    assert all("--notify" in p for p in probes)
    for gate_id in ("plan-wait-before-gate", "plan-wait-after-seed"):
        at = text.index(f"id: {gate_id}")
        assert "--scope, start" in text[at:at + 300]
    at = text.index("id: closure-wait\n")
    assert "--scope" not in text[at:at + 300]
