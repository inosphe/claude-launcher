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
    (definitions / source.name).write_text(source.read_text("utf-8"), "utf-8")
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
        assert not chart.node("execute").props.get("ask")
        assert chart.node("approval-wait").props["ask"]["otherwise"] == "human"


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


def test_snapshot_mismatch_replans_from_inspect(tmp_path, monkeypatch):
    """checks reports replan when verify is blocked by a moved local SHA;
    the run goes back to inspect and plans again (claunch-2ekz1)."""
    from claude_launcher.cflow2 import layers
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(runtime, "_launch", lambda fn, *args: fn(*args))
    kw = {"cwd": str(tmp_path), "scope": "replan-test"}
    st = runtime.start(layers.load("reposync-merge-push-v2"), caps={},
                       inputs={"assignment": json.dumps({"branch": "master"})}, **kw)
    for _ in range(2):
        assert st["tasks"][0]["label"] == "inspect"
        st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
        assert st["tasks"][0]["label"] == "prepare"
        st = runtime.report(st["tasks"][0]["instance"], "ok",
                            {"plan": "p", "candidate": "c" * 40, "tree": "t" * 40}, **kw)
        assert st["tasks"][0]["label"] == "checks"
        st = runtime.report(st["tasks"][0]["instance"], "replan",
                            {"reason": "assignment local != current local"}, **kw)
    assert st["status"] == "running"


@pytest.mark.parametrize("given,shown", [(None, "rebase"), ("merge", "merge")])
def test_conflict_repair_follows_the_integration_input(tmp_path, monkeypatch, given, shown):
    from claude_launcher.cflow2 import layers
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    kw = {"cwd": str(tmp_path), "scope": "conflict-test"}
    inputs = {"assignment": "{}"}
    if given:
        inputs["integration"] = given
    st = runtime.start(layers.load("reposync-conflict-v2"), caps={}, inputs=inputs, **kw)
    st = runtime.report(st["tasks"][0]["instance"], "ok", {}, **kw)
    repair = st["tasks"][0]
    assert repair["label"] == "repair"
    assert f"{shown}을 따른다" in repair["prompt"]
    assert "--rebase-merges로 재현" in repair["prompt"] and "`git merge`로 재현" in repair["prompt"]


# ── approval window, autonomous retry and sync-stall (claunch-dqgpt) ──

T0 = 1_700_000_000.0
PREPARED = {"plan": "p", "candidate": "c" * 40, "tree": "t" * 40}


class _Push:
    """Drive reposync-merge-push-v2 by hand with a controlled clock."""

    def __init__(self, tmp_path, monkeypatch):
        from claude_launcher.cflow2 import layers
        monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
        monkeypatch.setattr(runtime, "_launch", lambda fn, *args: fn(*args))
        self.kw = {"cwd": str(tmp_path), "scope": "push-test"}
        self.now = T0
        self.st = runtime.start(layers.load("reposync-merge-push-v2"), caps={},
                                inputs={"assignment": json.dumps({"branch": "master"})},
                                now=self.now, **self.kw)

    def labels(self):
        return [t["label"].rsplit("/", 1)[-1] for t in self.st.get("tasks", [])]

    def choices(self):
        return {c["node"].rsplit("/", 1)[-1]: c for c in self.st.get("choices", [])}

    def report(self, label, outcome="ok", value=None):
        hit = [t for t in self.st["tasks"] if t["label"].rsplit("/", 1)[-1] == label]
        assert len(hit) == 1, (label, self.labels())
        self.st = runtime.report(hit[0]["instance"], outcome, value or {}, now=self.now,
                                 verify_runner=lambda *a: (0, "green", False), **self.kw)
        return self.st

    def select(self, node, option, caller="user", reason="r"):
        self.st = runtime.select(node, option, reason, caller=caller, now=self.now, **self.kw)
        return self.st

    def approve(self, decline=False):
        self.st = runtime.approve(None, decline=decline, now=self.now, **self.kw)
        return self.st

    def tick(self, seconds):
        self.now += seconds
        self.st = runtime.wait_tick(self.now, None, **self.kw)
        self.st = runtime.status(now=self.now, **self.kw)
        return self.st

    def plan(self):
        self.report("inspect")
        self.report("prepare", value=PREPARED)
        self.report("checks")
        return self.st

    def reviewed(self):
        """Plan, then answer the reviewer ask as a person."""
        self.plan()
        assert self.asking() == ["review"]
        self.approve()
        assert self.labels() == ["review"]
        return self.report("review")

    def options(self, node):
        return [o["name"] if isinstance(o, dict) else o for o in self.choices()[node]["options"]]

    def asking(self):
        return [a["node"].rsplit("/", 1)[-1] for a in self.st.get("asks", [])]


def test_window_unanswered_auto_executes_after_eligibility(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    gate = d.choices()["auto-execute"]
    assert gate["optional"] is True
    assert d.labels() == []
    d.tick(301)
    assert d.labels() == ["approval-check"]
    d.report("approval-check")
    assert d.labels() == ["execute"] and "auto-execute" not in d.choices()
    d.report("execute")
    assert d.labels() == ["receipt"]
    d.report("receipt")
    assert d.st["status"] == "done"


def test_window_ineligible_falls_to_person_approval(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.tick(301)
    d.report("approval-check", "ineligible", {"reason": "rebase rewrites a pinned SHA"})
    assert d.st["status"] == "waiting_approval" and d.asking() == ["approval-wait"]
    d.approve()
    assert d.labels() == ["approval-wait"]
    d.report("approval-wait")
    assert d.labels() == ["execute"]


def test_window_approve_now_skips_the_timer(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "approve-now")
    assert d.labels() == ["execute"] and d.st["waits"] == []


def test_window_manual_waits_for_a_person_and_sticks_on_retry(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "manual")
    assert d.st["waits"] == [] and d.asking() == ["approval-wait"]
    d.approve()
    d.report("approval-wait")
    d.report("execute", "remote_moved",
             {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "non-fast-forward"})
    d.plan()
    assert "auto-push" not in d.options("retry-route")
    d.select("retry-route", "re-approve", caller="agent")
    d.approve()  # review
    d.report("review")
    # no window on this retry: the run went to a person once, it goes there again
    assert "auto-execute" not in d.choices()
    assert d.asking() == ["approval-wait"]


def test_window_decline_writes_a_receipt(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "decline")
    assert d.labels() == ["receipt"]
    d.report("receipt")
    assert d.st["status"] == "done"


def test_remote_moved_retries_without_a_person(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "approve-now")
    d.report("execute", "remote_moved",
             {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "non-fast-forward"})
    assert d.labels() == ["inspect"]
    d.plan()
    assert set(d.options("retry-route")) >= {"auto-push", "re-approve", "stop"}
    d.select("retry-route", "auto-push", caller="agent", reason="ff only, range-diff equal")
    assert d.labels() == ["execute"]
    d.report("execute")
    assert d.labels() == ["receipt"]


def test_retry_re_approve_goes_through_review_and_window(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "approve-now")
    d.report("execute", "remote_moved",
             {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "rejected"})
    d.plan()
    d.select("retry-route", "re-approve", caller="agent")
    assert d.asking() == ["review"]
    d.approve()
    d.report("review")
    assert "auto-execute" in d.choices()


@pytest.mark.parametrize("via", ["route", "execute"])
def test_stall_waits_for_the_user_then_continue_or_end(tmp_path, monkeypatch, via):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "approve-now")
    if via == "route":
        d.report("execute", "remote_moved",
                 {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "rejected"})
        d.plan()
        d.select("retry-route", "stop", caller="agent", reason="upstream rewritten")
    else:
        d.report("execute", "stall", {"reason": "daemon card expired"})
    assert d.labels() == ["stall-notice"]
    d.report("stall-notice")
    stall = d.choices()["sync-stall"]
    assert not stall.get("optional")
    with pytest.raises(Exception):
        d.select("sync-stall", "continue", caller="agent")
    d.select("sync-stall", "continue", reason="rebase onto the new upstream")
    assert d.labels() == ["inspect"]
    d.plan()
    # after a stall the fast path is closed: the person's instruction changed the plan
    assert "auto-push" not in d.options("retry-route")
    d.select("retry-route", "stop", caller="agent", reason="same again")
    d.report("stall-notice")
    d.select("sync-stall", "end", reason="give up")
    assert d.labels() == ["receipt"]


def test_sync_loop_is_bounded(tmp_path, monkeypatch):
    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    d.select("auto-execute", "approve-now")
    moved = {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "rejected"}
    d.report("execute", "remote_moved", moved)
    for _ in range(3):
        d.plan()
        d.select("retry-route", "auto-push", caller="agent")
        d.report("execute", "remote_moved", moved)
    assert d.st["status"] == "waiting_approval" and d.st["reason"] == "loop_limit"


def test_every_window_and_retry_step_holds_the_merge_pause(tmp_path, monkeypatch):
    """The pause reads node labels off the live state (daemon/reposync_pause.py):
    the approval window and the retry route must stand inside its span."""
    from claude_launcher.daemon import reposync_pause

    def held(d):
        state = RunStore(**d.kw).load_state()
        return reposync_pause.span_node(state)

    d = _Push(tmp_path, monkeypatch)
    d.reviewed()
    assert held(d) == "auto-execute"
    d.tick(301)
    assert held(d) in ("approval-check", "auto-execute")
    d.report("approval-check", "ineligible", {"reason": "x"})
    assert held(d) == "approval-wait"
    d.approve()
    d.report("approval-wait")
    assert held(d) == "execute"
    d.report("execute", "remote_moved",
             {"old_upstream": "a" * 40, "new_upstream": "b" * 40, "reason": "rejected"})
    d.plan()
    assert held(d) == "retry-route"
    d.select("retry-route", "stop", caller="agent")
    assert held(d) is None
