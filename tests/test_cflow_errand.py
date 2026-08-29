"""The errand workflow: one delegated task, then the daemon ends the session.

``errand`` fills the gap between ``default_child_cflow`` (the parent's full
worker procedure, far too heavy for one commit or one test run) and
``workflow: '-'`` (no run at all, so the session idles until the parent
kills it): a single step whose completion makes the run ``done``, where the
run event clock's kill-on-end reaps the session. These tests pin both halves
against the REAL bundled file, not an inline copy — a rewording that grew a
second step, a recur, or a review gate would re-open the gap the workflow
exists to close.
"""

from __future__ import annotations

import asyncio

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import model, state as sstate
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

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
# the canon is the shape the gap needs: one step, one shot, no frills
# --------------------------------------------------------------------------- #
def test_the_bundled_errand_is_a_one_shot_single_step():
    wf = model.load(BUNDLED)
    assert wf.name == "errand"
    assert list(wf.steps) == ["work"]
    assert wf.start == "work"
    step = wf.steps["work"]
    # ``next: end`` parses to None (model.END is the reserved termination
    # target): leaving the one step finishes the run
    assert step.next is None
    assert step.done_when                # the model warns without a criterion
    assert not wf.recur                  # one-shot: kill-on-end spares it otherwise
    # nothing that would hold the session open: no human/peer gate, no timed
    # wait, no repo-specific command (that belongs to a project layer)
    assert step.ask is None and step.select is None
    assert step.timer is None and step.verify is None
    assert not wf.warnings and not wf.advice and not wf.deprecations


# --------------------------------------------------------------------------- #
# the engine path: start -> report -> next -> done
# --------------------------------------------------------------------------- #
def test_errand_run_goes_start_report_next_done(proj):
    cwd = str(proj)
    cflow_engine.start("errand", cwd=cwd, scope="w1")
    out = cflow_engine.status(cwd, scope="w1")
    assert out["status"] == "step" and out["step_id"] == "work"

    cflow_engine.report("committed abc123; targeted tests 12/12", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    out = cflow_engine.status(cwd, scope="w1")
    assert out["status"] == "done"
    # the two conditions that would spare the driver are both absent:
    # the workflow does not recur, and no next start is pending
    assert not out.get("recur")
    assert not (out.get("pending_start") or {}).get("by")


# --------------------------------------------------------------------------- #
# kill-on-end: the done errand run reaps its own session — record first
# --------------------------------------------------------------------------- #
def test_kill_on_end_reaps_the_errand_session(proj):
    cwd = str(proj)
    cflow_engine.start("errand", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()                                # arm at the step
    cflow_engine.report("done; evidence in the report", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    run_id = cflow_engine.status(cwd, scope="w1")["run"]

    assert clock.scan() == []                   # not an overseer event
    assert len(worker.recorded) == 1            # the WAL landed first
    assert "session ended" in worker.recorded[0]
    assert [s[1] for s in clock._end_pending] == ["w1"]
    asyncio.run(clock._finish_end(cwd, "w1", run_id))
    assert worker.killed is True
    assert worker.exited is True
    assert clock.scan() == []                   # marked — never replayed
