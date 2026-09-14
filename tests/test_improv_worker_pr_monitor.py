"""``improv-worker-pr-monitor``: the PR wizard's watcher is a bundled,
standalone, report-only run.

The wizard (``POST /api/sessions/{name}/pr``, ``monitor: true``) spawns a
child of the session on this workflow with the PR's facts as the run
context. These tests pin the shape the wizard and the parent's own run rely
on rather than the prose:

* it loads on its own -- no ``extends``, no ``verify``, no ``inputs`` (a
  standalone run starts with context) -- and volunteers for no role;
* the round is intake -> watch (a daemon timer) -> check (an agent select)
  -> conflict | landed | wrapup -> end, and the timer's budget closes it;
* every step's prose keeps it report-only: no git write, no merge, no kill.
"""

from __future__ import annotations

from claude_launcher.cflow import model, state as state_mod
from claude_launcher.daemon import api as api_mod

NAME = "improv-worker-pr-monitor"


def _doc() -> dict:
    path = dict(state_mod.bundled_workflows())[NAME]
    return model.read_doc(path.read_text(encoding="utf-8"), where=str(path))


def _wf() -> model.Workflow:
    return state_mod.load_bundled(NAME)


def test_it_is_the_workflow_the_wizard_spawns():
    assert api_mod.PR_MONITOR_WORKFLOW == NAME
    assert NAME in dict(state_mod.bundled_workflows())


def test_it_loads_standalone_and_volunteers_for_no_role():
    doc = _doc()
    assert model.extends_ref(doc) is None
    assert "inputs" not in doc           # a standalone run starts with context
    wf = _wf()
    assert wf.name == NAME
    assert wf.default_role is None       # the wizard's role=worker keeps improv-worker
    assert wf.start == "intake"
    assert all(step.verify is None for step in wf.steps.values())


def test_the_round_is_a_timed_watch_closed_by_its_budget():
    wf = _wf()
    assert list(wf.steps) == ["intake", "watch", "check", "conflict", "landed", "wrapup"]
    assert wf.steps["intake"].next == "watch"
    timer = wf.steps["watch"].timer
    assert timer is not None
    assert timer.then == "check" and timer.after == "wrapup"
    assert timer.every >= 60             # a PR does not change by the second
    # every fire is a visit, so the loop guard sits above the budget
    assert wf.max_visits > timer.max
    check = wf.steps["check"].select
    assert check is not None and check.chooser == "agent"
    assert {k: o.next for k, o in check.options.items()} == {
        "keep-watching": "watch", "conflict": "conflict",
        "merged": "landed", "closed": "wrapup",
    }
    assert wf.steps["conflict"].next == "watch"    # asked, then keeps watching
    assert wf.steps["landed"].next == "wrapup"
    assert wf.steps["wrapup"].next is None         # end


def test_every_step_keeps_it_report_only():
    wf = _wf()
    prose = "\n".join(
        (s.instructions or "") + (s.select.prompt if s.select else "")
        for s in wf.steps.values()
    )
    for phrase in ("보고 전용", "손대지 않는다", "gh pr view"):
        assert phrase in prose
    assert "세션을 죽이는 명령" in wf.steps["wrapup"].instructions
    # the conflict goes UP as an ask; rebasing is the parent's job
    assert "--type ask" in wf.steps["conflict"].instructions
    assert "리베이스" in wf.steps["conflict"].instructions
    # landed is the agent reading MERGED off gh, not a fetch of the parent's repo
    assert "MERGED" in wf.steps["landed"].instructions
    assert "mergeCommit" in wf.steps["landed"].instructions
    # the leader's queue marker is not this run's to write
    assert "LANDING REQUEST" in wf.steps["wrapup"].instructions
    assert "PR MONITOR:" in wf.steps["wrapup"].instructions
