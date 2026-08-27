"""Recurrence owned by the daemon: `recur: {auto: true}`.

Plain ``recur: true`` keeps today's driver-performs-start flow. With auto,
the finished round's own request carries ``by: recur`` + ``auto`` (and the
run's mesh), the done payload tells the agent to END its turn instead of
starting, and :func:`cflow.engine.auto_start_next_round` — driven by
:class:`cflow_clock.RoundStartClock` — performs the start: round count,
context and mesh carry over, and the single-use request channel makes it
idempotent and restart-safe. Human requests and plain-``recur`` requests
are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import engine, model, mcp, state as state_mod
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

AUTO = """
name: autoloop
recur: {auto: true}
steps:
  one:
    instructions: do the round
"""

PLAIN = """
name: plainloop
recur: true
steps:
  one:
    instructions: do the round
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "autoloop.yaml").write_text(AUTO, encoding="utf-8")
    (d / ".claunch" / "workflows" / "plainloop.yaml").write_text(PLAIN, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    mcp._seen_run = None
    return d


def _events(**match) -> list:
    return [
        e for e in state_mod.read_journal()
        if all(e.get(k) == v for k, v in match.items())
    ]


def _end_round(workflow: str, *, context: str = "", mesh: str = "") -> dict:
    engine.start(workflow, context=context, mesh=mesh)
    engine.report("round done")
    return engine.next_step()          # ends the round; recur files the request


# --------------------------------------------------------------------------- #
# the request the round leaves behind
# --------------------------------------------------------------------------- #
def test_the_auto_request_carries_auto_mesh_and_round(proj):
    done = _end_round("autoloop", context="ctx1", mesh="m1")
    assert done["status"] == "done"
    pending = done["pending_start"]
    assert pending["by"] == "recur"
    assert pending["auto"] is True
    assert pending["mesh"] == "m1"
    assert pending["context"] == "ctx1"
    assert pending["round"] == 2


def test_a_plain_recur_request_is_not_auto(proj):
    done = _end_round("plainloop")
    assert done["pending_start"]["by"] == "recur"
    assert not done["pending_start"].get("auto")


def test_the_done_note_hands_an_auto_round_to_the_daemon(proj):
    done = _end_round("autoloop")
    assert "starts the next round itself" in done["note"]
    assert "Do not call 'start'" in done["note"]

    plain = _end_round("plainloop")
    assert "then start the next round" in plain["note"]
    assert "Do not call 'start'" not in plain["note"]


def test_a_human_request_keeps_its_own_note(proj):
    _end_round("plainloop")
    engine.request_start("autoloop", by="web")
    again = engine.status()
    assert again["pending_start"]["by"] == "web"
    assert "perform the requested start" in again["note"]


# --------------------------------------------------------------------------- #
# the daemon's start
# --------------------------------------------------------------------------- #
def test_auto_start_begins_round_two_with_carry_over(proj):
    _end_round("autoloop", context="ctx1", mesh="m1")
    started = engine.auto_start_next_round()
    assert started["workflow"] == "autoloop"
    assert started["round"] == 2
    assert started["step"] == "one"
    payload = engine.status()
    assert payload["status"] == "step"
    assert payload["round"] == 2
    assert payload["context"] == "ctx1"
    # mesh is a property of the RUN (delegations), not of the status payload
    assert state_mod.load_state()["mesh"] == "m1"
    assert _events(event="round_auto_started", round=2)
    # the request is consumed: another pass starts nothing
    assert engine.auto_start_next_round() is None
    assert state_mod.read_request() is None


def test_auto_start_ignores_a_human_request(proj):
    engine.request_start("autoloop", by="web")
    assert engine.auto_start_next_round() is None
    assert state_mod.read_request()["by"] == "web"


def test_auto_start_ignores_a_plain_recur_request(proj):
    _end_round("plainloop")
    assert engine.auto_start_next_round() is None
    assert state_mod.read_request()["by"] == "recur"


def test_auto_start_ignores_a_consumed_request(proj):
    _end_round("autoloop")
    engine.start(state_mod.read_request()["workflow"], context="manually")
    assert engine.auto_start_next_round() is None


# --------------------------------------------------------------------------- #
# the clock that starts it
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
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


def test_round_start_clock_starts_and_wakes_the_driver(proj):
    cwd = str(proj)
    engine.start("autoloop", cwd=cwd, scope="w1")
    engine.report("round done", cwd=cwd, scope="w1")
    engine.next_step(cwd=cwd, scope="w1")
    session = _FakeSession("w1", cwd)
    clock_clock = cflow_clock.RoundStartClock(_FakeManager({"w1": session}))
    started = clock_clock.scan()
    assert len(started) == 1
    cwd_out, scope_out, block = started[0]
    assert cwd_out == cwd and scope_out == "w1"
    assert "round started" in block and "round: 2" in block
    assert "step 'one'" in block
    assert engine.status(cwd, scope="w1")["round"] == 2
    assert clock_clock.scan() == []          # request consumed: nothing to start


def test_round_start_clock_leaves_plain_recur_and_humans_alone(proj):
    cwd = str(proj)
    engine.start("plainloop", cwd=cwd, scope="w2")
    engine.report("round done", cwd=cwd, scope="w2")
    engine.next_step(cwd=cwd, scope="w2")
    assert cflow_clock.RoundStartClock(_FakeManager({})).scan() == []


# --------------------------------------------------------------------------- #
# the bundled workflow that uses both tiers
# --------------------------------------------------------------------------- #
def test_the_bundled_poll_workflow_parses():
    from claude_launcher.cflow import state as sstate

    name = "poll"
    bundled = dict(sstate.bundled_workflows())
    assert name in bundled
    wf = model.load(bundled[name])
    assert wf.recur and wf.recur_auto
    assert wf.steps["wait"].timer is not None
    assert wf.steps["wait"].timer.then == "poll"
    assert wf.steps["poll"].next == "wait"
    assert wf.max_visits >= wf.steps["wait"].timer.max + 2
    assert not wf.warnings and not wf.advice
