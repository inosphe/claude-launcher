"""The errand workflow: one delegated task, then the daemon ends the session.

``errand`` fills the gap between ``default_child_cflow`` (the parent's full
worker procedure, far too heavy for one commit or one test run) and
``workflow: '-'`` (no run at all, so the session idles until the parent
kills it): one work step, a final report to the spawner, and an approval gate
before the run becomes ``done`` and the run event clock's kill-on-end reaps
the session. These tests pin both halves against the REAL bundled file, not
an inline copy.
"""

from __future__ import annotations

import asyncio

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import model, state as sstate
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef
from test_cflow import _driving_session, _mesh  # noqa: E402

import pytest

BUNDLED = dict(sstate.bundled_workflows())["errand"]


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project whose layer carries the bundled errand, verbatim."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "errand.yaml").write_text(
        BUNDLED.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return d


class _FakeSession:
    """Just enough of a managed session for the clock's kill-on-end."""

    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.recorded: list = []
        self.killed = False
        self.status_value = "idle"

    def status(self, threshold=None):
        return self.status_value

    def append_wal(self, text: str) -> bool:
        self.recorded.append(text)
        return True

    def kill(self, *, force: bool = False) -> None:
        self.killed = True
        self.exited = True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]

    def persist(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# the canon is the shape the gap needs: work, report, approval, then one shot
# --------------------------------------------------------------------------- #
def test_the_bundled_errand_is_a_one_shot_with_spawner_gate():
    wf = model.load(BUNDLED)
    assert wf.name == "errand"
    assert list(wf.steps) == ["work", "wrapup", "end-gate", "end-hold"]
    assert wf.start == "work"
    step = wf.steps["work"]
    assert step.next == "wrapup"
    assert step.done_when
    assert not wf.recur                  # one-shot: kill-on-end spares it otherwise
    assert wf.steps["wrapup"].next == "end-gate"
    gate = wf.steps["end-gate"]
    assert gate.ask is not None
    assert [c.role for c in gate.ask.delegate.candidates] == ["worker", "leader"]
    assert all(c.scope == "ancestor" for c in gate.ask.delegate.candidates)
    assert gate.ask.delegate.otherwise == model.OTHERWISE_HUMAN
    assert gate.ask.on_decline == "end-hold"
    assert gate.next is None and wf.steps["end-hold"].next is None
    # work and wrapup have no entry gate; the final gate is the only approval.
    assert step.ask is None and step.select is None
    assert step.timer is None and step.verify is None
    assert wf.steps["wrapup"].ask is None
    assert not wf.warnings and not wf.advice and not wf.deprecations


# --------------------------------------------------------------------------- #
# the engine path: start -> work report -> wrapup report -> spawner approval -> done
# --------------------------------------------------------------------------- #
def test_errand_run_waits_for_spawner_before_done(proj, monkeypatch):
    cwd = str(proj)
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"), parent="leader-boss")
    cflow_engine.start("errand", cwd=cwd, scope="driver")
    out = cflow_engine.status(cwd, scope="driver")
    assert out["status"] == "step" and out["step_id"] == "work"

    cflow_engine.report("committed abc123; targeted tests 12/12", cwd=cwd, scope="driver")
    cflow_engine.next_step(cwd=cwd, scope="driver")
    assert cflow_engine.status(cwd, scope="driver")["step_id"] == "wrapup"
    cflow_engine.report("wrapup sent to spawner; no remaining issues", cwd=cwd, scope="driver")
    waiting = cflow_engine.next_step(cwd=cwd, scope="driver")
    assert waiting["status"] in ("waiting_answer", "waiting_approval")
    assert waiting["reason"] in ("ask", "approval")
    assert "instructions" not in waiting
    assert cflow_engine.next_step(cwd=cwd, scope="driver")["status"] in (
        "waiting_answer", "waiting_approval"
    )

    token = sstate.push_scope("driver")
    try:
        cflow_engine.answer(
            waiting["ask"]["id"], "approve", by_session="boss", cwd=cwd
        )
    finally:
        sstate.pop_scope(token)
    assert cflow_engine.next_step(cwd=cwd, scope="driver")["step_id"] == "end-gate"
    cflow_engine.report("spawner approval recorded", cwd=cwd, scope="driver")
    cflow_engine.next_step(cwd=cwd, scope="driver")
    out = cflow_engine.status(cwd, scope="driver")
    assert out["status"] == "done"
    # the two conditions that would spare the driver are both absent:
    # the workflow does not recur, and no next start is pending
    assert not out.get("recur")
    assert not (out.get("pending_start") or {}).get("by")


def test_errand_spawner_decline_routes_to_hold(proj, monkeypatch):
    cwd = str(proj)
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("worker", "boss"), parent="worker-boss")
    cflow_engine.start("errand", cwd=cwd, scope="driver")
    cflow_engine.report("work complete", cwd=cwd, scope="driver")
    cflow_engine.next_step(cwd=cwd, scope="driver")
    cflow_engine.report("final wrapup sent", cwd=cwd, scope="driver")
    waiting = cflow_engine.next_step(cwd=cwd, scope="driver")
    token = sstate.push_scope("driver")
    try:
        cflow_engine.answer(
            waiting["ask"]["id"], "decline", "추가 확인 필요", by_session="boss", cwd=cwd
        )
    finally:
        sstate.pop_scope(token)
    assert cflow_engine.status(cwd, scope="driver")["step_id"] == "end-hold"


# --------------------------------------------------------------------------- #
# kill-on-end: the done errand run reaps its own session — record first
# --------------------------------------------------------------------------- #
def test_kill_on_end_reaps_the_errand_session(proj, monkeypatch):
    cwd = str(proj)
    _driving_session(monkeypatch)
    _mesh(monkeypatch, ("leader", "boss"), parent="leader-boss")
    cflow_engine.start("errand", cwd=cwd, scope="driver")
    worker = _FakeSession("driver", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"driver": worker}))
    clock.scan()                                # arm at the step
    cflow_engine.report("done; evidence in the report", cwd=cwd, scope="driver")
    cflow_engine.next_step(cwd=cwd, scope="driver")
    cflow_engine.report("wrapup sent to spawner", cwd=cwd, scope="driver")
    waiting = cflow_engine.next_step(cwd=cwd, scope="driver")
    token = sstate.push_scope("driver")
    try:
        cflow_engine.answer(
            waiting["ask"]["id"], "approve", by_session="boss", cwd=cwd
        )
    finally:
        sstate.pop_scope(token)
    cflow_engine.next_step(cwd=cwd, scope="driver")
    cflow_engine.report("spawner approval recorded", cwd=cwd, scope="driver")
    cflow_engine.next_step(cwd=cwd, scope="driver")
    run_id = cflow_engine.status(cwd, scope="driver")["run"]
    assert cflow_engine.status(cwd, scope="driver")["status"] == "done"
    assert (str(proj.resolve()), "driver") in sstate.known_runs()
    assert cflow_clock.session_for(clock.manager, str(proj.resolve()), "driver") is worker

    assert clock.scan() == []                   # not an overseer event
    assert len(worker.recorded) == 1            # the WAL landed first
    assert "session ended" in worker.recorded[0]
    assert [s[1] for s in clock._end_pending] == ["driver"]
    asyncio.run(clock._finish_end(cwd, "driver", run_id))
    assert worker.killed is True
    assert worker.exited is True
    assert clock.scan() == []                   # marked — never replayed
