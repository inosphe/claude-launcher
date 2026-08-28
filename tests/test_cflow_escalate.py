"""A step handing the slot to another workflow: ``escalate``.

A run that finishes normally goes quiet and the daemon returns its session's
slot. ``escalate`` on the step a run ends through says that this particular
ending continues elsewhere: the engine files an ordinary start request for the
named workflow, and everything the request already did follows from that — the
pending request spares the session from kill-on-end, the done payload names
the start its driver must perform, and the request carries the session's beads
issue so one board record spans both runs.

The checks are the other half. They run on THIS side of the ending, where
declining costs a journal line: at start time the declaration is reported
(``escalation_check``), and when the escalation would fire the target is
resolved and held against ``filter_roles``. A refused target is not escalated
to — the run ends the ordinary way — and "refused", "approved" and "could not
be asked" are three answers, not two.
"""

from __future__ import annotations

import pytest

from claude_launcher import daemon_client
from claude_launcher.cflow import engine, model, responders, state as state_mod


PLAIN = """
name: plainflow
steps:
  one:
    instructions: do the work
"""

ESCALATING = """
name: workerflow
steps:
  one:
    instructions: do the work
    next: wrapup
  wrapup:
    instructions: wrap up
    escalate:
      workflow: midflow
      context: the follow-up needs a stack
"""

SHORTHAND = """
name: shortflow
steps:
  one:
    instructions: do the work
    escalate: midflow
"""

RECUR_AND_ESCALATE = """
name: recurflow
recur: true
steps:
  one:
    instructions: do the round
    escalate: midflow
"""

MISSING_TARGET = """
name: brokenflow
steps:
  one:
    instructions: do the work
    escalate: nosuchflow
"""

MID = """
name: midflow
filter_roles:
  type: whitelist
  roles: [worker]
steps:
  one:
    instructions: manage the stack
"""

FILES = {
    "plainflow": PLAIN,
    "workerflow": ESCALATING,
    "shortflow": SHORTHAND,
    "recurflow": RECUR_AND_ESCALATE,
    "brokenflow": MISSING_TARGET,
    "midflow": MID,
}


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    for name, text in FILES.items():
        (d / ".claunch" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return d


@pytest.fixture(autouse=True)
def no_daemon(monkeypatch):
    """Default: nothing to ask. Tests that need an answer install their own."""
    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (None, "down")
    )
    monkeypatch.setattr(daemon_client, "unreachable_reason", lambda why: "daemon is down")


def _events(**match) -> list:
    return [
        e for e in state_mod.read_journal()
        if all(e.get(k) == v for k, v in match.items())
    ]


def _finish(workflow: str, *, context: str = "", mesh: str = "") -> dict:
    """Run a workflow to its end, reporting at every step."""
    payload = engine.start(workflow, context=context, mesh=mesh)
    while payload.get("status") not in ("done", "aborted"):
        engine.report("step done")
        payload = engine.next_step()
    return payload


class _Reach:
    """A stand-in for ``responders.pool``'s answer about the driving session."""

    def __init__(self, *, me="s247", role="worker", problem=None, mesh="m1"):
        self.me, self.me_role, self.problem, self.mesh = me, role, problem, mesh


def _role(monkeypatch, **kw) -> None:
    monkeypatch.setattr(responders, "pool", lambda **_: _Reach(**kw))


def _issue(monkeypatch, issue: str | None) -> None:
    """Make the daemon answer ``GET /api/sessions/{name}`` with this issue."""
    class _Client:
        def get(self, path, timeout=None):
            # `checkout.check` asks the same client for the session list; only
            # the single-session route is this fixture's business.
            if path == f"/api/sessions/{'s247'}":
                return {"name": "s247", "issue": issue}
            return {"sessions": []}

    monkeypatch.setattr(
        daemon_client, "connect_with_diagnosis", lambda *a, **k: (_Client(), None)
    )
    monkeypatch.setenv(state_mod.SESSION_ENV, "s247")


# --------------------------------------------------------------------------- #
# nothing declared: the ending is exactly what it was
# --------------------------------------------------------------------------- #
def test_a_workflow_that_declares_nothing_ends_with_no_request(proj):
    done = _finish("plainflow")
    assert done["status"] == "done"
    assert "pending_start" not in done
    assert state_mod.read_request() is None
    assert not _events(event="escalate_requested")


def test_a_workflow_that_declares_nothing_reports_no_escalation_check(proj):
    started = engine.start("plainflow")
    assert "escalation_check" not in started


# --------------------------------------------------------------------------- #
# the hand-off itself
# --------------------------------------------------------------------------- #
def test_ending_on_an_escalating_step_files_the_start_request(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("workerflow", context="round context", mesh="m1")

    assert done["status"] == "done"
    pending = done["pending_start"]
    assert pending["by"] == "escalate"
    assert pending["name"] == "midflow"
    assert pending["from_workflow"] == "workerflow"
    assert pending["from_step"] == "wrapup"
    assert pending["mesh"] == "m1"
    # The same record a human's `claunch cflow request` leaves, on disk.
    assert state_mod.read_request()["id"] == pending["id"]


def test_the_pending_request_is_what_keeps_the_session_alive(proj, monkeypatch):
    """The daemon's kill-on-end spares a done run with a pending start.

    Not asserted through the clock here — asserted at the field the clock
    reads (``pending_start.by``), which is non-empty exactly when the
    escalation was filed. An empty ``by`` is what ends the session.
    """
    _role(monkeypatch)
    done = _finish("workerflow")
    assert done["pending_start"]["by"] == "escalate"


def test_the_done_note_names_the_workflow_to_start(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("workerflow")
    assert "escalates to" in done["note"]
    assert "midflow" in done["note"]


def test_the_escalation_is_not_the_daemon_auto_start_path(proj, monkeypatch):
    """``auto_start_next_round`` admits ``by: recur`` only."""
    _role(monkeypatch)
    _finish("workerflow")
    pending = state_mod.read_request()
    assert pending["by"] != "recur"
    assert "auto" not in pending


def test_the_shorthand_names_the_workflow(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("shortflow")
    assert done["pending_start"]["name"] == "midflow"


# --------------------------------------------------------------------------- #
# what the request carries
# --------------------------------------------------------------------------- #
def test_the_request_carries_the_session_issue_in_its_context(proj, monkeypatch):
    _role(monkeypatch)
    _issue(monkeypatch, "claunch-l2a8")
    done = _finish("workerflow", context="round context")

    pending = done["pending_start"]
    assert pending["issue"] == "claunch-l2a8"
    assert pending["context"].startswith("issue: claunch-l2a8")
    # And the declaration's own words, and the finishing run's context.
    assert "the follow-up needs a stack" in pending["context"]
    assert "round context" in pending["context"]


def test_a_run_outside_a_managed_session_records_why_it_has_no_issue(
    proj, monkeypatch
):
    """DEFAULT_SCOPE has no session record to hold an issue — and says so."""
    _role(monkeypatch)
    _finish("workerflow")

    entry = _events(event="escalate_requested")[0]
    assert entry["issue"] is None
    assert "not a managed session" in entry["issue_problem"]


def test_a_session_with_no_issue_is_not_an_unasked_question(proj, monkeypatch):
    _role(monkeypatch)
    _issue(monkeypatch, None)
    _finish("workerflow")

    entry = _events(event="escalate_requested")[0]
    assert entry["issue"] is None
    assert "issue_problem" not in entry


def test_an_unreachable_daemon_is_recorded_rather_than_read_as_no_issue(
    proj, monkeypatch
):
    _role(monkeypatch)  # the daemon fixture leaves the lookup unanswerable
    monkeypatch.setenv(state_mod.SESSION_ENV, "s247")
    _finish("workerflow")

    entry = _events(event="escalate_requested")[0]
    assert entry["issue"] is None
    assert "daemon is down" in entry["issue_problem"]


def test_the_context_carries_over_when_nothing_else_does(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("shortflow", context="carry me")
    assert done["pending_start"]["context"] == "carry me"


# --------------------------------------------------------------------------- #
# the role check: three answers, not two
# --------------------------------------------------------------------------- #
def test_a_target_that_admits_this_session_is_escalated_to(proj, monkeypatch):
    _role(monkeypatch, role="worker")
    done = _finish("workerflow")
    assert done["pending_start"]["by"] == "escalate"
    assert "role_filter" not in done["pending_start"]


def test_a_target_that_turns_this_session_away_is_not_escalated_to(proj, monkeypatch):
    _role(monkeypatch, role="reviewer")
    done = _finish("workerflow")

    assert done["status"] == "done"
    assert "pending_start" not in done
    assert state_mod.read_request() is None
    declined = _events(event="escalate_declined")
    assert len(declined) == 1
    assert "filter_roles" in declined[0]["reason"]


def test_an_unenforceable_check_escalates_and_says_so(proj, monkeypatch):
    """No mesh identity is not a refusal — but it is not an approval either."""
    _role(monkeypatch, me="", problem="no membership")
    done = _finish("workerflow")

    pending = done["pending_start"]
    assert pending["by"] == "escalate"
    assert "could not be enforced" in pending["role_filter"]
    assert "could not be enforced" in _events(event="escalate_requested")[0]["role_filter"]


def test_a_target_that_does_not_exist_is_declined(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("brokenflow")

    assert "pending_start" not in done
    declined = _events(event="escalate_declined")
    assert "could not be loaded" in declined[0]["reason"]


# --------------------------------------------------------------------------- #
# alongside `recur`
# --------------------------------------------------------------------------- #
def test_an_escalation_wins_over_recur(proj, monkeypatch):
    _role(monkeypatch)
    done = _finish("recurflow")
    assert done["pending_start"]["by"] == "escalate"


def test_a_declined_escalation_falls_back_to_the_next_round(proj, monkeypatch):
    """A refused hand-off must not also silence a service loop."""
    _role(monkeypatch, role="reviewer")
    done = _finish("recurflow")

    pending = done["pending_start"]
    assert pending["by"] == "recur"
    assert pending["round"] == 2
    assert _events(event="escalate_declined")


# --------------------------------------------------------------------------- #
# the declaration, reported where it is read
# --------------------------------------------------------------------------- #
def test_start_reports_what_the_declaration_resolves_to(proj, monkeypatch):
    _role(monkeypatch, role="worker")
    started = engine.start("workerflow", mesh="m1")

    check = started["escalation_check"]
    assert check["steps"][0]["step"] == "wrapup"
    assert check["steps"][0]["name"] == "midflow"
    assert "whitelist" in check["steps"][0]["filter_roles"]
    assert check["steps"][0]["role_check"] == "admits 'worker'"
    assert "1 escalation(s) declared" in check["note"]


def test_the_report_separates_the_target_from_the_role_question(proj, monkeypatch):
    """Resolving the file needs no daemon; the role does. Two fields."""
    _role(monkeypatch, me="", problem="no membership")
    started = engine.start("workerflow")

    entry = started["escalation_check"]["steps"][0]
    assert entry["resolves"].endswith("midflow.yaml")   # answered from files
    assert entry["role_check"].startswith("unchecked:")  # answered by the daemon


def test_the_report_names_a_target_that_would_refuse(proj, monkeypatch):
    _role(monkeypatch, role="reviewer")
    started = engine.start("workerflow")

    check = started["escalation_check"]
    assert check["steps"][0]["role_check"].startswith("would be declined")
    assert "would be declined by the target's filter_roles" in check["note"]


def test_the_report_names_a_target_that_does_not_exist(proj, monkeypatch):
    _role(monkeypatch)
    started = engine.start("brokenflow")

    check = started["escalation_check"]
    assert "does not resolve to a workflow" in check["steps"][0]["problem"]
    assert "name(s) no workflow" in check["note"]


def test_request_start_reports_the_declaration_too(proj, monkeypatch):
    _role(monkeypatch)
    result = engine.request_start("workerflow", by="human")
    assert result["escalation_check"]["steps"][0]["name"] == "midflow"


# --------------------------------------------------------------------------- #
# the schema
# --------------------------------------------------------------------------- #
def test_the_mapping_and_the_shorthand_parse_to_the_same_target():
    long = model.parse(ESCALATING).steps["wrapup"].escalate
    short = model.parse(SHORTHAND).steps["one"].escalate
    assert long.workflow == short.workflow == "midflow"
    assert long.context == "the follow-up needs a stack"
    assert short.context is None


def test_an_escalation_needs_a_workflow():
    with pytest.raises(model.WorkflowError, match="needs a 'workflow'"):
        model.parse(
            "name: x\nsteps:\n  one:\n    instructions: go\n    escalate: {context: hi}\n"
        )


def test_an_unknown_key_is_refused():
    with pytest.raises(model.WorkflowError, match="unknown key"):
        model.parse(
            "name: x\nsteps:\n  one:\n    instructions: go\n"
            "    escalate: {workflow: midflow, recur: true}\n"
        )


def test_an_escalation_on_a_step_that_cannot_end_is_refused():
    """It fires when the run ENDS there; a step that never ends never reads it."""
    with pytest.raises(model.WorkflowError, match="never terminates"):
        model.parse(
            "name: x\nsteps:\n"
            "  one:\n    instructions: go\n    escalate: midflow\n    next: two\n"
            "  two:\n    instructions: stop\n"
        )


def test_a_select_step_may_escalate_through_an_ending_option():
    workflow = model.parse(
        "name: x\nsteps:\n"
        "  one:\n"
        "    escalate: midflow\n"
        "    select:\n"
        "      prompt: done?\n"
        "      chooser: agent\n"
        "      options:\n"
        "        stop: {description: end here, next: end}\n"
        "        again: {description: keep going, next: one}\n"
    )
    assert workflow.steps["one"].escalate.workflow == "midflow"
