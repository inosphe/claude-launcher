"""A/B onboarding starts distinct executable v2 charts, not prose-only tasks."""
import asyncio
import json
from pathlib import Path

import pytest

from claude_launcher.cflow2 import runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import onboard, reposync_agent
from claude_launcher import reposync_verify
from test_reposync_workspaces import world, _git, _commit


@pytest.mark.parametrize("mode,name", [("agent", "reposync-merge-push-v2"),
                                      ("on_failure", "reposync-conflict-v2")])
def test_onboarding_starts_selected_workflow(home, tmp_path, mode, name):
    project = tmp_path / "project"
    definitions = project / ".claunch" / "workflows" / "v2"
    definitions.mkdir(parents=True)
    source = Path(__file__).parents[1] / "src/claude_launcher/workflows/v2" / (name + ".yaml")
    (definitions / source.name).write_text(source.read_text())
    card = {"repo": str(project), "workspace": "proj", "remote": "origin", "branch": "master",
            "local": "a" * 40, "upstream": "b" * 40, "action": "rebase"}
    assignment = reposync_agent.assignment(card, mode=mode, error="conflict in x" if mode == "on_failure" else "")
    # Standalone onboarding exercises workflow startup; mesh role admission is
    # covered by the mesh role catalog and API tests.
    assignment.pop("role")
    plan = onboard.preflight(assignment, mesh_mgr=None, session_name="sync1",
                             cwd=str(project), harness="claude")
    assert plan.workflow == name and plan.workflow_version == 2
    report, opening = asyncio.run(onboard.arrange(plan, name="sync1", cwd=str(project), mesh_mgr=None))
    assert report["workflow"]["ok"] and "flow_status" in opening
    status = runtime.status(cwd=str(project), scope="sync1", tick=False)
    assert status["tasks"][0]["label"] == "inspect"
    assert json.loads(status["inputs"]["assignment"]) == card
    chart = RunStore(str(project), "sync1").load_chart()
    if mode == "on_failure":
        assert not any("git.push" in n.props.get("effects", []) for n in chart.nodes.values())
    else:
        assert chart.node("execute").props["ask"]["otherwise"] == "human"


def test_candidate_verifier_detects_original_branch_changes(world, tmp_path):
    wt = tmp_path / "candidate"
    old = _git(world["a"], "rev-parse", "master")
    _git(world["a"], "worktree", "add", "-b", "candidate", str(wt))
    assignment = {"repo": str(world["a"]), "branch": "master", "local": old}
    assert reposync_verify.candidate(str(wt), assignment)["original"] == old
    _commit(world["a"], "concurrent.txt")
    with pytest.raises(Exception, match="original source changed"):
        reposync_verify.candidate(str(wt), assignment)


def test_managed_worker_cannot_report_past_local_landing(world, monkeypatch):
    from test_local_sync import setup
    from test_cflow2_improv_worker import _medium_flow
    setup(world)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(runtime, "_launch", lambda fn, *args: fn(*args))
    kw = {"cwd": str(world["a"]), "scope": "policy-test"}
    st = runtime.start(_medium_flow(world["a"], verified=True), caps={}, **kw)
    task = st["tasks"][0]
    st = runtime.report(task["instance"], "ok", {"medium": "local", "reason": "forced local"}, **kw)
    assert st["tasks"][0]["label"].rsplit("/", 1)[-1] == "local-landing-policy"
    runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    st = runtime.status(**kw)
    assert st["tasks"][0]["label"].rsplit("/", 1)[-1] == "local-landing-policy"
    state = RunStore(**kw).load_state()
    assert "landing" not in state["data"]


def test_manager_workflow_returns_to_wait_for_next_request(world, monkeypatch):
    from claude_launcher.cflow2 import layers
    from test_local_sync import setup, bind
    registry, request = setup(world)
    bind(registry)
    registry.apply(request["id"], session="manager", epoch=1)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(runtime, "_launch", lambda fn, *args: fn(*args))
    kw = {"cwd": str(world["a"]), "scope": "service-test"}
    st = runtime.start(layers.load("local-sync-v2"), caps={},
                       inputs={"request_id": request["id"], "epoch": "1"}, **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok",
                        {"request_id": request["id"], "epoch": 1}, **kw)
    runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    st = runtime.status(**kw)
    assert st["tasks"][0]["label"] == "receipt"
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    assert st["tasks"][0]["label"] == "inspect"
    assert st["status"] == "running"


def test_approval_candidate_binding_rejects_changed_head(world, tmp_path, monkeypatch):
    wt = tmp_path / "candidate"
    old = _git(world["a"], "rev-parse", "master")
    _git(world["a"], "worktree", "add", "-b", "candidate", str(wt))
    monkeypatch.chdir(wt)
    monkeypatch.setattr("sys.argv", ["reposync_verify", "candidate"])
    monkeypatch.setenv("CFLOW_DATA_INPUTS", json.dumps({"assignment": json.dumps({
        "repo": str(world["a"]), "branch": "master", "local": old})}))
    monkeypatch.setenv("CFLOW_DATA_PREPARED", json.dumps({
        "candidate": old, "tree": _git(wt, "rev-parse", "HEAD^{tree}"), "plan": "reviewed"}))
    assert reposync_verify.main() == 0
    _commit(wt, "new.txt")
    assert reposync_verify.main() == 1
