"""An off-graph move, asked for by the agent and answered by a person.

The graph is the run's account of what may happen, so the agent does not get
to re-position itself: ``goto`` is a human command and the harness deny rules
keep the agent's shell off it. Reality still out-runs the graph — a merge
turns up work belonging to a step already passed — and before this path
existed that ended as a permission failure: nothing recorded, nobody asked,
and an agent left to either abandon the finding or quietly carry on.

What these pin is the shape of the door, not its politeness:

* filing a request MOVES NOTHING and stops the run, so an approval landing
  later moves the run the person was actually looking at;
* a refusal reaches the agent exactly once as an answer it must act on,
  rather than silently expiring;
* a human who forces some third position answers the request too, instead of
  leaving it behind to stop the run a second time;
* the grant is not reachable from the agent's own surface at all.
"""

from __future__ import annotations

import json

import pytest

from claude_launcher import cli
from claude_launcher.cflow import engine, mcp, state as state_mod
from claude_launcher.cflow.engine import CflowError
from claude_launcher.cflow.model import WorkflowError

from test_cflow import _write, flow_dir  # noqa: F401  (fixture import)

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
    next: three
  three:
    instructions: do three
"""


def _at_two(proj):
    """Start LINEAR and advance to 'two' — the position every case starts from."""
    _write(proj, "linear", LINEAR)
    engine.start("linear")
    engine.report("did one")
    payload = engine.next_step()
    assert payload["step_id"] == "two"
    return payload


def _journal(proj):
    path = state_mod.journal_path(str(proj))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _events(proj, name):
    return [e for e in _journal(proj) if e.get("event") == name]


# --------------------------------------------------------------------------- #
# filing
# --------------------------------------------------------------------------- #
def test_request_records_and_holds_the_run(flow_dir):
    _at_two(flow_dir)
    filed = engine.request_goto("one", "the merge turned up an unhandled case")
    assert filed["status"] == "goto_requested"
    assert filed["goto_request"]["step"] == "one"
    assert filed["goto_request"]["from"] == "two"

    # Nothing moved: that is the whole difference between this and 'goto'.
    assert engine.status()["step_id"] == "two"

    # And the run will not advance past the question. A report is still
    # accepted (evidence for the person deciding), but 'next' stops.
    engine.report("found the case")
    held = engine.next_step()
    assert held["status"] == "waiting_goto"
    assert held["goto_request"]["id"] == filed["goto_request"]["id"]
    assert engine.status()["status"] == "waiting_goto"
    assert engine.status()["step_id"] == "two"


def test_request_needs_a_reason(flow_dir):
    _at_two(flow_dir)
    with pytest.raises(CflowError, match="reason"):
        engine.request_goto("one", "   ")
    assert engine.status()["status"] == "step"


def test_request_refuses_an_unknown_or_current_step(flow_dir):
    _at_two(flow_dir)
    with pytest.raises(WorkflowError):
        engine.request_goto("nowhere", "because")
    with pytest.raises(CflowError, match="already at"):
        engine.request_goto("two", "because")
    assert engine.status()["status"] == "step"


def test_request_refused_on_a_finished_run(flow_dir):
    _write(flow_dir, "linear", LINEAR)
    engine.start("linear")
    engine.abort()
    with pytest.raises(CflowError, match="aborted"):
        engine.request_goto("one", "because")


def test_refiling_replaces_the_pending_one(flow_dir):
    _at_two(flow_dir)
    first = engine.request_goto("one", "first reading")
    second = engine.request_goto("three", "no — it is the later step")
    assert engine.status()["goto_request"]["step"] == "three"
    replaced = _events(flow_dir, "goto_requested")[-1]
    assert replaced["replaces"] == first["goto_request"]["id"]
    assert second["goto_request"]["id"] != first["goto_request"]["id"]


# --------------------------------------------------------------------------- #
# answering
# --------------------------------------------------------------------------- #
def test_approval_moves_the_run_and_says_who_asked(flow_dir):
    _at_two(flow_dir)
    engine.request_goto("one", "the parser case belongs to step one", by="s241")
    granted = engine.resolve_goto("approve", by="user")
    assert granted["status"] == "state_set"
    assert granted["step_id"] == "one"
    assert engine.status()["step_id"] == "one"
    assert engine.status()["status"] != "waiting_goto"
    assert "goto_request" not in engine.status()

    forced = _events(flow_dir, "state_forced")[-1]
    assert forced["step"] == "one" and forced["to"] == "one"
    assert forced["asked_by"] == "s241"
    assert _events(flow_dir, "goto_approved")[-1]["step"] == "one"

    # The step is re-delivered by 'next', per-visit gates and all.
    resumed = engine.next_step()
    assert resumed["step_id"] == "one" and resumed["visit"] == 2


def test_refusal_leaves_the_position_and_reaches_the_agent_once(flow_dir):
    _at_two(flow_dir)
    engine.request_goto("one", "I think step one was wrong")
    refused = engine.resolve_goto("deny", by="user", reason="its outcome still holds")
    assert refused["status"] == "goto_denied"
    assert engine.status()["step_id"] == "two"

    # Visible on status for as long as it stands...
    stood = engine.status()
    assert stood["goto_request"]["decision"] == "denied"
    assert stood["goto_request"]["decided_reason"] == "its outcome still holds"
    assert stood["status"] != "waiting_goto"

    # ...and delivered by the next advancing call, which then clears it: the
    # run carries on down the declared route rather than stopping again.
    engine.report("found the case")
    moved = engine.next_step()
    assert moved["goto_request"]["decision"] == "denied"
    assert "its outcome still holds" in moved["note"]
    assert moved["step_id"] == "three"
    assert "goto_request" not in engine.status()
    assert _events(flow_dir, "goto_denied")[-1]["reason"] == "its outcome still holds"


def test_resolve_needs_a_pending_request(flow_dir):
    _at_two(flow_dir)
    with pytest.raises(CflowError, match="no pending"):
        engine.resolve_goto("approve")
    engine.request_goto("one", "because")
    with pytest.raises(CflowError, match="approve"):
        engine.resolve_goto("maybe")
    engine.resolve_goto("deny")
    with pytest.raises(CflowError, match="no pending"):
        engine.resolve_goto("deny")


def test_withdrawal_releases_the_run(flow_dir):
    _at_two(flow_dir)
    engine.request_goto("one", "on reflection, maybe")
    engine.cancel_goto_request()
    assert "goto_request" not in engine.status()
    engine.report("carried on")
    assert engine.next_step()["step_id"] == "three"
    assert _events(flow_dir, "goto_withdrawn")


def test_a_forced_third_position_answers_the_request(flow_dir):
    """The human's own override is the third answer, and must not leave the
    question standing — a run that stopped twice for one question is a run
    whose operator answered it and was ignored."""
    _at_two(flow_dir)
    engine.request_goto("one", "step one, I think")
    engine.goto("three", by="user", reason="no — forward, not back")
    assert engine.status()["step_id"] == "three"
    assert "goto_request" not in engine.status()
    superseded = _events(flow_dir, "goto_superseded")[-1]
    assert superseded["step"] == "one" and superseded["forced_to"] == "three"

    # A forced position is not a delivered one: the agent picks it up with
    # 'next', which is what a nudge tells it to do.
    assert engine.next_step()["step_id"] == "three"
    engine.report("did three")
    assert engine.next_step()["status"] == "done"


def test_a_plain_goto_still_journals_no_grant(flow_dir):
    """No request behind it: the record must not read as one being granted."""
    _at_two(flow_dir)
    engine.goto("one", by="user", reason="redo it")
    forced = _events(flow_dir, "state_forced")[-1]
    assert "granted" not in forced and "asked_by" not in forced
    assert not _events(flow_dir, "goto_superseded")


def test_end_is_a_position_that_can_be_asked_for(flow_dir):
    _at_two(flow_dir)
    engine.request_goto("end", "the goal turned out to be already met")
    done = engine.resolve_goto("approve", by="user")
    assert done["status"] == "done"
    assert engine.status()["status"] == "done"


# --------------------------------------------------------------------------- #
# the agent's surface
# --------------------------------------------------------------------------- #
def test_mcp_files_and_withdraws_but_cannot_grant(flow_dir, monkeypatch):
    monkeypatch.setenv(state_mod.SESSION_ENV, "s241")
    _at_two(flow_dir)
    mcp._seen_run = engine.status()["run"]

    filed = mcp.call_tool("request_goto", {"step": "one", "reason": "unhandled case"})
    assert filed["status"] == "goto_requested"
    # Attributed to the process, not to an argument — the same rule 'answer'
    # follows, and for the same reason.
    assert filed["goto_request"]["by"] == "s241"

    held = mcp.call_tool("next", {})
    assert held["status"] == "waiting_goto"

    withdrawn = mcp.call_tool("request_goto", {"cancel": True})
    assert withdrawn["status"] == "goto_withdrawn"

    names = {t["name"] for t in mcp.TOOLS}
    assert "request_goto" in names
    # The grant stays a human control: no tool of any spelling reaches it.
    assert not (names & {"approve", "goto", "resolve_goto", "goto_resolve"})
    with pytest.raises(CflowError):
        mcp.call_tool("resolve_goto", {"decision": "approve"})


def test_mcp_request_is_fenced_against_a_replaced_run(flow_dir, monkeypatch):
    monkeypatch.setenv(state_mod.SESSION_ENV, "s241")
    _at_two(flow_dir)
    mcp._seen_run = "run-somebodyelse"
    with pytest.raises(CflowError, match="not the run here"):
        mcp.call_tool("request_goto", {"step": "one", "reason": "because"})


# --------------------------------------------------------------------------- #
# the human's surface
# --------------------------------------------------------------------------- #
def _cli(*args):
    return cli.main(["cflow", *args])


def test_cli_approves_denies_and_refuses_a_mixed_press(flow_dir, capsys):
    _at_two(flow_dir)
    engine.request_goto("one", "unhandled case")

    # A step AND a flag is two different answers at once: refused rather than
    # silently preferring one of them.
    assert _cli("goto", "three", "--approve") == 2
    assert engine.status()["status"] == "waiting_goto"

    assert _cli("goto", "--approve") == 0
    assert engine.status()["step_id"] == "one"
    assert "granted" in capsys.readouterr().out

    engine.next_step()
    engine.report("redid one")
    engine.next_step()
    engine.request_goto("one", "again")
    assert _cli("goto", "--deny", "--reason", "no") == 0
    out = capsys.readouterr().out
    assert "refused" in out
    assert engine.status()["goto_request"]["decision"] == "denied"


def test_cli_goto_still_needs_something_to_do(flow_dir, capsys):
    _at_two(flow_dir)
    assert _cli("goto") == 2
    assert "--approve" in capsys.readouterr().err


def test_cli_goto_step_is_unchanged(flow_dir):
    _at_two(flow_dir)
    assert _cli("goto", "one") == 0
    assert engine.status()["step_id"] == "one"
    assert engine.next_step()["step_id"] == "one"
