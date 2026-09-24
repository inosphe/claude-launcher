"""The ``stack`` sub definition and the improv-worker steps that meet it.

claunch-u8wjx.2. A worker that spawns children keeps them on a session-long
stack branch driven by a ``stack`` sub run beside its main run; the two runs
meet only at four milestones:

    main  -> stack  cut-wanted   improv-worker stack-cut (before landing)
    stack -> main   cut          stack cut
    main  -> stack  frozen       improv-worker integration-request
    main  -> stack  landed       improv-worker landed (checklist)

What this file pins is the wiring the prose relies on — names, edges and the
two sentences of the cut rule — not the prose itself. The handshake's
semantics (counted consumption, goto publishes nothing) are pinned in
``tests/test_cflow_milestones.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_launcher.cflow import model, state as state_mod

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "claude_launcher" / "workflows"
PROJECT = ROOT / ".claunch" / "workflows"


def _project(name):
    return model.compose(PROJECT / f"{name}.yaml", resolve=state_mod.base_resolver(str(ROOT))).workflow


@pytest.fixture(scope="module")
def stack():
    return model.load(BUNDLED / "stack.yaml")


@pytest.fixture(scope="module")
def worker():
    return model.load(BUNDLED / "improv-worker.yaml")


def test_stack_is_a_sub_definition_that_holds_the_session_queue(stack):
    assert stack.kind == model.KIND_SUBFLOW
    assert stack.start == "open"
    assert stack.inputs["issue"].required and stack.inputs["base"].required
    spec = stack.landing_queue
    assert spec.branch("s9") == "s9-stack" and spec.reset_at == "cut"
    # its one intended loop is the standby hub; nothing else to warn about
    assert all(w.startswith("cycle detected") for w in stack.warnings)


def test_stack_milestones_match_the_worker(stack, worker):
    published_by_main = {m for s in worker.steps.values() for m in s.publishes}
    awaited_by_stack = {s.awaits.main for s in stack.steps.values() if s.awaits and s.awaits.main}
    assert awaited_by_stack == {"cut-wanted", "frozen", "landed"} <= published_by_main | awaited_by_stack
    assert awaited_by_stack <= published_by_main
    assert stack.steps["cut"].publishes == ("cut",)
    merge = worker.steps["stack-merge"].awaits
    assert (merge.sub, merge.at) == ("stack", "cut")
    assert worker.steps["stack-cut"].publishes == ("cut-wanted",)
    assert worker.steps["integration-request"].publishes == ("frozen",)
    assert worker.steps["landed"].publishes == ("landed",)


def test_stack_standby_routes_every_event_and_seals(stack):
    options = stack.steps["standby"].select.options
    assert {o: options[o].next for o in options} == {
        "land": "land", "cut": "cut", "submitted": "submitted",
        "restack": "restack", "seal": "seal",
    }
    assert stack.steps["seal"].next is None


def test_the_cut_rule_is_written_where_the_cut_is_chosen(stack, worker):
    prompt = stack.steps["standby"].select.prompt
    assert "착지를 요청한 자식은 기다린다" in prompt
    assert "다음 컷으로" in prompt and "사용자가 그렇게" in prompt
    assert "사용자가 지시했을 때만" in worker.steps["stack-cut"].instructions


def test_the_worker_meets_the_stack_only_where_a_stack_stands(worker):
    for step in ("stack-cut", "stack-merge"):
        assert worker.steps[step].skip, step
        assert worker.editable[model.skip_path(step)].by == ("agent",)
    assert worker.steps["queue-next"].select.options["drained"].next == "stack-cut"
    assert worker.steps["stack-cut"].next == "stack-merge"
    assert worker.steps["stack-merge"].next == "landing"
    work = worker.steps["work"].instructions
    assert "workflow: stack" in work and "sub: stack" in work and "rebase_onto: <내 세션>-stack" in work


def test_await_landing_declares_reopen_for_the_agent(worker):
    select = worker.steps["await-landing"].select
    assert select.options["reopen"].next == "landing-withdraw"
    assert "reopen" in select.prompt
    assert worker.steps["landing-withdraw"].next == "work"
    assert "WITHDRAWN @ <tip>" in worker.steps["landing-withdraw"].instructions


def test_a_delegation_only_round_skips_commit(worker):
    recheck = worker.steps["queue-recheck"].select.options
    assert recheck["stack-round"].next == "stack-round"
    round_ = worker.steps["stack-round"].select.options
    assert round_["cut"].next == "stack-cut" and round_["release"].next == "settle-check"


def test_a_stacked_branch_rebases_with_its_merges(worker):
    assert "--rebase-merges" in worker.steps["rebase"].instructions


def test_the_project_layer_asks_this_checkout(worker):
    """The gate rule: every await the project layer runs is a tools/ script."""
    project = _project("improv-worker")
    merge = project.steps["stack-merge"]
    assert merge.awaits.at == "cut"
    assert merge.awaits.command(merge) == (
        "uv run --no-sync python tools/published.py stack cut --step stack-merge"
    )
    work = project.steps["work"]
    assert work.awaits.command(work).endswith("tools/sub_done.py --all --except stack")
    stack = _project("stack")
    for step in ("cut", "submitted", "restack"):
        cmd = stack.steps[step].awaits.command(stack.steps[step])
        assert cmd.startswith("uv run --no-sync python tools/published.py main "), step
        assert cmd.endswith(f"--step {step}"), step


def test_peer_review_asks_the_parent_before_the_leader(worker):
    """A stack child's direct parent is the session that lands its branch,
    and the one member a grandchild is wired to (user decision 2026-09-24)."""
    groups = [(c.role, c.scope) for c in worker.steps["peer-review"].select.delegate.candidates]
    assert groups == [
        ("reviewer", "descendant"), ("reviewer", "sibling"), ("reviewer", "ancestor"),
        ("worker", "ancestor"),
        ("leader", "ancestor"), ("leader", "sibling"),
    ]


def test_every_milestone_wait_is_also_a_verify(stack, worker):
    """An await does not stop the run leaving; the verify does, so an early
    leave cannot record a stale count (s763's review of claunch-u8wjx.2)."""
    guarded = [("stack", s) for s in stack.steps.values() if s.awaits and s.awaits.milestone]
    guarded += [("worker", s) for s in worker.steps.values() if s.awaits and s.awaits.milestone]
    assert {s.id for _, s in guarded} == {"cut", "submitted", "restack", "stack-merge"}
    for _, s in guarded:
        assert s.verify is not None and "published" in s.verify.command, s.id
        assert f"--step {s.id}" in s.verify.command, s.id
    for name, step in (("stack", "cut"), ("improv-worker", "stack-merge")):
        cmd = _project(name).steps[step].verify.command
        assert cmd.startswith("uv run --no-sync python tools/published.py "), (name, step)
