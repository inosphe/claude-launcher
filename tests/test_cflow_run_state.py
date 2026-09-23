"""Run state: the paths a workflow's ``editable:`` leaves writable while it runs.

A run's workflow is fixed at start (composed, snapshotted). These tests pin
the one door left open after that: which paths may be declared and what the
parser refuses, who may write each one (a person through the CLI, the agent
through the ``set_state`` MCP tool), the ``state_set`` journal, the payload's
``state``, the step ``skip`` read at ENTRY only (a write never reaches back),
a checklist item ticked instead of measured, and the ``CFLOW_VAR_<PATH>``
environment a command reads.
"""

from __future__ import annotations

import argparse

import pytest

from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError

#: Exits 0 when the named environment variable holds the expected value.
ENVCHECK = """
import os
import sys

sys.exit(0 if os.environ.get(sys.argv[1]) == sys.argv[2] else 1)
"""

FLOW = """
name: stated
description: a workflow with writable run state
editable:
  steps.review.skip:
    describe: this round goes without peer review
  notes:
    type: text
    describe: what the person asks of this run
  fast:
    type: bool
    by: [user, agent]
steps:
  work:
    instructions: do the work
    next: review
  review:
    instructions: have it reviewed
    next: landed
  landed:
    instructions: wait for the landing
    checklist:
      then: wrapup
      items:
        - id: approved
          describe: the person looked at the result
          by: user
        - id: fast
          describe: the fast flag reaches the command
          check: 'python envcheck.py CFLOW_VAR_FAST true'
  wrapup:
    instructions: close the round
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "stated.yaml").write_text(FLOW, encoding="utf-8")
    (d / "envcheck.py").write_text(ENVCHECK, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return d


def _events(kind: str) -> list:
    return [e for e in state_mod.read_journal() if e.get("event") == kind]


def _advance(summary: str = "done") -> dict:
    engine.report(summary)
    return engine.next_step()


def _state(payload: dict) -> dict:
    return {entry["path"]: entry for entry in payload.get("state") or []}


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_editable_is_parsed_with_its_defaults():
    wf = model.parse(FLOW)
    skip = wf.editable["steps.review.skip"]
    assert (skip.type, skip.by, skip.default) == ("bool", ("user",), False)
    notes = wf.editable["notes"]
    assert (notes.type, notes.by, notes.default) == ("text", ("user",), "")
    assert wf.editable["fast"].by == ("user", "agent")
    assert wf.editable["fast"].env_name == "CFLOW_VAR_FAST"
    # The ticked checklist item declares its own path; nobody writes it twice.
    ticked = wf.editable["steps.landed.checklist.approved"]
    assert ticked.implicit and ticked.type == "bool" and ticked.by == ("user",)
    item = wf.steps["landed"].checklist.item("approved")
    assert item.manual and item.check is None


def test_a_static_skip_default_follows_the_step():
    text = FLOW.replace(
        "    instructions: have it reviewed\n",
        "    instructions: have it reviewed\n    skip: true\n",
    )
    assert model.parse(text).editable["steps.review.skip"].default is True


@pytest.mark.parametrize(
    "editable, match",
    [
        ("  steps.nowhere.skip: {}\n", "no step 'nowhere'"),
        ("  steps.landed.skip: {}\n", "cannot be skipped"),
        ("  steps.work.skip: {}\n", "start step"),
        ("  steps.review.title: {type: text}\n", "only step property"),
        ("  steps.landed.checklist.approved: {}\n", "the item itself"),
        ("  Bad-Name: {type: bool}\n", "a path is"),
        ("  note: {}\n", "needs a 'type'"),
        ("  note: {type: number}\n", "allowed: bool, text"),
        ("  note: {type: text, by: [agent]}\n", "person only"),
        ("  note: {type: text, by: [user, agent]}\n", "person only"),
        ("  flag: {type: bool, by: [robot]}\n", "allowed: user, agent"),
        ("  flag: {type: bool, default: maybe}\n", "true or false"),
        ("  flag: {type: bool, nope: 1}\n", "unknown key"),
        ("  steps.review.skip: {type: text}\n", "is a bool"),
    ],
)
def test_editable_refusals(editable, match):
    text = FLOW.replace(
        "editable:\n", "editable:\n" + editable
    ) if not editable.startswith("  steps.review.skip") else FLOW.replace(
        "  steps.review.skip:\n    describe: this round goes without peer review\n",
        editable,
    )
    with pytest.raises(WorkflowError, match=match):
        model.parse(text)


@pytest.mark.parametrize(
    "item, match",
    [
        ("{id: a, describe: b, check: c, by: user}", "both 'check' and 'by'"),
        ("{id: a, describe: b}", "or 'by: \\[user\\]'"),
        ("{id: a, describe: b, by: nobody}", "allowed: user, agent"),
    ],
)
def test_checklist_item_refusals(item, match):
    text = FLOW.replace(
        "        - id: approved\n"
        "          describe: the person looked at the result\n"
        "          by: user\n",
        f"        - {item}\n",
    )
    with pytest.raises(WorkflowError, match=match):
        model.parse(text)


@pytest.mark.parametrize(
    "where",
    [
        # a select has no single edge to pass through to
        "select",
        # a checklist is a daemon-held condition
        "checklist",
    ],
)
def test_skip_is_refused_on_a_step_without_one_plain_exit(where):
    if where == "select":
        text = """
name: s
steps:
  a:
    instructions: x
    next: b
  b:
    skip: true
    select:
      prompt: which
      chooser: agent
      options:
        one: {description: one, next: end}
"""
    else:
        text = FLOW.replace(
            "    instructions: wait for the landing\n",
            "    instructions: wait for the landing\n    skip: true\n",
        )
    with pytest.raises(WorkflowError, match="cannot be skipped"):
        model.parse(text)


# --------------------------------------------------------------------------- #
# writing: who may, what is recorded, what the payload shows
# --------------------------------------------------------------------------- #
def test_a_person_writes_and_the_payload_and_journal_show_it(proj):
    first = engine.start("stated")
    view = _state(first)
    assert view["notes"]["value"] == "" and "set_by" not in view["notes"]

    out = engine.set_state("notes", "leave app.js alone", by="user")
    assert out["status"] == "state_written"
    assert (out["was"], out["value"], out["changed"]) == ("", "leave app.js alone", True)

    view = _state(engine.status())
    assert view["notes"]["value"] == "leave app.js alone"
    assert view["notes"]["set_by"] == "user"
    written = _events("state_set")
    assert [(e["path"], e["value"], e["by"], e["step"]) for e in written] == [
        ("notes", "leave app.js alone", "user", "work")
    ]


def test_a_value_change_does_not_change_the_step_text_id(proj):
    """The digest names the step's text; a person's note is not new text."""
    before = engine.start("stated")["digest"]
    engine.set_state("notes", "something", by="user")
    assert engine.status()["digest"] == before


def test_the_agent_is_refused_a_persons_path(proj):
    engine.start("stated")
    with pytest.raises(engine.CflowError, match="written by user only"):
        engine.set_state("notes", "mine now", by="agent")
    with pytest.raises(engine.CflowError, match="written by user only"):
        engine.set_state("steps.review.skip", True, by="agent")
    assert _events("state_set") == []


def test_the_mcp_tool_writes_as_the_agent(proj):
    mcp.call_tool("start", {"workflow": "stated"})
    out = mcp.call_tool("set_state", {"path": "fast", "value": True})
    assert out["by"] == "agent" and out["value"] is True
    with pytest.raises(engine.CflowError, match="written by user only"):
        mcp.call_tool("set_state", {"path": "notes", "value": "x"})


def test_undeclared_paths_and_bad_values_are_refused(proj):
    engine.start("stated")
    with pytest.raises(engine.CflowError, match="not writable"):
        engine.set_state("steps.work.skip", True, by="user")
    with pytest.raises(engine.CflowError, match="is a bool"):
        engine.set_state("fast", "maybe", by="user")
    for spelling, expected in (("yes", True), ("OFF", False), ("1", True)):
        assert engine.set_state("fast", spelling, by="user")["value"] is expected


def test_a_finished_run_takes_no_writes(proj):
    engine.start("stated")
    engine.goto("end")
    with pytest.raises(engine.CflowError, match="done"):
        engine.set_state("notes", "late", by="user")


def test_the_cli_writes_as_a_person(proj, capsys, monkeypatch):
    from claude_launcher import cli_cflow

    monkeypatch.setattr(cli_cflow, "_nudge_via_daemon", lambda *a, **k: [])
    engine.start("stated")
    args = argparse.Namespace(
        path="steps.review.skip", value="true", session=None, cwd=None
    )
    assert cli_cflow._cmd_set(args) == 0
    out = capsys.readouterr().out
    assert "steps.review.skip: False -> True" in out
    assert "read when the run next enters that step" in out
    assert _events("state_set")[0]["by"] == "user"


# --------------------------------------------------------------------------- #
# skip: read at entry, never backwards
# --------------------------------------------------------------------------- #
def test_a_skip_written_ahead_passes_the_step(proj):
    engine.start("stated")
    engine.set_state("steps.review.skip", True, by="user")
    payload = _advance()
    assert payload["step_id"] == "landed"
    skipped = _events("step_skipped")
    assert [(e["step"], e["next"], e["by"], e["set_by"]) for e in skipped] == [
        ("review", "landed", "state", "user")
    ]
    # A skipped step was never entered: no visit counted.
    assert state_mod.load_state()["visits"].get("review") is None


def test_a_skip_written_while_on_the_step_does_not_reach_back(proj):
    engine.start("stated")
    assert _advance()["step_id"] == "review"
    out = engine.set_state("steps.review.skip", True, by="user")
    assert "applies from the next time the run enters it" in out["applies"]
    # Still standing here; the visit under way is what it was.
    assert engine.status()["step_id"] == "review"
    assert _advance()["step_id"] == "landed"
    assert _events("step_skipped") == []


def test_a_skip_turned_off_again_is_honoured(proj):
    engine.start("stated")
    engine.set_state("steps.review.skip", True, by="user")
    engine.set_state("steps.review.skip", False, by="user")
    assert _advance()["step_id"] == "review"


def test_a_static_skip_passes_and_a_person_goto_still_lands_there(proj):
    text = FLOW.replace(
        "    instructions: have it reviewed\n",
        "    instructions: have it reviewed\n    skip: true\n",
    ).replace("  steps.review.skip:\n    describe: this round goes without peer review\n", "")
    (proj / ".claunch" / "workflows" / "stated.yaml").write_text(text, encoding="utf-8")
    engine.start("stated")
    assert _advance()["step_id"] == "landed"
    assert _events("step_skipped")[0]["by"] == "workflow"
    # Naming a step outright is asking for it, skipped or not.
    engine.goto("review")
    assert engine.status()["step_id"] == "review"


# --------------------------------------------------------------------------- #
# the ticked checklist item, and the environment a command reads
# --------------------------------------------------------------------------- #
def test_a_ticked_item_opens_the_gate_with_a_measured_one(proj):
    engine.start("stated")
    engine.set_state("steps.review.skip", True, by="user")
    payload = _advance()
    assert payload["status"] == "waiting_checklist"
    items = {i["id"]: i for i in payload["checklist"]["items"]}
    assert items["approved"]["ok"] is False and items["approved"]["by"] == ["user"]
    engine.report("waited for the landing")

    # The command reads CFLOW_VAR_FAST; with the default (false) it is red.
    assert engine.check_checklist() is not None
    assert engine.status()["step_id"] == "landed"

    engine.set_state("fast", True, by="user")
    engine.check_checklist()
    assert engine.status()["step_id"] == "landed"  # nobody ticked 'approved' yet

    # The tick shows at once, before any measurement.
    engine.set_state("steps.landed.checklist.approved", True, by="user")
    items = {i["id"]: i for i in engine.status()["checklist"]["items"]}
    assert items["approved"]["ok"] is True
    assert items["approved"]["output"] == "set by user"

    moved = engine.check_checklist()
    assert moved["moved_to"] == "wrapup"
    assert engine.status()["step_id"] == "wrapup"


def test_the_agent_cannot_tick_a_persons_item(proj):
    mcp.call_tool("start", {"workflow": "stated"})
    with pytest.raises(engine.CflowError, match="written by user only"):
        mcp.call_tool(
            "set_state", {"path": "steps.landed.checklist.approved", "value": True}
        )


def test_verify_reads_the_run_state(proj):
    text = FLOW.replace(
        "    instructions: do the work\n    next: review\n",
        "    instructions: do the work\n"
        "    verify: 'python envcheck.py CFLOW_VAR_NOTES go'\n"
        "    next: review\n",
    )
    (proj / ".claunch" / "workflows" / "stated.yaml").write_text(text, encoding="utf-8")
    engine.start("stated")
    engine.report("did it")
    assert engine.next_step()["status"] == "verify_failed"
    engine.set_state("notes", "go", by="user")
    engine.report("did it")
    assert engine.next_step()["step_id"] == "review"


def test_the_cli_says_who_ticks_an_item(proj, capsys):
    """A ticked item has no exit code; 'could not measure' would read as broken."""
    from claude_launcher import cli_cflow

    engine.start("stated")
    engine.set_state("steps.review.skip", True, by="user")
    payload = _advance()
    cli_cflow._print_checklist(payload["checklist"])
    out = capsys.readouterr().out
    assert (
        "approved: the person looked at the result (waits for user to tick it: "
        "claunch cflow set steps.landed.checklist.approved true)"
    ) in out
    engine.set_state("steps.landed.checklist.approved", True, by="user")
    cli_cflow._print_checklist(engine.status()["checklist"])
    out = capsys.readouterr().out
    assert "approved: the person looked at the result (set by user)" in out
    assert "could not measure" not in out.split("fast:")[0]
