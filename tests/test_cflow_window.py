"""Cadence: a paced select option is held until its window opens.

The leader used to merge and sweep once per finished task — every branch
whose evidence was complete was its own ``integrate``, and every integrate
its own full sweep. ``interval:`` on a select option is the fix at the
engine: the driver's take of that option is HELD until the interval since
the option's last take has passed, the daemon's clock releases it (moving
the run and waking the driver), and everything that became ready during
the wait rides in the same batch. These tests pin each half — the parse,
the hold, the renewal and cancel, the release (daemon and no-daemon), the
human override, and the leader declaration that uses it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_launcher.cflow import engine, mcp, model, state as state_mod
from claude_launcher.cflow.model import WorkflowError
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

PACED = """
name: batcher
recur: true
steps:
  standby:
    select:
      prompt: anything waiting?
      chooser: agent
      options:
        integrate: {description: merge what waits, next: merge, interval: 300}
        wind-down: {description: stop, next: end}
  merge:
    instructions: merge it
"""

T0 = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "batcher.yaml").write_text(PACED, encoding="utf-8")
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


def _first_round_merged(clock) -> None:
    """Round 1: the first take is immediate; the round ends; round 2 begins."""
    engine.start("batcher")
    payload = engine.select("integrate", "b1")
    assert (payload["status"], payload["step_id"]) == ("step", "merge")
    engine.report("merged b1")
    done = engine.next_step()
    assert done["status"] == "done" and done["pending_start"]["by"] == "recur"
    engine.start(done["pending_start"]["workflow"], done["pending_start"]["context"])


# --------------------------------------------------------------------------- #
# the declaration
# --------------------------------------------------------------------------- #
def test_interval_is_parsed_per_option():
    wf = model.parse(PACED)
    options = wf.steps["standby"].select.options
    assert options["integrate"].interval == 300
    assert options["wind-down"].interval is None


@pytest.mark.parametrize("bad", ["0", "-5", "soon", "true"])
def test_interval_must_be_a_positive_number_of_seconds(bad):
    with pytest.raises(WorkflowError, match="interval"):
        model.parse(PACED.replace("interval: 300", f"interval: {bad}"))


def test_an_option_refuses_keys_it_does_not_know():
    """A typo (``intervall``) must not parse into an unpaced option."""
    with pytest.raises(WorkflowError, match="unknown key"):
        model.parse(PACED.replace("interval: 300", "intervall: 300"))


# --------------------------------------------------------------------------- #
# the hold
# --------------------------------------------------------------------------- #
def test_the_first_take_is_immediate_and_recorded(proj, clock):
    engine.start("batcher")
    payload = engine.select("integrate", "b1")
    assert (payload["status"], payload["step_id"]) == ("step", "merge")
    key = state_mod.window_key("batcher", "standby", "integrate")
    assert state_mod.read_windows()[key] == _iso(T0)
    assert _events(event="select_confirmed", by="agent", option="integrate")


def test_a_take_inside_the_interval_is_held_not_refused(proj, clock):
    _first_round_merged(clock)
    clock(60)
    payload = engine.select("integrate", "b2")
    assert payload["status"] == "waiting_window"
    assert payload["option"] == "integrate"
    assert payload["opens_at"] == _iso(T0 + timedelta(seconds=300))
    assert payload["remaining"] == 240
    assert payload["interval"] == 300
    assert "do not poll" in payload["note"]
    # the run has not moved: still on standby, and every read agrees
    assert engine.status()["status"] == "waiting_window"
    assert engine.next_step()["status"] == "waiting_window"
    assert state_mod.load_state()["current"] == "standby"
    held = _events(event="select_held")
    assert held and held[-1]["reason"] == "b2" and held[-1]["renewed"] is False
    assert not _events(event="select_confirmed", reason="b2")


def test_the_hold_survives_the_round_boundary(proj, clock):
    """The record is the slot's, not the run's: round 2 paces against
    round 1's take even though round 1 was archived in between."""
    _first_round_merged(clock)
    assert engine.status()["round"] == 2
    clock(299)
    assert engine.select("integrate", "b2")["status"] == "waiting_window"


def test_reselecting_renews_the_reason_and_another_option_cancels(proj, clock):
    _first_round_merged(clock)
    clock(60)
    engine.select("integrate", "b2")
    clock(30)
    payload = engine.select("integrate", "b2 + b3")
    assert payload["status"] == "waiting_window"
    assert payload["held_since"] == _iso(T0 + timedelta(seconds=60))  # unchanged
    assert payload["opens_at"] == _iso(T0 + timedelta(seconds=300))    # unchanged
    renewed = _events(event="select_held", renewed=True)
    assert renewed and renewed[-1]["reason"] == "b2 + b3"

    done = engine.select("wind-down", "shift over")
    assert done["status"] == "done"
    cancelled = _events(event="select_hold_cancelled")
    assert cancelled and (cancelled[-1]["was"], cancelled[-1]["now"]) == ("integrate", "wind-down")
    assert not _events(event="select_confirmed", option="integrate", by="window")


# --------------------------------------------------------------------------- #
# the release
# --------------------------------------------------------------------------- #
def test_release_window_confirms_the_latest_reason_when_due(proj, clock):
    _first_round_merged(clock)
    clock(60)
    engine.select("integrate", "b2")
    engine.select("integrate", "b2 + b3")
    assert engine.release_window(now=T0 + timedelta(seconds=299)) is None
    assert state_mod.load_state()["current"] == "standby"

    moved = engine.release_window(now=T0 + timedelta(seconds=300))
    assert moved is not None
    assert (moved["step"], moved["option"], moved["now_at"]) == ("standby", "integrate", "merge")
    assert engine.status()["step_id"] == "merge"
    confirmed = _events(event="select_confirmed", by="window")
    assert confirmed and confirmed[-1]["reason"] == "b2 + b3"
    assert confirmed[-1]["held_since"] == _iso(T0 + timedelta(seconds=60))
    # the release IS a take: the next window is measured from it
    key = state_mod.window_key("batcher", "standby", "integrate")
    assert state_mod.read_windows()[key] == _iso(T0 + timedelta(seconds=300))


def test_next_releases_a_due_hold_without_a_daemon(proj, clock):
    _first_round_merged(clock)
    clock(60)
    engine.select("integrate", "b2")
    clock(240)
    payload = engine.next_step()
    assert (payload["status"], payload["step_id"]) == ("step", "merge")
    assert _events(event="select_confirmed", by="window")


def test_a_human_confirm_is_not_paced(proj, clock):
    """The override: a person taking the option from the CLI or dashboard
    goes through at once, and the journal says it was one."""
    _first_round_merged(clock)
    clock(60)
    engine.select("integrate", "b2")
    payload = engine.select("integrate", by="user")
    assert payload["status"] == "selected"
    assert state_mod.load_state()["current"] == "merge"
    confirmed = _events(event="select_confirmed", by="user")
    assert confirmed and confirmed[-1]["paced"] == "override"
    assert state_mod.load_state()["window"] is None


def test_goto_discards_a_held_choice(proj, clock):
    _first_round_merged(clock)
    clock(60)
    engine.select("integrate", "b2")
    engine.goto("merge", by="user")
    assert state_mod.load_state()["window"] is None
    discarded = _events(event="window_discarded")
    assert discarded and discarded[-1]["option"] == "integrate"
    # and nothing releases it later
    assert engine.release_window(now=T0 + timedelta(seconds=900)) is None


# --------------------------------------------------------------------------- #
# the daemon's clock
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []

    def status(self, threshold=None):
        return "idle"

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

    def list(self):
        return list(self._sessions.values())


def test_the_window_clock_releases_and_wakes_the_driver(proj, clock, monkeypatch):
    cwd = str(proj)
    monkeypatch.setenv(state_mod.SESSION_ENV, "w1")
    _first_round_merged(clock)
    clock(60)
    assert engine.select("integrate", "b2")["status"] == "waiting_window"

    session = _FakeSession("w1", cwd)
    wclock = cflow_clock.WindowClock(_FakeManager({"w1": session}))
    assert wclock.scan() == []                       # not due: nothing moves
    assert engine.status(cwd, scope="w1")["status"] == "waiting_window"

    clock(240)
    due = wclock.scan()
    assert [(c, s) for c, s, _ in due] == [(cwd, "w1")]
    assert engine.status(cwd, scope="w1")["step_id"] == "merge"
    block = due[0][2]
    assert "window opened" in block and "machine-generated" in block
    assert "'integrate'" in block and "step 'merge'" in block
    assert "nothing was decided for you" in block

    asyncio.run(wclock._deliver(cwd, "w1", block))
    assert session.delivered == [block]
    assert wclock.scan() == []                       # released once, not again


def test_the_clock_does_not_disturb_runs_with_no_hold(proj, clock, monkeypatch):
    monkeypatch.setenv(state_mod.SESSION_ENV, "w1")
    engine.start("batcher")
    wclock = cflow_clock.WindowClock(_FakeManager({}))
    assert wclock.scan() == []
    assert engine.status(str(proj), scope="w1")["status"] == "select"


# --------------------------------------------------------------------------- #
# what the protocol texts say
# --------------------------------------------------------------------------- #
def test_the_driver_skill_explains_the_held_status():
    from claude_launcher.cflow import install as cflow_install

    text = " ".join(cflow_install.SKILL_MD.split())
    assert "`waiting_window`" in text
    assert "STOP your turn and do not poll" in text
    assert "a different option cancels the hold" in text


def test_the_authoring_skill_documents_the_cadence():
    from claude_launcher.cflow import authoring

    text = " ".join(authoring.SKILL_MD.split())
    assert "`interval`" in text
    assert "interval: 300" in text
    assert "journaled as an override" in text


# --------------------------------------------------------------------------- #
# the leader declaration that asked for all this
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layer", ["bundled", "project"])
def test_the_leader_batches_integration_and_sweeps_after_a_gate(layer):
    """Merge every 5 minutes, not every task; then a gate; then the sweep.

    The gate between merge and sweep is the leader's own here (``otherwise:
    self``) — another autonomous declaration may put a role or a person at
    the same door, which is why it is a real ``ask`` and not prose.
    """
    if layer == "bundled":
        wf = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    else:
        wf = model.load(OVERRIDES / "improv-leader.yaml")

    integrate = wf.steps["standby"].select.options["integrate"]
    assert integrate.interval == 300
    assert integrate.next == "integrate-preflight"
    assert wf.steps["standby"].select.options["wind-down"].interval is None
    assert "waiting_window" in wf.steps["standby"].instructions
    assert "reason" in wf.steps["standby"].select.prompt

    assert wf.steps["integrate"].next == "sweep"
    assert wf.steps["sweep"].next == "reflect"
    assert wf.steps["integrate"].verify is None       # the merge is not the sweep
    gate = wf.steps["sweep"].ask
    assert gate is not None and gate.delegate.otherwise == "self"
    assert not gate.delegate.candidates
    assert "subagent" in wf.steps["sweep"].instructions
    assert "sweep N/M" in wf.steps["sweep"].instructions
    if layer == "project":
        # Armed, but deliberately NOT with the suite. This assertion used to
        # read `"pytest" in ...`, from when the step's verify *was* the sweep
        # — which the engine runs synchronously on leaving the step, so the
        # leader ran a 178-second sweep inside the turn this same workflow
        # says it never sweeps in. The suite moved into a spawned subagent
        # (`tools/sweep.py run`) and this gate reads the receipt it leaves.
        # tests/test_sweep.py owns the behaviour; what belongs here is that
        # the step is still armed and still is not the sweep.
        verify = wf.steps["sweep"].verify
        assert verify is not None
        assert "pytest" not in verify.command, (
            "the sweep gate must not run the suite in the leader's turn"
        )
        assert "sweep.py check" in verify.command
