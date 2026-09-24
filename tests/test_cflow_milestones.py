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


@pytest.mark.parametrize(
    "text, refused",
    [
        # only a sub run has a main run to read
        ("steps:\n  a:\n    instructions: x\n    awaits: {main: m}\n", "only a 'kind: subflow'"),
        # a milestone belongs to one named sub run
        ("steps:\n  a:\n    instructions: x\n    awaits: {sub: all, at: m}\n", "name the run"),
        ("steps:\n  a:\n    instructions: x\n    awaits: {at: m}\n", "give the run as 'sub'"),
        ("kind: subflow\nsteps:\n  a:\n    instructions: x\n    awaits: {main: m, probe: 'true'}\n",
         "takes neither"),
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


def test_only_one_run_of_a_scope_holds_the_landing_queue(proj):
    engine.start("qmain")
    assert engine.landing_queue_run() == state_mod.MAIN_RUN
    with pytest.raises(CflowError, match="already holds one"):
        engine.start("queued", run="stack")
