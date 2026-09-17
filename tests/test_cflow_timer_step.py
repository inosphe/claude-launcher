"""Timed steps: a step's `timer:` moves the run on the daemon's schedule.

The tier INSIDE a recurring round: while the run sits at `wait` it reports
`waiting_timer`, the engine arms it (``opens_at`` = arrival + ``every``),
each due fire moves the run to ``timer.then`` — a paid visit whose count
survives the ``then`` round trip — and the fire past the budget moves it to
``timer.after``, closing the inner loop. These tests pin the parse, the
arming and its carry/keep rules, the payload, the fires and the budget, the
agent's early exit, and the TimerClock's scan that performs and announces
the fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_launcher.cflow import engine, model, mcp, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

POLL = """
name: poller
steps:
  poll:
    instructions: check the mesh
    next: wait
  wait:
    timer: {every: 300, max: 3, then: poll, after: end}
    instructions: wait for the timer
    next: end
"""

T0 = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "poller.yaml").write_text(POLL, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return d


@pytest.fixture
def clock(monkeypatch):
    """The engine's clock, owned by the test: ``clock(seconds)`` advances it."""
    now = {"at": T0}
    monkeypatch.setattr(engine, "_utc_now", lambda: now["at"])

    def advance(seconds: float = 0) -> datetime:
        now["at"] = now["at"] + timedelta(seconds=seconds)
        return now["at"]

    return advance


def _events(**match) -> list:
    return [
        e for e in state_mod.read_journal()
        if all(e.get(k) == v for k, v in match.items())
    ]


def _at_wait() -> None:
    """Round in progress: start, poll once, advance onto the timer step."""
    engine.start("poller")
    engine.report("first poll")
    engine.next_step()


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_timer_is_parsed():
    wf = model.parse(POLL)
    timer = wf.steps["wait"].timer
    assert (timer.every, timer.max, timer.then, timer.after) == (300, 3, "poll", "end")
    assert wf.steps["poll"].timer is None
    assert wf.steps["wait"].successors() == [None, None]  # next + after, both end


@pytest.mark.parametrize(
    "text, match",
    [
        ("every: 0, max: 1, then: poll, after: end", "every"),
        ("every: 300, max: 0, then: poll, after: end", "max"),
        ("every: 300, max: 1, then: nobody, after: end", "then"),
        ("every: 300, max: 1, then: wait, after: end", "then"),
        ("every: 'soon', max: 1, then: poll, after: end", "every"),
        ("every: 300, max: 1, then: poll", "after"),
    ],
)
def test_timer_parse_errors(text, match):
    with pytest.raises(WorkflowError, match=match):
        model.parse(POLL.replace(
            "timer: {every: 300, max: 3, then: poll, after: end}",
            f"timer: {{{text}}}",
        ))


def test_timer_refuses_awaits_and_select_on_the_same_step():
    with pytest.raises(WorkflowError, match="awaits"):
        model.parse(POLL.replace(
            "instructions: wait for the timer",
            "instructions: wait for the timer\n    awaits: verify\n    verify: 'echo hi'",
        ))
    with pytest.raises(WorkflowError, match="select"):
        model.parse(POLL.replace(
            "instructions: wait for the timer",
            "select:\n      prompt: p\n      options:\n        o: {description: d, next: end}",
        ))


# --------------------------------------------------------------------------- #
# arming
# --------------------------------------------------------------------------- #
def test_arrival_arms_the_timer(proj, clock):
    _at_wait()
    state = state_mod.load_state()
    armed = state["timer_armed"]
    assert armed["step"] == "wait"
    assert armed["fires"] == 0
    assert armed["opens_at"] == (T0 + timedelta(seconds=300)).isoformat(
        timespec="seconds"
    )
    assert _events(event="timer_armed", step="wait", carried=False)


def test_arrival_reports_waiting_timer(proj, clock):
    _at_wait()
    payload = engine.status()
    assert payload["status"] == "waiting_timer"
    assert payload["fires"] == 0
    assert payload["max"] == 3
    assert payload["then"] == "poll"
    assert payload["after"] == "end"
    assert payload["opens_at"] == (T0 + timedelta(seconds=300)).isoformat(
        timespec="seconds"
    )
    assert "do not poll" in payload["note"]
    # not an agent-actionable position: the reminder and stall clocks stay out
    assert not cflow_clock._actionable(payload)


# --------------------------------------------------------------------------- #
# fires and the budget
# --------------------------------------------------------------------------- #
def test_a_fire_before_the_window_moves_nothing(proj, clock):
    _at_wait()
    clock(299)
    assert engine.fire_timer() is None
    assert engine.status()["status"] == "waiting_timer"


def test_a_due_fire_moves_the_run_to_then(proj, clock):
    _at_wait()
    clock(300)
    moved = engine.fire_timer()
    assert moved == {
        "run": state_mod.load_state()["run_id"],
        "workflow": "poller",
        "step": "wait",
        "fires": 1,
        "max": 3,
        "moved_to": "poll",
        "opens_at": (T0 + timedelta(seconds=300)).isoformat(timespec="seconds"),
    }
    assert engine.status()["step_id"] == "poll"  # delivered on the next read
    assert _events(event="timer_fired", fires=1, max=3)


def test_the_fire_count_survives_the_round_trip(proj, clock):
    """Returning to the timestep carries the budget: it counts POLLS, not
    visits."""
    _at_wait()
    clock(300)
    engine.fire_timer()
    engine.next_step()          # delivery of the poll step
    engine.report("polled")
    engine.next_step()          # back onto wait: fire count carried
    state = state_mod.load_state()
    assert state["timer_armed"]["fires"] == 1
    assert _events(event="timer_re_armed", carried=True, fires=1)
    clock(300)
    moved = engine.fire_timer()
    assert moved["fires"] == 2
    assert moved["moved_to"] == "poll"


def test_the_fire_past_the_budget_closes_the_round(proj, clock):
    _at_wait()
    for _ in range(3):
        clock(300)
        engine.fire_timer()
        engine.next_step()
        engine.report("polled")
        engine.next_step()
    clock(300)                  # the 4th fire: budget spent
    moved = engine.fire_timer()
    assert moved["fires"] == 4
    assert moved["moved_to"] == "end"
    done = engine.status()
    assert done["status"] == "done"
    state = state_mod.load_state()
    assert "timer_armed" not in state
    assert "timer_fires" not in state
    assert _events(event="timer_budget_spent", fires=4, max=3)


def test_the_agent_can_close_the_round_early(proj, clock):
    _at_wait()
    engine.report("closing early")
    engine.next_step()
    assert engine.status()["status"] == "done"
    state = state_mod.load_state()
    assert "timer_armed" not in state
    assert "timer_fires" not in state


# --------------------------------------------------------------------------- #
# the clock that fires
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []

    def queue_delivery(self, text: str) -> bool:
        self.delivered.append(text)
        return True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def test_timer_clock_scan_fires_and_wakes_the_driver(proj, clock):
    cwd = str(proj)
    engine.start("poller", cwd=cwd, scope="w1")
    engine.report("first poll", cwd=cwd, scope="w1")
    engine.next_step(cwd=cwd, scope="w1")
    session = _FakeSession("w1", cwd)
    clock_clock = cflow_clock.TimerClock(_FakeManager({"w1": session}))
    assert clock_clock.scan() == []                 # armed, not due yet
    clock(300)
    fired = clock_clock.scan()
    assert len(fired) == 1
    cwd_out, scope_out, block = fired[0]
    assert cwd_out == cwd and scope_out == "w1"
    assert "timer fired" in block and "'wait' (1/3)" in block
    assert "step 'poll'" in block
    assert engine.status(cwd, scope="w1")["step_id"] == "poll"
    assert clock_clock.scan() == []                 # moved on: nothing to say


def test_timer_clock_leaves_non_timer_runs_alone(proj, clock):
    cwd = str(proj)
    engine.start("poller", cwd=cwd, scope="w2")
    assert cflow_clock.TimerClock(_FakeManager({})).scan() == []


def test_timer_serialized_for_the_run_page():
    """The dashboard's workflow view carries a timed step's schedule, so
    the drawing can say what the engine is doing: `every` is the cadence
    the diagram shows, `max` the fires per round, and then/after where the
    run goes by fire and by budget."""
    from claude_launcher.daemon.api import _serialize_workflow

    view = _serialize_workflow(model.parse(POLL))
    wait = next(s for s in view["steps"] if s["id"] == "wait")
    assert wait["timer"] == {
        "every": 300, "max": 3, "then": "poll", "after": "end",
    }
    poll = next(s for s in view["steps"] if s["id"] == "poll")
    assert poll["timer"] is None
