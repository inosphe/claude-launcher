"""The PM role and its workflow: procedures written per child, not per run.

A PM plans a child's procedure as a small overlay file under
``.cflow/generated/`` and names that file in the spawn. Nothing about the
engine changes for this: the file is an ordinary ``extends:`` layer, the
daemon composes it before the session exists, and the run snapshots it at
start. What this file pins is the gate — which paths a spawn may name — and
that the packaged role and workflow exist and agree with each other.
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import engine, model, state as state_mod
from claude_launcher.daemon import mesh_roles, onboard

BASE = """
name: base
steps:
  work:
    instructions: do the work
    next: review
  review:
    instructions: review it
"""

OVERLAY = """
# generated-by: pm1 issue=claunch-x at=2026-09-10T00:00:00Z
extends: base
steps:
  wait-for-a:
    instructions: wait for the sibling's tip
    checklist:
      prompt: is the sibling landed?
      then: work
      items:
        - id: landed
          describe: the sibling's tip is an ancestor
          check: 'true'
  review:
    verify: 'true'
start: wait-for-a
"""

BROKEN = """
extends: nosuchbase
steps:
  work:
    verify: 'true'
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "base.yaml").write_text(BASE, encoding="utf-8")
    gen = state_mod.generated_workflows_dir(str(d))
    gen.mkdir(parents=True)
    (gen / "pm1-claunch-x.yaml").write_text(OVERLAY, encoding="utf-8")
    (gen / "broken.yaml").write_text(BROKEN, encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return d


# --------------------------------------------------------------------------- #
# the role and the workflow are packaged, and point at each other
# --------------------------------------------------------------------------- #
def test_pm_is_a_packaged_role():
    rs = mesh_roles.resolve()
    assert "pm" in rs.roles
    assert rs.infer("pm1") == "pm"
    assert rs.infer("planner2") == "pm"
    role = rs.get("pm")
    assert role.stall_watch is True
    assert ".cflow/generated/" in role.stance
    assert "never merge" in role.stance


def test_improv_pm_is_bundled_and_driven_by_the_pm_role():
    path = dict(state_mod.bundled_workflows())["improv-pm"]
    wf = model.load(path)
    assert wf.filter_roles == ("whitelist", ("pm",)) or (
        getattr(wf.filter_roles, "type", None) == "whitelist"
        and tuple(wf.filter_roles.roles) == ("pm",)
    )
    assert wf.default_role == "pm"
    assert wf.default_child_cflow == "improv-worker"
    assert wf.start == "intake"
    for step in ("intake", "plan", "validate", "spawn", "standby", "reconcile", "handoff", "end-gate"):
        assert step in wf.steps, step
    # The loop is closed by a person: the only termination is past the ask.
    assert wf.steps["end-gate"].ask is not None
    assert wf.steps["end-gate"].ask.on_decline == "standby"
    assert not wf.warnings or all("cycle" in w for w in wf.warnings)


def test_improv_pm_carries_the_shared_beads_block():
    """Same block as the worker, byte for byte — see test_beads_protocol."""
    from test_beads_protocol import BLOCK_END, BLOCK_START, _block, _bundled

    assert _block(_bundled("improv-pm")) == _block(_bundled("improv-worker"))
    text = _bundled("improv-pm").read_text(encoding="utf-8")
    assert text.count(BLOCK_START) == 1 and text.count(BLOCK_END) == 1


# --------------------------------------------------------------------------- #
# the gate: which files a spawn may name
# --------------------------------------------------------------------------- #
def test_generated_workflow_admits_the_generated_directory_only(proj, tmp_path):
    gen = state_mod.generated_workflows_dir(str(proj))
    good = state_mod.generated_workflow(".cflow/generated/pm1-claunch-x.yaml", cwd=str(proj))
    assert good == (gen / "pm1-claunch-x.yaml").resolve()
    # An absolute spelling is the same file.
    assert state_mod.generated_workflow(str(gen / "pm1-claunch-x.yaml"), cwd=str(proj)) == good

    # A declared layer is named, never pathed.
    with pytest.raises(model.WorkflowError, match="outside the generated"):
        state_mod.generated_workflow(".claunch/workflows/base.yaml", cwd=str(proj))
    # A subdirectory or a climb-out does not count as "inside".
    (gen / "deeper").mkdir()
    (gen / "deeper" / "x.yaml").write_text(OVERLAY, encoding="utf-8")
    with pytest.raises(model.WorkflowError, match="outside the generated"):
        state_mod.generated_workflow(".cflow/generated/deeper/x.yaml", cwd=str(proj))
    with pytest.raises(model.WorkflowError, match="outside the generated"):
        state_mod.generated_workflow(".cflow/generated/../../.claunch/workflows/base.yaml", cwd=str(proj))
    # Only workflow files, and only ones that exist.
    with pytest.raises(model.WorkflowError, match="not a workflow file"):
        state_mod.generated_workflow(".cflow/generated/notes.txt", cwd=str(proj))
    with pytest.raises(model.WorkflowError, match="not found"):
        state_mod.generated_workflow(".cflow/generated/missing.yaml", cwd=str(proj))


def test_generated_workflow_accepts_the_parents_directory_too(proj, tmp_path):
    """A parent in the main checkout writes the file; the child stands in
    its own worktree. The parent's generated directory is a second root."""
    child_cwd = tmp_path / "wt"
    child_cwd.mkdir()
    gen = state_mod.generated_workflows_dir(str(proj))
    path = state_mod.generated_workflow(
        str(gen / "pm1-claunch-x.yaml"), cwd=str(child_cwd), roots=(str(proj),)
    )
    assert path == (gen / "pm1-claunch-x.yaml").resolve()
    with pytest.raises(model.WorkflowError, match="outside the generated"):
        state_mod.generated_workflow(str(gen / "pm1-claunch-x.yaml"), cwd=str(child_cwd))


# --------------------------------------------------------------------------- #
# preflight: composed before a session exists, carried as the resolved path
# --------------------------------------------------------------------------- #
def _preflight(body, cwd, **kw):
    return onboard.preflight(body, mesh_mgr=None, session_name="w1", cwd=cwd, harness="py", **kw)


def test_preflight_admits_a_generated_overlay_and_records_its_path(proj):
    plan = _preflight({"workflow": ".cflow/generated/pm1-claunch-x.yaml"}, str(proj))
    expected = (state_mod.generated_workflows_dir(str(proj)) / "pm1-claunch-x.yaml").resolve()
    assert plan.workflow == str(expected)
    # The composed result is the overlay over its base: a new start step,
    # the base's steps inherited, the one verify grafted.
    composed = state_mod.load_workflow(plan.workflow, str(proj))
    assert composed.workflow.start == "wait-for-a"
    assert composed.workflow.steps["review"].verify.command == "true"
    assert composed.workflow.steps["work"].instructions.strip() == "do the work"


def test_preflight_refuses_a_broken_overlay_and_a_path_elsewhere(proj):
    with pytest.raises(onboard.OnboardError, match="generated workflow"):
        _preflight({"workflow": ".cflow/generated/broken.yaml"}, str(proj))
    with pytest.raises(onboard.OnboardError, match="outside the generated"):
        _preflight({"workflow": ".claunch/workflows/base.yaml"}, str(proj))
    # A name still resolves the declared way, and an unknown one still fails
    # the declared way.
    assert _preflight({"workflow": "base"}, str(proj)).workflow == "base"
    with pytest.raises(onboard.OnboardError, match="no workflow named"):
        _preflight({"workflow": "ghost"}, str(proj))


def test_preflight_uses_the_parents_directory_as_a_second_root(proj, tmp_path):
    child_cwd = tmp_path / "wt"
    child_cwd.mkdir()
    (child_cwd / ".claunch" / "workflows").mkdir(parents=True)
    (child_cwd / ".claunch" / "workflows" / "base.yaml").write_text(BASE, encoding="utf-8")
    path = state_mod.generated_workflows_dir(str(proj)) / "pm1-claunch-x.yaml"
    plan = _preflight({"workflow": str(path)}, str(child_cwd), parent="pm1", parent_cwd=str(proj))
    assert plan.workflow == str(path.resolve())


# --------------------------------------------------------------------------- #
# a run started on the generated file snapshots it: the file is free to go
# --------------------------------------------------------------------------- #
def test_a_run_on_a_generated_overlay_survives_the_file_being_rewritten(proj):
    path = state_mod.generated_workflows_dir(str(proj)) / "pm1-claunch-x.yaml"
    engine.start(str(path), cwd=str(proj), scope="w1")
    assert engine.status(str(proj), scope="w1")["step_id"] == "wait-for-a"
    path.write_text(BROKEN, encoding="utf-8")
    status = engine.status(str(proj), scope="w1")
    assert status["step_id"] == "wait-for-a"
    assert state_mod.load_snapshot(str(proj), "w1").steps["review"].verify.command == "true"
