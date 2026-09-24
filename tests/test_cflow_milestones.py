"""Milestones: the sync points between a session's main run and a sub run.

A step ``publishes:`` a milestone when the run leaves it by its own progress;
the other run of the scope reads it with ``awaits: {sub: <name>, at: <m>}``
(main reading a sub run) or ``awaits: {main: <m>}`` (a sub run reading its
main run). Whether a wait is over is counted, not timed: the step records the
publication count it has seen when it is left, and a publication past that
record is "new". claunch-u8wjx.1 (the stack sub run's cut handshake) is the
first user; the shape pinned here is the handshake itself.

What this file pins:

* parsing: ``publishes``, ``awaits.at`` / ``awaits.main`` and their refusals;
* the handshake: request -> cut -> consumed, both directions, and a
  publication made before the reader arrives still counts;
* a person's goto away publishes nothing;
* the probe command, the CLI's exit codes and the probe environment's run;
* ``landing_queue`` on a sub definition, one holder per scope.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import cli_cflow
from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.engine import CflowError

MAIN = """
name: main
steps:
  work:
    instructions: do the work
    next: request
  request:
    instructions: ask the stack for a cut
    publishes: cut-wanted
    next: land
  land:
    instructions: land what the cut holds
    awaits: {sub: stack, at: cut}
    select:
      prompt: again or finish?
      chooser: agent
      options:
        again: {description: another round, next: work}
        finish: {description: stop}
"""

STACK = """
name: stack
kind: subflow
steps:
  standby:
    instructions: keep the stack
    awaits: {main: cut-wanted}
    next: cut
  cut:
    instructions: split the table
    publishes: cut
    select:
      prompt: keep going or seal?
      chooser: agent
      options:
        again: {description: back to standby, next: standby}
        seal: {description: stop}
"""

QUEUED_SUB = """
name: queued
kind: subflow
landing_queue: {target: base}
steps:
  hold:
    instructions: hold the children's requests
"""

QUEUED_MAIN = """
name: qmain
landing_queue: {target: master}
steps:
  work:
    instructions: x
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    for name, text in (("main", MAIN), ("stack", STACK), ("queued", QUEUED_SUB),
                       ("qmain", QUEUED_MAIN)):
        (wf / f"{name}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.setenv(state_mod.SESSION_ENV, "s1")
    monkeypatch.delenv(state_mod.RUN_ENV, raising=False)
    mcp._seen_run = None
    mcp._seen_subs.clear()
    token = state_mod._run_override.set(None)
    yield d
    state_mod._run_override.reset(token)


def _step(run=None):
    """Move the given run one step: take `again` at a select, else report
    and go on."""
    if engine.status(run=run).get("status") == "select":
        return engine.select("again", "loop", by="agent", run=run)
    engine.report("done", run=run)
    return engine.next_step(run=run)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_publishes_and_milestone_awaits_parse():
    main = model.parse(MAIN)
    assert main.steps["request"].publishes == ("cut-wanted",)
    land = main.steps["land"].awaits
    assert (land.sub, land.at, land.main, land.milestone) == ("stack", "cut", None, "cut")
    assert land.command(main.steps["land"]) == "claunch cflow published stack cut --step land"
    stack = model.parse(STACK)
    wait = stack.steps["standby"].awaits
    assert (wait.main, wait.milestone) == ("cut-wanted", "cut-wanted")
    assert wait.command(stack.steps["standby"]) == (
        "claunch cflow published main cut-wanted --step standby"
    )
    listed = model.parse("steps:\n  a:\n    instructions: x\n    publishes: [p, q]\n")
    assert listed.steps["a"].publishes == ("p", "q")


def test_a_milestone_await_may_name_its_own_probe():
    """The repository picks the command; the milestone still names what is
    consumed on the way out."""
    sub = model.parse(
        "kind: subflow\nsteps:\n  a:\n    instructions: x\n"
        "    awaits: {main: m, probe: 'twin main m --step a'}\n"
    )
    a = sub.steps["a"]
    assert (a.awaits.milestone, a.awaits.command(a)) == ("m", "twin main m --step a")
    main = model.parse(
        "steps:\n  a:\n    instructions: x\n"
        "    awaits: {sub: s, at: m, probe: 'twin s m --step a'}\n"
    )
    a = main.steps["a"]
    assert (a.awaits.sub, a.awaits.milestone, a.awaits.command(a)) == ("s", "m", "twin s m --step a")
    with pytest.raises(model.WorkflowError, match="not both"):
        model.parse("steps:\n  a:\n    instructions: x\n    awaits: {sub: s, probe: 'x'}\n")


@pytest.mark.parametrize(
    "text, refused",
    [
        # only a sub run has a main run to read
        ("steps:\n  a:\n    instructions: x\n    awaits: {main: m}\n", "only a 'kind: subflow'"),
        # a milestone belongs to one named sub run
        ("steps:\n  a:\n    instructions: x\n    awaits: {sub: all, at: m}\n", "name the run"),
        ("steps:\n  a:\n    instructions: x\n    awaits: {at: m}\n", "give the run as 'sub'"),
        ("kind: subflow\nsteps:\n  a:\n    instructions: x\n    awaits: {main: m, sub: s}\n",
         "takes no 'sub'"),
        ("steps:\n  a:\n    instructions: x\n    publishes: 'a b'\n", "milestone name"),
        ("steps:\n  a:\n    instructions: x\n    publishes: [p, p]\n", "twice"),
        # still one level: a sub run does not wait on another sub run
        ("kind: subflow\nsteps:\n  a:\n    instructions: x\n    awaits: {sub: s, at: m}\n",
         "only the main run waits on sub runs"),
    ],
)
def test_milestone_refusals(text, refused):
    with pytest.raises(model.WorkflowError, match=refused):
        model.parse(text)


def test_the_awaits_payload_names_the_milestone_and_its_source(proj):
    engine.start("main")
    engine.start("stack", run="stack")
    wait = engine.status(run="stack")["awaits"]
    assert wait["milestone"] == "cut-wanted" and wait["from"] == "main"
    assert wait["probe"] == "claunch cflow published main cut-wanted --step standby"


# --------------------------------------------------------------------------- #
# the handshake
# --------------------------------------------------------------------------- #
def test_request_cut_consume_round_trip(proj):
    engine.start("main")
    engine.start("stack", run="stack")
    asked = engine.published("main", "cut-wanted", step="standby", run="stack")
    assert asked["stands"] and not asked["new"] and asked["count"] == 0

    _step()                      # work -> request
    at_land = _step()            # request -> land: publishes cut-wanted
    assert at_land["step_id"] == "land"
    asked = engine.published("main", "cut-wanted", step="standby", run="stack")
    assert asked["new"] and asked["count"] == 1 and asked["consumed"] == 0
    assert asked["last"]["step"] == "request"
    assert not engine.published("stack", "cut", step="land")["new"]

    assert _step(run="stack")["step_id"] == "cut"       # consumes cut-wanted #1
    assert _step(run="stack")["step_id"] == "standby"   # publishes cut #1
    asked = engine.published("main", "cut-wanted", step="standby", run="stack")
    assert not asked["new"] and asked["consumed"] == 1
    cut = engine.published("stack", "cut", step="land")
    assert cut["new"] and cut["count"] == 1

    assert _step()["step_id"] == "work"                 # main consumes cut #1
    assert not engine.published("stack", "cut", step="land")["new"]

    journal = [e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "published"]
    assert [(e["milestone"], e["count"]) for e in journal] == [("cut-wanted", 1)]
    token = state_mod.push_run("stack")
    try:
        sub_journal = [
            e for e in state_mod.read_journal(str(proj), "s1") if e["event"] == "published"
        ]
    finally:
        state_mod.pop_run(token)
    assert [(e["milestone"], e["count"], e["sub"]) for e in sub_journal] == [("cut", 1, "stack")]


def test_a_publication_made_before_the_reader_arrives_still_counts(proj):
    """The sub run is busy in `cut` when the main run asks again; when it
    comes back to `standby` the request is waiting for it, not lost."""
    engine.start("main")
    engine.start("stack", run="stack")
    _step(); _step()                   # main at land, cut-wanted #1
    _step(run="stack")                 # stack at cut (consumed #1)
    _step()                            # main -> work (nothing consumed: no cut yet)
    _step(); _step()                   # main at land again, cut-wanted #2
    assert engine.status(run="stack")["step_id"] == "cut"
    _step(run="stack")                 # stack back at standby, cut #1 published
    asked = engine.published("main", "cut-wanted", step="standby", run="stack")
    assert asked["new"] and (asked["count"], asked["consumed"]) == (2, 1)


def test_a_goto_away_publishes_nothing(proj):
    engine.start("main")
    _step()                            # at request
    engine.goto("land", by="user", reason="skip the request")
    assert engine.status()["step_id"] == "land"
    engine.start("stack", run="stack")
    asked = engine.published("main", "cut-wanted", step="standby", run="stack")
    assert asked["count"] == 0


def test_a_missing_source_run_does_not_stand(proj):
    engine.start("main")
    view = engine.published("stack", "cut", step="land")
    assert view["stands"] is False and view["new"] is False


# --------------------------------------------------------------------------- #
# the probe's doors
# --------------------------------------------------------------------------- #
def _ns(**kw):
    base = {"session": None, "run": None, "json": False, "step": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_published_answers_with_exit_codes(proj, capsys, monkeypatch):
    engine.start("main")
    assert cli_cflow._cmd_published(_ns(source="stack", milestone="cut", step="land")) == 2
    engine.start("stack", run="stack")
    assert cli_cflow._cmd_published(_ns(source="stack", milestone="cut", step="land")) == 1
    _step(); _step()
    # the sub run's probe finds its run in the environment, as the daemon sets it
    monkeypatch.setenv(state_mod.RUN_ENV, "stack")
    assert cli_cflow._cmd_published(
        _ns(source="main", milestone="cut-wanted", step="standby")
    ) == 0
    state_mod._run_override.set(None)
    out = capsys.readouterr().out
    assert "not running" in out and "new" in out


PUBLISHED = Path(__file__).resolve().parents[1] / "tools" / "published.py"
SUB_DONE = Path(__file__).resolve().parents[1] / "tools" / "sub_done.py"


def _tool(script, where, *args, run=None):
    env = {k: v for k, v in os.environ.items() if k != state_mod.RUN_ENV}
    env[state_mod.SESSION_ENV] = "s1"
    if run:
        env[state_mod.RUN_ENV] = run
    return subprocess.run(
        [sys.executable, str(script), *args], cwd=str(where), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).returncode


def _engine_code(source, milestone, step, run=None):
    view = engine.published(source, milestone, step=step, run=run)
    return 2 if not view["stands"] else (0 if view["new"] else 1)


def test_the_gate_twin_answers_as_the_engine_does(proj):
    """tools/published.py is this repository's probe for a milestone await
    (the project layer names it); it must read the same two fields."""
    engine.start("main")
    asks = [("stack", "cut", "land", None), ("main", "cut-wanted", "standby", "stack")]
    assert _tool(PUBLISHED, proj, "stack", "cut", "--step", "land") == 2
    engine.start("stack", run="stack")
    below = proj / "deeper"
    below.mkdir()
    for moves in ([], [None, None], ["stack", "stack"], [None]):
        for run in moves:
            _step(run=run)
        for source, milestone, step, run in asks:
            want = _engine_code(source, milestone, step, run)
            got = _tool(PUBLISHED, below, source, milestone, "--step", step, run=run)
            assert got == want, (moves, source, milestone)
    assert _tool(PUBLISHED, proj, "stack", "cut", "-t", "nobody") == 2


def test_sub_done_all_may_leave_a_session_long_sub_run_out(proj, capsys):
    engine.start("main")
    engine.start("stack", run="stack")
    assert cli_cflow._cmd_sub_done(_ns(name=None, all=True)) == 1
    assert cli_cflow._cmd_sub_done(_ns(name=None, all=True, excluded=["stack"])) == 0
    assert _tool(SUB_DONE, proj, "--all") == 1
    assert _tool(SUB_DONE, proj, "--all", "--except", "stack") == 0


def test_probe_env_names_the_asking_run(monkeypatch):
    monkeypatch.setenv(state_mod.RUN_ENV, "leaked")
    assert engine.probe_env("s1", run="stack")[state_mod.RUN_ENV] == "stack"
    assert state_mod.RUN_ENV not in engine.probe_env("s1", run="")
    token = state_mod.push_run("stack")
    try:
        assert engine.probe_env("s1")[state_mod.RUN_ENV] == "stack"
    finally:
        state_mod.pop_run(token)
    assert state_mod.RUN_ENV not in engine.probe_env("s1")


# --------------------------------------------------------------------------- #
# landing_queue on a sub run
# --------------------------------------------------------------------------- #
def test_a_sub_definition_may_hold_the_landing_queue(proj):
    assert model.parse(QUEUED_SUB).landing_queue is not None
    engine.start("main")
    assert engine.landing_queue_run() is None
    engine.start("queued", run="stack")
    assert engine.landing_queue_run() == "stack"


def test_the_mcp_tools_that_take_a_run_advertise_it(proj):
    """A sub run may hold the queue, so the queue tool must say it takes
    `run` (s763's review of claunch-u8wjx.1); set_state reads it the same way."""
    schemas = {t["name"]: t["inputSchema"]["properties"] for t in mcp.TOOLS}
    for name in ("landing_queue", "set_state"):
        assert "run" in schemas[name], name
    engine.start("main")
    engine.start("queued", run="stack")
    engine.enqueue_landing(["x-1"], "w1-feature", "0" * 40, by="w1", run="stack")
    listed = mcp.call_tool("landing_queue", {"run": "stack"})
    assert [e["issue"] for e in listed["landing_queue"]] == ["x-1"]
    moved = mcp.call_tool("landing_queue", {"run": "stack", "issue": "x-1", "status": "rejected"})
    assert moved["status"] == "landing_marked"
    assert not mcp.call_tool("landing_queue", {}).get("landing_queue")  # main holds none


def test_only_one_run_of_a_scope_holds_the_landing_queue(proj):
    engine.start("qmain")
    assert engine.landing_queue_run() == state_mod.MAIN_RUN
    with pytest.raises(CflowError, match="already holds one"):
        engine.start("queued", run="stack")


# --------------------------------------------------------------------------- #
# the daemon drives sub runs
# --------------------------------------------------------------------------- #
GATED = """
name: gated
kind: subflow
steps:
  hold:
    instructions: wait for the flag
    checklist:
      prompt: is the flag there?
      then: after
      poll: 30
      items:
        - id: flag
          describe: the flag file exists
          check: 'python -c "import os,sys; sys.exit(0 if os.path.exists(\\"flag\\") else 1)"'
  after:
    instructions: carry on
"""


class _FakeManager:
    def get(self, name):
        raise KeyError(name)


def test_known_slots_lists_main_runs_then_sub_runs(proj):
    engine.start("main")
    engine.start("stack", run="stack")
    slots = [(scope, run) for _, scope, run in state_mod.known_slots()]
    assert slots == [("s1", None), ("s1", "stack")]


def test_for_run_names_the_sub_run_under_the_header():
    from claude_launcher.daemon import cflow_clock

    block = "---\n# claunch cflow: x\nstep: a\n---"
    assert cflow_clock.for_run(block, None) == block
    lines = cflow_clock.for_run(block, "stack").splitlines()
    assert lines[2].startswith("run: stack")


def test_a_sub_run_hears_its_awaits_signal_and_no_step_reminders(proj, monkeypatch):
    from claude_launcher.daemon import cflow_clock

    engine.start("main")
    engine.start("stack", run="stack")
    calls = []
    codes = {"code": 1}

    def fake_probe(command, cwd, timeout, *, scope, inputs=None, values=None, run=None):
        calls.append((command, scope, run))
        return {"code": codes["code"], "says": "probe"}

    monkeypatch.setattr(cflow_clock.cflow_engine, "run_probe", fake_probe)
    monkeypatch.setattr(cflow_clock.store, "daemon_config", lambda: {})
    clock = cflow_clock.ReminderClock(_FakeManager())
    assert clock.scan(1000.0) == []                  # baselines only
    codes["code"] = 0
    due = clock.scan(1100.0)
    signals = [d for d in due if d[3] == "signal"]
    assert len(signals) == 1
    assert "run: stack" in signals[0][2] and "exit 1 -> exit 0" in signals[0][2]
    probed = {run for command, scope, run in calls if "cut-wanted" in command}
    assert probed == {"stack"}
    # past any reminder interval the sub run is still never restated
    later = clock.scan(100000.0)
    assert not [d for d in later if "run: stack" in d[2] and d[3] == "reminder"]


def test_the_checklist_clock_opens_a_sub_runs_gate(proj):
    from claude_launcher.daemon import cflow_clock

    (proj / ".claunch" / "workflows" / "gated.yaml").write_text(GATED, encoding="utf-8")
    engine.start("main")
    engine.start("gated", run="gate")
    engine.report("waiting for the flag", run="gate")
    assert engine.status(run="gate")["status"] == "waiting_checklist"
    clock = cflow_clock.ChecklistClock(_FakeManager())
    assert clock.scan(now=1000.0) == []
    (proj / "flag").touch()
    moved = clock.scan(now=1040.0)
    assert len(moved) == 1 and "run: gate" in moved[0][2]
    assert engine.status(run="gate")["step_id"] == "after"
    assert engine.status()["step_id"] == "work"     # the main run never moved


def test_a_mutual_wait_is_reported_once_per_pair_of_positions(proj):
    engine.start("main")
    engine.start("stack", run="stack")
    assert engine.sync_deadlock(run="stack") is None      # main is at work
    _step()                                                # at request
    engine.goto("land", by="user", reason="skip the request")  # publishes nothing
    stuck = engine.sync_deadlock(run="stack")
    assert stuck == {
        "sub": "stack",
        "main_step": "land", "main_waits_for": "stack.cut",
        "sub_step": "standby", "sub_waits_for": "main.cut-wanted",
    }
    assert engine.sync_deadlock(run="stack") is None      # said once
    events = [e["event"] for e in state_mod.read_journal(str(proj), "s1")]
    assert events.count("sync_deadlock") == 1
    # a move that unsticks it clears the record; getting stuck again reports again
    engine.goto("request", by="user", reason="ask properly")
    assert engine.sync_deadlock(run="stack") is None
    engine.next_step()                                     # a goto's step is fetched first
    _step()                                                # request -> land, publishes
    assert engine.sync_deadlock(run="stack") is None      # the sub has something new
