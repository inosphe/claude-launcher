"""No simulation runs while a session's cflow run stands on a goal-setting
step (claunch-efnbt.9): the ``sims: deny`` step/task marker, the predicate
(sim/run_gate.py), the sim API refusal of sim_start/sim_round, and the marks
on the canonical workflows."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import engine, model, state as state_mod
from claude_launcher.cflow2 import check, frontend, ir, runtime
from claude_launcher.sim import run_gate, store as S

from test_sim_api import PATCH, run

V1_FLOW = """\
name: gated
steps:
  goal:
    title: 목표 수립
    sims: deny
    instructions: settle the goal
    next: work
  work:
    instructions: do it
"""

V2_FLOW = """\
cflow: 2
name: gated2
nodes:
  goal: {title: 목표 수립, prompt: settle the goal, sims: deny}
  work: {prompt: do it}
body:
  seq: [goal, work]
"""


def _v1_run(tmp_path, scope="s1"):
    wf = tmp_path / ".claunch" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "gated.yaml").write_text(V1_FLOW, encoding="utf-8")
    engine.start("gated", cwd=str(tmp_path), scope=scope)
    return str(tmp_path)


def _v2_run(tmp_path, scope="s1"):
    runtime.start(frontend.compile_text(V2_FLOW), cwd=str(tmp_path), scope=scope, caps={})
    return str(tmp_path)


# -- the marker ----------------------------------------------------------------

def test_v1_step_takes_sims_deny_and_refuses_other_values(tmp_path):
    path = tmp_path / "gated.yaml"
    path.write_text(V1_FLOW, encoding="utf-8")
    wf = model.load(path)
    assert wf.steps["goal"].sims == "deny" and wf.steps["work"].sims is None
    path.write_text(V1_FLOW.replace("sims: deny", "sims: allow"), encoding="utf-8")
    with pytest.raises(model.WorkflowError, match="'sims' takes one value"):
        model.load(path)


def test_v2_task_carries_sims_deny_as_a_node_prop():
    chart = frontend.compile_text(V2_FLOW)
    marked = {n.label: n.props.get("sims") for n in chart.nodes.values() if n.type == ir.T_TASK}
    assert marked == {"goal": "deny", "work": None}
    with pytest.raises(frontend.FrontendError, match="sims takes one value"):
        frontend.compile_text(V2_FLOW.replace("sims: deny", "sims: maybe"))


def test_checker_refuses_a_sims_deny_task_that_calls_sim_run():
    chart = frontend.compile_text(V2_FLOW)
    goal = next(n for n in chart.nodes.values() if n.label == "goal")
    work = next(n for n in chart.nodes.values() if n.label == "work")
    goal.props["calls"] = ["sim-run"]
    work.props["calls"] = ["sim-run"]  # not marked: allowed
    found = [f for f in check.errors(chart) if f.code == "sims-deny-call"]
    assert [f.node for f in found] == [goal.id]
    assert "sim-run" in found[0].message


# -- the predicate -------------------------------------------------------------

def test_v1_run_on_a_marked_step_is_blocked_and_leaving_it_clears(tmp_path):
    cwd = _v1_run(tmp_path)
    b = run_gate.blocked(cwd, "s1")
    assert b is not None and (b.version, b.step, b.title) == (1, "goal", "목표 수립")
    assert "목표 승인 후 실행 가능" in b.message("sim_start") and "goal" in b.message()
    assert run_gate.blocked(cwd, "other") is None  # another session's slot: no run
    engine.report("goal settled", cwd=cwd, scope="s1")
    assert engine.next_step(cwd=cwd, scope="s1")["step_id"] == "work"
    assert run_gate.blocked(cwd, "s1") is None


def test_v2_run_on_a_marked_task_is_blocked_and_leaving_it_clears(tmp_path):
    cwd = _v2_run(tmp_path)
    b = run_gate.blocked(cwd, "s1")
    assert b is not None and (b.version, b.step) == (2, "goal")
    runtime.report("goal", cwd=cwd, scope="s1")
    assert run_gate.blocked(cwd, "s1") is None


def test_no_run_is_not_blocked(tmp_path):
    assert run_gate.blocked(str(tmp_path), "s1") is None
    assert run_gate.blocked("", "s1") is None


# -- the sim API ---------------------------------------------------------------

def _running(tmp_path, sid):
    """Put a campaign in ``running`` without a run session: the state a patch
    reruns a round in. Since claunch-efnbt.8 every API start launches a run
    session, so the test reaches it through the store the API reads."""
    S.SimStore(tmp_path / "sims").start(sid, S.USER, round=False)


def test_sim_api_refuses_start_and_round_in_a_goal_step_and_allows_the_rest(tmp_path):
    async def scenario(req, mgr, s1, s2):
        cwd = s1.sdef.cwd
        _v1_run(Path(cwd), scope="s1")
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1",
                                               "settings": {"max_rounds": 1}}, status=201))["sim"]["id"]
        # Building the model is allowed.
        assert (await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH}))["ok"]
        refused = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
        assert "목표 승인 후 실행 가능" in refused["error"] and "goal" in refused["error"]
        assert refused["sims_denied"]["step"] == "goal" and refused["sims_denied"]["version"] == 1
        refused = await req("POST", f"/api/sims/{sid}/round", {"session": "s1"}, status=409)
        assert refused["error"].startswith("sim_round refused")
        # Reads stay open.
        assert (await req("GET", f"/api/sims/{sid}"))["sim"]["id"] == sid
        # The user is never restricted.
        assert (await req("POST", f"/api/sims/{sid}/round", {}))["result"]
        # Leaving the goal step lifts the refusal.
        engine.report("goal settled", cwd=cwd, scope="s1")
        engine.next_step(cwd=cwd, scope="s1")
        assert (await req("POST", f"/api/sims/{sid}/round", {"session": "s1"}))["result"]
    run(tmp_path, scenario)


def test_goal_step_refusal_answers_before_the_runner_fault(tmp_path):
    """A start with no interpreter profile faults the simulation (claunch-efnbt.8);
    in a goal-setting step the 409 is the goal refusal and the record does not
    move to fault."""
    async def scenario(req, mgr, s1, s2):
        _v2_run(Path(s1.sdef.cwd), scope="s1")
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1"}, status=201))["sim"]["id"]
        assert (await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH}))["ok"]
        refused = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
        assert "목표 승인 후 실행 가능" in refused["error"] and "fault" not in refused
        shown = (await req("GET", f"/api/sims/{sid}"))["sim"]
        assert shown["state"] != "fault"
        # Out of the goal step the same start meets the fault, as before.
        runtime.report("goal", cwd=s1.sdef.cwd, scope="s1")
        faulted = await req("POST", f"/api/sims/{sid}/start", {"session": "s1"}, status=409)
        assert faulted["fault"] == "no-profile" and faulted["sim"]["state"] == "fault"
    run(tmp_path, scenario)


def test_sim_api_skips_the_auto_round_of_a_patch_in_a_goal_step(tmp_path):
    async def scenario(req, mgr, s1, s2):
        sid = (await req("POST", "/api/sims", {"title": "door", "session": "s1",
                                               "settings": {"max_rounds": 5}}, status=201))["sim"]["id"]
        assert (await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": PATCH}))["ok"]
        _running(tmp_path, sid)
        first = await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": {
            "events": [{"name": "close", "kind": "actor"}]}})
        assert first["ok"] and "round" in first  # no run yet: the round runs
        _v2_run(Path(s1.sdef.cwd), scope="s1")
        again = await req("POST", f"/api/sims/{sid}/model", {"session": "s1", "patch": {
            "events": [{"name": "kick", "kind": "actor"}]}})
        assert again["ok"] and "round" not in again
        assert "목표 승인 후 실행 가능" in again["round_skipped"]
    run(tmp_path, scenario)


# -- the canonical workflows -----------------------------------------------------

V1_MARKED = {
    "improv-worker": ["intake", "issue-check", "issue-auto", "issue-search",
                      "issue-decision", "issue-adopt", "issue-create"],
    "improv-leader": ["intake"],
}
V2_MARKED = {
    "improv-worker-v2": ["intake", "prior-context", "prior-context-fallback", "intake-issue",
                         "issue-auto", "issue-search", "issue-adopt", "issue-create"],
    "improv-leader-v2": ["intake"],
}


@pytest.mark.parametrize("name", sorted(V1_MARKED))
def test_canonical_v1_goal_steps_are_marked(name):
    wf = state_mod.load_bundled(name)
    assert [s.id for s in wf.steps.values() if s.sims == "deny"] == V1_MARKED[name]


@pytest.mark.parametrize("name", sorted(V2_MARKED))
def test_canonical_v2_goal_tasks_are_marked(name):
    path = state_mod.bundled_workflows_dir() / "v2" / f"{name}.yaml"
    chart = frontend.compile_file(path)
    marked = sorted({n.label for n in chart.nodes.values()
                     if n.type == ir.T_TASK and n.props.get("sims") == "deny"})
    assert marked == sorted(V2_MARKED[name])
