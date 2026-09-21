"""Declarative daemon side effects: a step's ``triggers:``.

Two capabilities of the daemon — re-taking a session's Y/N status checks and
recomposing its LLM briefing — have no tool a run can call for itself, so a
workflow that wanted either asked its agent for it in prose. ``triggers:``
is the declarative form: the step names the capability and the moment, the
engine records the moment as it performs the move, and
:class:`~claude_launcher.daemon.cflow_clock.TriggerClock` performs it.

What these tests hold in place:

* the spelling, including the refusals — an unknown action, an unknown
  moment, a duplicate, and the ``on:`` that PyYAML turns into a boolean;
* one queued entry per visit, at the moment declared, recorded by the engine
  rather than diffed out of positions by a clock;
* a claim is single-use, so a restarted daemon does not repeat an action;
* and the failure policy: every "could not" is journalled and the run's
  position is untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher.cflow import engine, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import briefing, cflow_clock, status_checks
from claude_launcher.daemon.harness import SessionDef


FLOW = """
name: triggered
steps:
  one:
    instructions: do one
    triggers: [briefing]
    next: two
  two:
    instructions: do two
    triggers:
      - do: checks
        at: leave
      - do: briefing
        at: enter
    next: three
  three:
    instructions: do three
    triggers:
      - do: checks
        at: leave
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "triggered.yaml").write_text(FLOW, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return d


def _journal(cwd, event):
    return [e for e in state_mod.read_journal(cwd) if e.get("event") == event]


# --------------------------------------------------------------------------- #
# the spelling
# --------------------------------------------------------------------------- #
def test_the_shorthand_is_the_action_name_on_entering():
    steps = model.parse(FLOW).steps
    assert steps["one"].triggers == (
        model.Trigger(do="briefing", at="enter"),
    )
    assert steps["two"].triggers == (
        model.Trigger(do="checks", at="leave"),
        model.Trigger(do="briefing", at="enter"),
    )


def test_a_step_with_no_triggers_carries_an_empty_tuple():
    assert model.parse(
        "name: t\nsteps:\n  a:\n    instructions: x\n"
    ).steps["a"].triggers == ()


@pytest.mark.parametrize("declared, expected", [
    ("triggers: [nope]", "unknown trigger action"),
    ("triggers: [{do: checks, at: never}]", "unknown moment"),
    ("triggers: [checks, {do: checks}]", "declared twice"),
    ("triggers: {do: checks}", "must be a list"),
    ("triggers: [[checks]]", "must be an action"),
    ("triggers: [{do: checks, when: leave}]", "unknown key"),
])
def test_a_misspelled_trigger_is_refused_rather_than_ignored(declared, expected):
    with pytest.raises(WorkflowError) as exc:
        model.parse(f"name: t\nsteps:\n  a:\n    instructions: x\n    {declared}\n")
    assert expected in str(exc.value)


def test_the_moment_is_spelled_at_because_yaml_reads_on_as_a_boolean():
    # PyYAML's 1.1 resolver: `on:` arrives as the key True. The message has
    # to say so, or the author reads "unknown key: True" and has nowhere to
    # go with it.
    with pytest.raises(WorkflowError) as exc:
        model.parse(
            "name: t\nsteps:\n  a:\n    instructions: x\n"
            "    triggers: [{do: checks, on: leave}]\n"
        )
    assert "boolean key" in str(exc.value)
    assert "'at:'" in str(exc.value)


def test_a_trigger_is_allowed_on_a_select_step():
    # Unlike `restart:`, a trigger gates nothing, so there is no reason to
    # keep it off the one kind of step whose exit an agent chooses.
    steps = model.parse(
        "name: t\n"
        "steps:\n"
        "  a:\n"
        "    triggers: [briefing]\n"
        "    select:\n"
        "      prompt: which?\n"
        "      options:\n"
        "        go: {description: go}\n"
    ).steps
    assert steps["a"].triggers == (model.Trigger(do="briefing", at="enter"),)


# --------------------------------------------------------------------------- #
# what the engine records, and when
# --------------------------------------------------------------------------- #
def test_the_start_step_is_an_arrival_like_any_other(proj):
    engine.start("triggered")
    claimed = engine.claim_triggers()
    assert [(c["do"], c["at"], c["step"], c["visit"]) for c in claimed] == [
        ("briefing", "enter", "one", 1)
    ]


def test_both_moments_are_recorded_by_the_move_that_passed_them(proj):
    engine.start("triggered")
    engine.claim_triggers()          # the start step's own arrival
    engine.report("did one")
    engine.next_step()               # one -> two: `two` is entered
    assert [c["do"] for c in engine.claim_triggers()] == ["briefing"]
    engine.report("did two")
    engine.next_step()               # two -> three: `two` is left
    claimed = engine.claim_triggers()
    assert [(c["do"], c["at"], c["step"]) for c in claimed] == [
        ("checks", "leave", "two")
    ]


def test_a_leave_on_the_last_step_survives_the_run_ending(proj):
    # The entries a run queues on its way out describe how it ended, so a
    # finished run is claimed from too.
    engine.start("triggered")
    for summary in ("did one", "did two", "did three"):
        engine.report(summary)
        engine.next_step()
    assert engine.status()["status"] == "done"
    claimed = engine.claim_triggers()
    # Nothing drained this run, so the whole round is here in order — and
    # the last entry is the one queued by the move that ended it.
    assert [(c["do"], c["at"], c["step"]) for c in claimed] == [
        ("briefing", "enter", "one"),
        ("briefing", "enter", "two"),
        ("checks", "leave", "two"),
        ("checks", "leave", "three"),
    ]


def test_each_visit_queues_its_own_entry(proj):
    looped = """
name: looped
steps:
  a:
    instructions: a
    triggers: [briefing]
    select:
      prompt: again?
      options:
        again: {description: loop, next: a}
        stop: {description: done}
"""
    (proj / ".claunch" / "workflows" / "looped.yaml").write_text(looped, encoding="utf-8")
    engine.start("looped")
    assert [c["visit"] for c in engine.claim_triggers()] == [1]
    engine.select("again", reason="round two")
    assert [c["visit"] for c in engine.claim_triggers()] == [2]


def test_the_position_payload_says_what_the_daemon_will_do_here(proj):
    engine.start("triggered")
    assert engine.status()["triggers"] == [{"do": "briefing", "at": "enter"}]
    engine.report("did one")
    engine.next_step()
    assert engine.status()["triggers"] == [
        {"do": "checks", "at": "leave"},
        {"do": "briefing", "at": "enter"},
    ]


def test_a_step_with_no_triggers_carries_no_payload_field(proj):
    engine.start("triggered")
    for summary in ("did one", "did two"):
        engine.report(summary)
        engine.next_step()
    payload = engine.status()
    assert payload["step_id"] == "three"
    assert payload["triggers"] == [{"do": "checks", "at": "leave"}]
    engine.report("did three")
    engine.next_step()
    assert "triggers" not in engine.status()      # the run is done


def test_a_claim_is_single_use(proj):
    engine.start("triggered")
    assert len(engine.claim_triggers()) == 1
    assert engine.claim_triggers() == []


def test_queued_and_done_are_both_journalled(proj):
    cwd = str(proj)
    engine.start("triggered")
    assert [e["do"] for e in _journal(cwd, "trigger_queued")] == ["briefing"]
    engine.claim_triggers()
    engine.complete_trigger(
        do="briefing", step_id="one", visit=1, performed=True, detail="recomposed"
    )
    assert [e["detail"] for e in _journal(cwd, "trigger_done")] == ["recomposed"]


def test_a_skip_is_journalled_and_moves_nothing(proj):
    cwd = str(proj)
    engine.start("triggered")
    engine.claim_triggers()
    engine.complete_trigger(
        do="briefing", step_id="one", visit=1, performed=False,
        detail="no llm endpoint is configured",
    )
    assert _journal(cwd, "trigger_done") == []
    assert [e["detail"] for e in _journal(cwd, "trigger_skipped")] == [
        "no llm endpoint is configured"
    ]
    # The run has not moved, and nothing is holding it.
    payload = engine.status()
    assert payload["step_id"] == "one"
    assert payload["status"] == "step"


def test_the_queue_is_capped_when_nobody_drains_it(proj, monkeypatch):
    monkeypatch.setattr(engine, "TRIGGER_QUEUE_MAX", 2)
    looped = """
name: capped
steps:
  a:
    instructions: a
    triggers: [briefing]
    select:
      prompt: again?
      options:
        again: {description: loop, next: a}
        stop: {description: done}
"""
    (proj / ".claunch" / "workflows" / "capped.yaml").write_text(looped, encoding="utf-8")
    engine.start("capped")
    for _ in range(3):
        engine.select("again", reason="again")
    claimed = engine.claim_triggers()
    assert len(claimed) == 2
    # The OLDEST went: what survives is the two most recent visits.
    assert [c["visit"] for c in claimed] == [3, 4]
    assert _journal(str(proj), "trigger_dropped")


def test_a_graph_review_shows_what_each_step_asks_the_daemon_for(proj, capsys):
    # `claunch cflow show` is where a reviewer reads a workflow's declared
    # properties. A trigger costs the driver context or an LLM call, so a
    # review that cannot see it reads the step as free.
    from types import SimpleNamespace

    from claude_launcher import cli_cflow

    cli_cflow._cmd_show(SimpleNamespace(workflow="triggered", cwd=None))
    out = capsys.readouterr().out
    assert "triggers: briefing at enter" in out
    assert "triggers: checks at leave, briefing at enter" in out


# --------------------------------------------------------------------------- #
# what the clock does with a claim
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.queued: list = []
        self.queue_ok = True

    def queue_delivery(self, text: str) -> bool:
        if not self.queue_ok:
            return False
        self.queued.append(text)
        return True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def _clock(proj, session=None):
    sessions = {"w1": session} if session is not None else {}
    return cflow_clock.TriggerClock(_FakeManager(sessions))


def test_the_scan_hands_back_what_the_engine_queued(proj):
    cwd = str(proj)
    engine.start("triggered", cwd=cwd, scope="w1")
    found = _clock(proj).scan()
    assert [(c, s, a["do"]) for c, s, a in found] == [(cwd, "w1", "briefing")]
    assert _clock(proj).scan() == []


def test_checks_queues_the_same_request_the_operator_button_sends(proj, monkeypatch):
    cwd = str(proj)
    session = _FakeSession("w1", cwd)
    monkeypatch.setattr(
        status_checks, "session_entries",
        lambda name, enabled_only=False: [{"id": "green", "name": "master green"}],
    )
    engine.start("triggered", cwd=cwd, scope="w1")
    engine.report("did one", cwd=cwd, scope="w1")
    engine.next_step(cwd=cwd, scope="w1")
    engine.report("did two", cwd=cwd, scope="w1")
    engine.next_step(cwd=cwd, scope="w1")       # leaves `two`
    clock = _clock(proj, session)
    action = [a for _, _, a in clock.scan() if a["do"] == "checks"][0]
    performed, detail = asyncio.run(clock._perform(cwd, "w1", action))
    assert performed and "1 status check" in detail
    assert session.queued == [status_checks.REFRESH_PROMPT]


def test_checks_with_nothing_configured_types_nothing(proj, monkeypatch):
    cwd = str(proj)
    session = _FakeSession("w1", cwd)
    monkeypatch.setattr(
        status_checks, "session_entries", lambda name, enabled_only=False: []
    )
    engine.start("triggered", cwd=cwd, scope="w1")
    clock = _clock(proj, session)
    performed, detail = asyncio.run(
        clock._perform(cwd, "w1", {"do": "checks", "step": "two", "visit": 1})
    )
    assert not performed
    assert "no enabled status checks" in detail
    assert session.queued == []


def test_briefing_recomposes_without_typing_into_the_terminal(proj, monkeypatch):
    cwd = str(proj)
    session = _FakeSession("w1", cwd)
    seen = {}

    async def fake_compose(sess, cfg, *, refresh=False):
        seen["refresh"] = refresh
        seen["session"] = sess.sdef.name
        return {"briefing": {"state": "working"}}

    monkeypatch.setattr(briefing, "llm_config", lambda: {"endpoint": "e", "model": "m", "api_key": "k"})
    monkeypatch.setattr(briefing, "llm_configured", lambda cfg: True)
    monkeypatch.setattr(briefing, "compose", fake_compose)
    engine.start("triggered", cwd=cwd, scope="w1")
    clock = _clock(proj, session)
    performed, detail = asyncio.run(
        clock._perform(cwd, "w1", {"do": "briefing", "step": "one", "visit": 1})
    )
    assert performed and "working" in detail
    assert seen == {"refresh": True, "session": "w1"}
    assert session.queued == []          # the driver spends no context


def test_briefing_without_an_endpoint_is_a_skip_not_an_error(proj, monkeypatch):
    cwd = str(proj)
    monkeypatch.setattr(briefing, "llm_configured", lambda cfg: False)
    engine.start("triggered", cwd=cwd, scope="w1")
    clock = _clock(proj, _FakeSession("w1", cwd))
    performed, detail = asyncio.run(
        clock._perform(cwd, "w1", {"do": "briefing", "step": "one", "visit": 1})
    )
    assert not performed
    assert "llm" in detail


def test_a_failing_endpoint_is_a_skip_not_an_error(proj, monkeypatch):
    cwd = str(proj)

    async def boom(sess, cfg, *, refresh=False):
        raise briefing.BriefingError("endpoint said 502")

    monkeypatch.setattr(briefing, "llm_configured", lambda cfg: True)
    monkeypatch.setattr(briefing, "llm_config", lambda: {})
    monkeypatch.setattr(briefing, "compose", boom)
    engine.start("triggered", cwd=cwd, scope="w1")
    clock = _clock(proj, _FakeSession("w1", cwd))
    performed, detail = asyncio.run(
        clock._perform(cwd, "w1", {"do": "briefing", "step": "one", "visit": 1})
    )
    assert not performed
    assert "502" in detail


def test_a_run_with_no_live_session_is_a_skip(proj):
    cwd = str(proj)
    engine.start("triggered", cwd=cwd, scope="w1")
    performed, detail = asyncio.run(
        _clock(proj)._perform(cwd, "w1", {"do": "briefing", "step": "one", "visit": 1})
    )
    assert not performed
    assert "no live session" in detail


def test_an_action_this_daemon_does_not_know_is_a_skip(proj):
    # A run started from a workflow snapshot a newer claunch wrote.
    cwd = str(proj)
    engine.start("triggered", cwd=cwd, scope="w1")
    performed, detail = asyncio.run(
        _clock(proj, _FakeSession("w1", cwd))._perform(
            cwd, "w1", {"do": "teleport", "step": "one", "visit": 1}
        )
    )
    assert not performed
    assert "teleport" in detail


# --------------------------------------------------------------------------- #
# the workflow that declares them
# --------------------------------------------------------------------------- #
def test_improv_worker_declares_the_triggers_it_stopped_asking_for_in_prose():
    from pathlib import Path

    import claude_launcher

    root = Path(claude_launcher.__file__).parent / "workflows" / "improv-worker.yaml"
    workflow = model.parse(root.read_text(encoding="utf-8"))
    declared = {
        step_id: {(t.do, t.at) for t in step.triggers}
        for step_id, step in workflow.steps.items() if step.triggers
    }
    assert declared == {
        "work": {("briefing", "enter")},
        "commit": {("checks", "leave")},
        "integration-request": {("briefing", "enter")},
        "landed": {("checks", "leave")},
        "wrapup": {("briefing", "enter")},
    }
    # The prose it replaces is gone: the step no longer tells the agent to
    # call the two MCP tools off its own bat.
    commit = workflow.steps["commit"].instructions
    assert "요청이 도착하면" in commit
    assert "커밋 또는 `--no-ff` 병합을 마칠 때마다 그" not in commit
