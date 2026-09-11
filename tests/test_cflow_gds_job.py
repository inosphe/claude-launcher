"""The gds-job workflow: an application-created analysis session ends itself.

``gds-job`` is the run an application (gds6's plane) attaches to the one-shot
analysis sessions it creates through ``POST /api/sessions``. Those sessions
run on a harness with no cflow tools (pi), so the run cannot be advanced by
its driver: both steps are ``timer:`` steps the daemon moves, the fire budget
carries across the work <-> overdue round trip, and the run reaches ``done``
with no human gate — which is the condition the run event clock's
kill-on-end reaps a session on. A driver WITH tools takes ``next: end``
instead. These tests pin the shape and both endings against the REAL bundled
file, not an inline copy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow import model, state as sstate

import pytest

BUNDLED = dict(sstate.bundled_workflows())["gds-job"]


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project whose layer carries the bundled gds-job, verbatim."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "gds-job.yaml").write_text(
        BUNDLED.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return d


T0 = datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """The engine's clock, owned by the test: ``clock(seconds)`` advances it."""
    now = {"at": T0}
    monkeypatch.setattr(cflow_engine, "_utc_now", lambda: now["at"])

    def advance(seconds: float = 0) -> datetime:
        now["at"] = now["at"] + timedelta(seconds=seconds)
        return now["at"]

    return advance


# --------------------------------------------------------------------------- #
# the canon: two timed steps, no gate anywhere, an analyst's run
# --------------------------------------------------------------------------- #
def test_the_bundled_gds_job_is_two_timers_with_no_human_gate():
    wf = model.load(BUNDLED)
    assert wf.name == "gds-job"
    assert list(wf.steps) == ["work", "overdue"]
    assert wf.start == "work"
    assert not wf.recur                      # one-shot: kill-on-end applies
    assert wf.default_child_cflow is None    # a parentless run pairs nothing
    assert wf.filter_roles is not None
    assert wf.filter_roles.type == "whitelist"
    assert list(wf.filter_roles.roles) == ["analyst"]
    assert wf.default_role == "analyst"
    for step in wf.steps.values():
        # nothing here waits on a person or on another session
        assert step.ask is None and step.select is None
        assert step.checklist is None and step.verify is None
        assert step.timer is not None
        assert step.next is None           # `next: end` — the tooled driver's early exit
        assert step.done_when or step.id == "overdue"
    work, overdue = wf.steps["work"], wf.steps["overdue"]
    assert (work.timer.then, work.timer.after) == ("overdue", model.END)
    assert (overdue.timer.then, overdue.timer.after) == ("work", model.END)
    assert work.timer.max == 1 and overdue.timer.max == 1
    # the round trip's visits fit the budget with room for a human goto
    assert wf.max_visits >= 2 * (work.timer.max + 1) + overdue.timer.max + 1
    # the deadline is minutes, not hours: an application's own job timeout
    # is 10 minutes and a session left for a day is the incident this fixes
    assert 600 <= work.timer.every <= 1800 and overdue.timer.every <= 900


# --------------------------------------------------------------------------- #
# the tool-less ending: the daemon's timer alone carries the run to done
# --------------------------------------------------------------------------- #
def test_timer_fires_alone_carry_a_toolless_session_to_done(proj, clock):
    cwd = str(proj)
    scope = "job-0123456789ab"
    cflow_engine.start("gds-job", cwd=cwd, scope=scope)
    out = cflow_engine.status(cwd, scope=scope)
    assert out["status"] == "waiting_timer" and out["step_id"] == "work"
    assert out["opens_at"]        # armed by start itself — nobody moved the run here
    wf = model.load(BUNDLED)
    work_every = wf.steps["work"].timer.every
    overdue_every = wf.steps["overdue"].timer.every

    # nothing is due before the deadline
    clock(work_every - 1)
    assert cflow_engine.fire_timer(cwd=cwd, scope=scope) is None

    # deadline: work -> overdue (the daemon's read of the new position arms it)
    clock(1)
    fired = cflow_engine.fire_timer(cwd=cwd, scope=scope)
    assert fired and fired["moved_to"] == "overdue"
    assert cflow_engine.status(cwd, scope=scope)["step_id"] == "overdue"

    # last chance elapsed: overdue -> work, with work's fire count carried
    clock(overdue_every)
    fired = cflow_engine.fire_timer(cwd=cwd, scope=scope)
    assert fired and fired["moved_to"] == "work"
    assert cflow_engine.status(cwd, scope=scope)["step_id"] == "work"
    assert cflow_engine.status(cwd, scope=scope)["fires"] == 1

    # the fire past work's budget closes the run: done, nothing pending
    clock(work_every)
    fired = cflow_engine.fire_timer(cwd=cwd, scope=scope)
    assert fired and fired["moved_to"] == "end"
    final = cflow_engine.status(cwd, scope=scope)
    assert final["status"] == "done"
    assert not final.get("recur") and not final.get("pending_by")
    events = [e["event"] for e in sstate.read_journal(cwd, scope)]
    assert "timer_budget_spent" in events
    assert "timer_re_armed" in events
    assert "gate_approved" not in events      # nobody was asked anything
    # the whole wait is the documented deadline: minutes, not hours
    assert 2 * work_every + overdue_every <= 3600


# --------------------------------------------------------------------------- #
# the tooled ending: report + next from work is done on the spot
# --------------------------------------------------------------------------- #
def test_a_driver_with_tools_ends_the_run_from_work_at_once(proj):
    cwd = str(proj)
    scope = "job-abcdef012345"
    cflow_engine.start("gds-job", cwd=cwd, scope=scope)
    cflow_engine.report("accepted after 2 attempts", cwd=cwd, scope=scope)
    cflow_engine.next_step(cwd=cwd, scope=scope)
    assert cflow_engine.status(cwd, scope=scope)["status"] == "done"
