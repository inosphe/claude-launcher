"""``claunch gate plan-waiting`` (claunch-rzepk).

The workflow used to decide two things with one input: whether a run asks
for coding approval at all, and whether the plan panel still owed the user a
decision. A round whose plan held an unchosen decision went straight to the
coding-approval gate and then into work, so the person read the same plan in
two surfaces and the gate asked about a plan the panel was still waiting on.

This gate is the second question on its own: it reads the summary the plan
store already computes (``plan.summary.waiting``) and answers in the three
values a ``wait`` probe consumes:

    0  nothing waits -- go on to the gate
    1  the user still owes the plan a decision -- keep waiting
    2  could not be told (no plan, no store, an unreadable row)

What is pinned here is the one distinction the whole order hangs on:
**2 is never spelled 0**. The confirmed rule reads "cannot read it" as
"stop" (docs/plan-coding-approval.md 규칙 1), so folding 2 into 0 would let
a round walk past a plan it could not even find. The gate also must not
write: it runs every ``poll`` seconds beside the daemon's own writers while
the user reads the same row, so it opens the file read-only.

The last test reads the summary through the real store rather than a
hand-built row, so a change to what ``waiting`` counts reaches this file.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import plan_waiting as gate  # noqa: E402  (the tools/ entry, as a gate runs it)

from claude_launcher.daemon import paths  # noqa: E402
from claude_launcher.plan import store as plan_store  # noqa: E402


# --------------------------------------------------------------------------- #
# the exit codes
# --------------------------------------------------------------------------- #
def test_a_plan_with_nothing_on_the_user_is_zero():
    assert gate.verdict({"waiting": 0}) == gate.CLEAR == 0


def test_an_unchosen_decision_is_one():
    assert gate.verdict({"waiting": 1}) == gate.WAITING == 1
    assert gate.verdict({"waiting": 3}) == gate.WAITING


def test_no_plan_is_not_clear():
    """A session that cannot show its plan has not shown that the user owes
    it nothing; the workflow's rule reads an unknown value as "stop"."""
    assert gate.verdict(None) == gate.CANNOT_TELL == 2


def test_a_summary_that_is_not_a_number_is_not_clear():
    assert gate.verdict({"waiting": "many"}) == gate.CANNOT_TELL
    assert gate.verdict({}) == gate.CANNOT_TELL


# --------------------------------------------------------------------------- #
# the store, read as the daemon writes it
# --------------------------------------------------------------------------- #
def _plan(session="s1", steps=None, closure=None):
    """Write a real plan through the store the daemon and the API use."""
    path = paths.daemon_dir() / gate.DB_NAME
    store = plan_store.PlanStore(path)
    store.set(session, session, 0, title="round", steps=steps or [],
              closure=closure or [])


def test_the_gate_reads_what_the_store_computes_for_an_unchosen_decision():
    doc = _plan(steps=[
        {"id": "c1", "kind": "decision", "title": "어느 쪽으로 갈까",
         "options": [{"label": "가", "effect": "a", "risk": "r"},
                     {"label": "나", "effect": "b", "risk": "r"}]},
        {"id": "s1", "kind": "step", "title": "구현", "status": "todo"},
    ])
    summary, why = gate.read_summary(gate.db_path(), "s1")
    assert why == "" and summary["waiting"] == 1
    assert gate.verdict(summary) == gate.WAITING
    assert gate._what_waits(summary) == ["어느 쪽으로 갈까"]


def test_a_plan_of_steps_alone_is_clear():
    _plan(steps=[{"id": "s1", "kind": "step", "title": "구현", "status": "doing"}])
    summary, why = gate.read_summary(gate.db_path(), "s1")
    assert why == "" and summary["waiting"] == 0
    assert gate.verdict(summary) == gate.CLEAR


def test_a_session_without_a_plan_is_cannot_tell():
    _plan("s1")
    summary, why = gate.read_summary(gate.db_path(), "other")
    assert summary is None and "no plan" in why
    assert gate.verdict(summary) == gate.CANNOT_TELL


def test_a_summary_without_a_count_is_cannot_tell():
    """The store's own checks make this row impossible today; the gate is a
    probe on a file another process writes, so a damaged row must still
    answer 2 instead of reading as "nothing waits"."""
    _plan("s1")
    db = sqlite3.connect(gate.db_path())
    try:
        db.execute(
            "INSERT INTO plans (session, revision, doc, summary, created_at, updated_at) "
            "VALUES ('s9', 1, '{}', '[]', 'now', 'now')")
        db.commit()
    finally:
        db.close()
    summary, why = gate.read_summary(gate.db_path(), "s9")
    assert summary is None and "no waiting count" in why
    assert gate.verdict(summary) == gate.CANNOT_TELL


def test_the_gate_does_not_create_the_database():
    """Read-only: a probe must never be the one that changes what the user is
    looking at, and a missing file answers 2 rather than being created."""
    assert gate.db_path() is None
    assert gate.main(["--session", "s1"]) == gate.CANNOT_TELL
    assert gate.db_path() is None


def test_no_session_is_cannot_tell(monkeypatch, capsys):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    assert gate.main([]) == gate.CANNOT_TELL
    assert "no session" in capsys.readouterr().err


def test_the_one_line_names_the_decisions_that_wait():
    _plan(steps=[
        {"id": "c1", "kind": "decision", "title": "어느 쪽으로 갈까",
         "options": [{"label": "가", "effect": "a", "risk": "r"},
                     {"label": "나", "effect": "b", "risk": "r"}]},
    ])
    summary, _ = gate.read_summary(gate.db_path(), "s1")
    assert gate._render(summary, gate.WAITING) == "plan waiting: 1: 어느 쪽으로 갈까"


def test_main_reads_the_session_it_is_given_and_exits_with_the_verdict(capsys):
    _plan("s1", steps=[
        {"id": "c1", "kind": "decision", "title": "어느 쪽으로 갈까",
         "options": [{"label": "가", "effect": "a", "risk": "r"},
                     {"label": "나", "effect": "b", "risk": "r"}]},
    ])
    assert gate.main(["--session", "s1"]) == gate.WAITING
    assert capsys.readouterr().out.startswith("plan waiting: 1")

    assert gate.main(["--session", "s1", "--json"]) == gate.WAITING
    assert json.loads(capsys.readouterr().out)["waiting"] == 1
