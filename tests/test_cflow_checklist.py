"""Checklist gates: a step whose exit is a list of exit codes, moved by the daemon.

The gate this exists for is `improv-worker`'s `landed` — *did the parent
actually merge my branch* — which was carried by 57 lines of prose and one
`verify`, with the driving agent deciding when to advance. These tests pin
what replaces that: the schema and everything it refuses, the
`waiting_checklist` position and the fact that `next` cannot leave it, the
three item states (true / false / could not measure), the two conditions on
the move, and the ChecklistClock's scan that performs and announces it.
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock

#: Every item asks the same question of a different flag file, so a test moves
#: the gate by touching a file rather than by mocking the measurement.
GATE = """
import pathlib
import sys

sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 1)
"""

FLOW = """
name: lander
description: land a branch
steps:
  work:
    instructions: do the work
    next: landed
  landed:
    title: has it landed?
    instructions: freeze the branch and wait
    done_when: the merge commit is in the history
    checklist:
      prompt: has this branch actually landed?
      then: wrapup
      poll: 30
      items:
        - id: merged
          describe: a merge commit on the target lists my tip as a parent
          check: 'python gate.py merged.flag'
        - id: frozen
          describe: the working tree is clean
          check: 'python gate.py frozen.flag'
  wrapup:
    instructions: close the round
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "lander.yaml").write_text(FLOW, encoding="utf-8")
    (d / "gate.py").write_text(GATE, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return d


def _events(**match) -> list:
    return [
        e for e in state_mod.read_journal()
        if all(e.get(k) == v for k, v in match.items())
    ]


def _at_landed() -> None:
    """Round in progress: start, finish `work`, arrive at the gate."""
    engine.start("lander")
    engine.report("did the work")
    engine.next_step()


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_checklist_is_parsed():
    step = model.parse(FLOW).steps["landed"]
    assert step.checklist.then == "wrapup"
    assert step.checklist.poll == 30
    assert step.checklist.timeout == model.DEFAULT_CHECKLIST_TIMEOUT
    assert [i.id for i in step.checklist.items] == ["merged", "frozen"]
    assert step.checklist.items[0].describe.startswith("a merge commit")
    # `then` is the step's ONLY exit, so it is the edge reachability sees.
    assert step.successors() == ["wrapup"]


def test_checklist_poll_has_a_floor_and_timeout_a_ceiling():
    text = FLOW.replace("poll: 30", "poll: 1\n      timeout: 900")
    checklist = model.parse(text).steps["landed"].checklist
    assert checklist.poll == model.MIN_CHECKLIST_POLL
    assert checklist.timeout == model.MAX_CHECKLIST_TIMEOUT


@pytest.mark.parametrize(
    "swap, match",
    [
        ("then: wrapup\n      items: []", "non-empty"),
        ("then: wrapup", "items"),
        ("items:\n        - {id: a, describe: b, check: c}", "then"),
        ("then: landed\n      items: [{id: a, describe: b, check: c}]", "different"),
        ("then: nowhere\n      items: [{id: a, describe: b, check: c}]", "unknown step"),
        ("then: wrapup\n      items: [{id: a, describe: b}]", "check"),
        ("then: wrapup\n      items: [{id: a, check: c}]", "describe"),
        ("then: wrapup\n      items: [{describe: b, check: c}]", "id"),
        (
            "then: wrapup\n      items: [{id: a, describe: b, check: c},"
            " {id: a, describe: d, check: e}]",
            "twice",
        ),
        ("then: wrapup\n      nope: 1\n      items: [{id: a, describe: b, check: c}]",
         "unknown key"),
    ],
)
def test_checklist_parse_errors(swap, match):
    head, _, _ = FLOW.partition("      prompt: has this branch actually landed?")
    text = head + "      " + swap.replace("\n      ", "\n      ") + "\n  wrapup:\n    instructions: close the round\n"
    with pytest.raises(WorkflowError, match=match):
        model.parse(text)


@pytest.mark.parametrize(
    "extra, match",
    [
        ("    next: wrapup\n", "next"),
        ("    verify: 'echo hi'\n", "verify"),
        ("    awaits: {probe: 'echo hi'}\n", "awaits"),
        ("    timer: {every: 30, max: 1, then: work, after: end}\n", "timer"),
    ],
)
def test_checklist_refuses_a_second_exit(extra, match):
    text = FLOW.replace("    title: has it landed?\n", "    title: has it landed?\n" + extra)
    with pytest.raises(WorkflowError, match=match):
        model.parse(text)


def test_checklist_step_is_not_advised_about_a_missing_criterion():
    """A checklist states completion in the most checkable form there is."""
    text = FLOW.replace("    done_when: the merge commit is in the history\n", "")
    advice = " ".join(model.parse(text).advice)
    assert "landed" not in advice


# --------------------------------------------------------------------------- #
# the position
# --------------------------------------------------------------------------- #
def test_arrival_reports_waiting_checklist_with_every_item(proj):
    _at_landed()
    payload = engine.status()
    assert payload["status"] == "waiting_checklist"
    checklist = payload["checklist"]
    assert checklist["total"] == 2 and checklist["passed"] == 0
    assert checklist["all_true"] is False
    assert checklist["then"] == "wrapup"
    # Nothing measured yet: unknown, and unknown is not true.
    assert [i["ok"] for i in checklist["items"]] == [None, None]
    assert [i["describe"] for i in checklist["items"]][1] == (
        "the working tree is clean"
    )
    assert payload["done_when"].startswith("the merge commit")
    assert _events(event="checklist_presented", step="landed")


def test_next_cannot_leave_a_checklist_gate(proj):
    _at_landed()
    engine.report("froze the branch at abc1234")
    for _ in range(3):
        payload = engine.next_step()
        assert payload["status"] == "waiting_checklist"
        assert payload["step_id"] == "landed"
    assert engine.status()["step_id"] == "landed"


# --------------------------------------------------------------------------- #
# measuring
# --------------------------------------------------------------------------- #
def test_a_false_item_holds_the_gate_shut(proj):
    _at_landed()
    engine.report("froze the branch")
    (proj / "frozen.flag").touch()
    result = engine.check_checklist()
    assert result["passed"] == 1 and result["all_true"] is False
    assert result.get("moved_to") is None
    assert engine.status()["step_id"] == "landed"
    items = {i["id"]: i for i in engine.status()["checklist"]["items"]}
    assert items["frozen"]["ok"] is True and items["frozen"]["exit_code"] == 0
    assert items["merged"]["ok"] is False and items["merged"]["exit_code"] == 1
    assert _events(event="checklist_changed", step="landed")


def test_an_unmeasurable_item_is_unknown_and_never_true(proj):
    """A command that cannot answer is no answer — and no answer holds."""
    text = FLOW.replace(
        "        - id: merged\n"
        "          describe: a merge commit on the target lists my tip as a parent\n"
        "          check: 'python gate.py merged.flag'\n",
        "        - id: merged\n"
        "          describe: a merge commit on the target lists my tip as a parent\n"
        "          check: 'python -c \"import time; time.sleep(30)\"'\n",
    ).replace("poll: 30", "poll: 30\n      timeout: 0.5")
    (proj / ".claunch" / "workflows" / "lander.yaml").write_text(text, encoding="utf-8")
    _at_landed()
    engine.report("froze the branch")
    (proj / "frozen.flag").touch()
    engine.check_checklist()
    # Read the three states off the run rather than off that return value. The
    # timeout is the *checklist's*, so `frozen` spends the same 0.5s launching
    # a shell and a Python that the sleeping item spends timing out; on a busy
    # machine it can miss it too, and then both items are unknown, nothing
    # changed, and `check_checklist` announces nothing by answering None. The
    # measurement is written to the state either way, and the position is what
    # "no answer holds" actually means.
    payload = engine.status()
    assert payload["status"] == "waiting_checklist"  # unknown never opened it
    checklist = payload["checklist"]
    assert checklist["all_true"] is False
    items = {i["id"]: i for i in checklist["items"]}
    assert items["merged"]["ok"] is None
    assert items["merged"]["exit_code"] is None
    assert items["merged"]["measured_at"] is not None  # measured, and unmeasurable


def test_an_unchanged_measurement_says_nothing(proj):
    _at_landed()
    engine.report("froze the branch")
    assert engine.check_checklist()["changed"] == ["frozen", "merged"]
    assert engine.check_checklist() is None
    assert len(_events(event="checklist_changed")) == 1


# --------------------------------------------------------------------------- #
# the move
# --------------------------------------------------------------------------- #
def test_all_true_without_a_report_holds_the_move(proj):
    _at_landed()
    (proj / "merged.flag").touch()
    (proj / "frozen.flag").touch()
    result = engine.check_checklist()
    assert result["all_true"] is True
    assert result["report_filed"] is False
    assert result.get("moved_to") is None
    assert engine.status()["step_id"] == "landed"
    assert engine.status()["note"].startswith("every checklist item is true;")


def test_a_report_alone_moves_nothing(proj):
    _at_landed()
    engine.report("I say it landed")
    assert engine.check_checklist()["all_true"] is False
    assert engine.status()["step_id"] == "landed"


def test_all_true_and_reported_moves_the_run_and_journals_the_evidence(proj):
    _at_landed()
    engine.report("froze the branch at abc1234", "requested landing")
    (proj / "merged.flag").touch()
    (proj / "frozen.flag").touch()
    result = engine.check_checklist()
    assert result["moved_to"] == "wrapup"
    assert engine.status()["step_id"] == "wrapup"
    passed = _events(event="checklist_passed", step="landed")
    assert len(passed) == 1
    recorded = {i["id"]: i for i in passed[0]["items"]}
    assert recorded["merged"]["exit_code"] == 0
    assert recorded["merged"]["check"] == "python gate.py merged.flag"
    assert recorded["merged"]["measured_at"]
    # The step is completed the ordinary way, so the journal and the PR text
    # carry the agent's own account alongside the machine's.
    assert _events(event="step_completed", step="landed")[0]["summary"] == (
        "froze the branch at abc1234"
    )


def test_the_gate_is_re_asked_on_a_second_visit(proj):
    """Last visit's green items are not an answer to this visit's question."""
    _at_landed()
    engine.report("froze the branch")
    (proj / "merged.flag").touch()
    (proj / "frozen.flag").touch()
    assert engine.check_checklist()["moved_to"] == "wrapup"
    engine.goto("landed", reason="the merge was reverted")
    payload = engine.status()
    assert payload["status"] == "waiting_checklist"
    assert [i["ok"] for i in payload["checklist"]["items"]] == [None, None]


def test_a_human_goto_is_the_only_way_past_a_red_gate(proj):
    _at_landed()
    engine.report("the leader is not landing this round")
    assert engine.check_checklist()["all_true"] is False
    engine.goto("wrapup", reason="held over to the next round")
    assert engine.status()["step_id"] == "wrapup"


# --------------------------------------------------------------------------- #
# the daemon's clock
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.name = name
        self.cwd = cwd
        self.delivered: list = []

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def test_checklist_clock_measures_then_moves_and_wakes_the_driver(proj):
    cwd = str(proj)
    engine.start("lander", cwd=cwd, scope="w1")
    engine.report("did the work", cwd=cwd, scope="w1")
    engine.next_step(cwd=cwd, scope="w1")
    engine.report("froze the branch at abc1234", cwd=cwd, scope="w1")
    session = _FakeSession("w1", cwd)
    clock = cflow_clock.ChecklistClock(_FakeManager({"w1": session}))

    # A red gate is measured and written, and says nothing.
    assert clock.scan(now=1000.0) == []
    shown = engine.status(cwd, scope="w1")["checklist"]
    assert shown["passed"] == 0 and shown["checked_at"]

    # Inside the poll window nothing is re-measured...
    (proj / "merged.flag").touch()
    (proj / "frozen.flag").touch()
    assert clock.scan(now=1010.0) == []
    assert engine.status(cwd, scope="w1")["step_id"] == "landed"

    # ...and past it the gate opens, the run moves, and the driver hears why.
    moved = clock.scan(now=1040.0)
    assert len(moved) == 1
    cwd_out, scope_out, block = moved[0]
    assert (cwd_out, scope_out) == (cwd, "w1")
    assert "checklist passed" in block
    assert "'landed' (2/2 items true)" in block
    assert "[x] merged:" in block and "exit 0" in block
    assert "step 'wrapup'" in block
    assert engine.status(cwd, scope="w1")["step_id"] == "wrapup"
    assert clock.scan(now=2000.0) == []  # moved on: nothing to say


def test_checklist_clock_leaves_other_runs_alone(proj):
    cwd = str(proj)
    engine.start("lander", cwd=cwd, scope="w2")
    assert cflow_clock.ChecklistClock(_FakeManager({})).scan(now=1.0) == []
    assert engine.status(cwd, scope="w2")["step_id"] == "work"


# --------------------------------------------------------------------------- #
# the two gates this was built for
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "workflow, step, then, items",
    [
        ("improv-worker", "landed", "wrapup", ["merged", "frozen"]),
        ("improv-leader", "reflect", "end", ["deployed"]),
    ],
)
def test_the_shipped_gates_are_checklists(workflow, step, then, items):
    """The two decisions that were carried by prose, now carried by exit codes.

    Pinned here rather than left to a reader of the YAML because the property
    that matters is not "a checklist exists" but "there is no other way out":
    a `next:` re-added to either step would put the transition back in the
    agent's hands, and the parser would accept the file if this did not.
    """
    import pathlib

    wf = model.load(
        pathlib.Path(f"src/claude_launcher/workflows/{workflow}.yaml")
    )
    checklist = wf.steps[step].checklist
    assert checklist is not None
    assert checklist.then == then
    assert [i.id for i in checklist.items] == items
    assert wf.steps[step].next is None
    assert wf.steps[step].verify is None and wf.steps[step].awaits is None
    assert all(i.describe and i.check for i in checklist.items)


# --------------------------------------------------------------------------- #
# the CLI surface
# --------------------------------------------------------------------------- #
def test_the_cli_prints_the_gate_as_a_list(proj, capsys):
    """`claunch cflow status` is the other half of "a person can see this".

    The dashboard is the richer view and the one most people watch, but it
    needs a daemon and a browser; a person standing in the directory has this.
    Both read the same payload, so what is pinned here is that the CLI renders
    every item rather than the status word alone — which is what it did for
    every other waiting position before the checklist gate existed.
    """
    import argparse

    from claude_launcher import cli_cflow

    _at_landed()
    engine.report("froze the branch at abc1234")
    (proj / "frozen.flag").touch()
    engine.check_checklist()

    args = argparse.Namespace(json=False, recheck=False, session=None, cwd=None)
    assert cli_cflow._cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "waiting_checklist" in out
    assert "1/2 true" in out
    assert "[x] frozen:" in out and "[ ] merged:" in out
    assert "exit 1" in out
    assert "wrapup" in out
    assert "claunch cflow checklist --recheck" in out


def test_the_cli_can_re_measure_without_a_daemon(proj, capsys):
    """The door that keeps a checklist honest where no clock is running."""
    import argparse

    from claude_launcher import cli_cflow

    _at_landed()
    engine.report("froze the branch at abc1234")
    (proj / "merged.flag").touch()
    (proj / "frozen.flag").touch()
    args = argparse.Namespace(json=False, recheck=True, session=None, cwd=None)
    assert cli_cflow._cmd_checklist(args) == 0
    out = capsys.readouterr().out
    assert "checklist passed: landed -> wrapup" in out
    assert engine.status()["step_id"] == "wrapup"
