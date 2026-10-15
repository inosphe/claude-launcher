"""Coordinator concurrency, completion gates, and board evidence boundaries."""

from __future__ import annotations

import argparse
import subprocess
from types import SimpleNamespace

import pytest

from claude_launcher import cli_cflow2
from claude_launcher.cflow2 import check, coordinator, frontend, layers, runtime
from claude_launcher.cflow2.store import RunStore


def chart():
    root = layers.package_dir()
    return frontend.compile_file(root / "improv-coordinator-v2.yaml",
                                 resolve_include=lambda n: root / f"{n}.yaml")


class Driver:
    def __init__(self, path):
        self.cwd = str(path)
        self.kw = {"cwd": self.cwd, "scope": "coordinator-test", "now": 1000}
        self.st = runtime.start(chart(), caps={}, inputs={"issue": "parent-1"}, **self.kw)
        self.report("align")

    def refresh(self):
        self.st = runtime.status(tick=False, **self.kw)
        return self.st

    def find(self, kind, label, issue=None):
        hits = [x for x in self.refresh().get(kind, []) if x.get("label", "").rsplit("/", 1)[-1] == label
                and (issue is None or issue in x.get("prompt", ""))]
        assert len(hits) == 1, (kind, label, issue, self.st)
        return hits[0]["instance"]

    def report(self, label, issue=None, value=None, outcome="ok", runner=None):
        iid = self.find("tasks", label, issue)
        self.st = runtime.report(iid, outcome, value, verify_runner=runner or
                                 (lambda *_: (0, "test evidence", False)), **self.kw)
        runtime.verify_join(10)
        self.refresh()
        return self.st

    def choose(self, label, option, issue=None, caller="agent"):
        iid = self.find("choices", label, issue)
        self.st = runtime.select(iid, option, "test evidence", caller=caller, **self.kw)
        return self.st

    def fork(self, issue, board=None):
        board = board or self.cwd
        return runtime.fork("jobs", {"board": board, "issue": issue,
                                    "key": f"{board}#{issue}"}, **self.kw)

    def jobs(self):
        return RunStore(self.cwd, self.kw["scope"]).load_state()["collections"]["jobs"]["items"]


@pytest.fixture
def d(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    return Driver(tmp_path)


def test_packaged_chart_checks_and_preserves_worker_default():
    c = chart()
    findings = check.check(c)
    assert not [f for f in findings if f.level == "error"]
    assert {(f.code, f.node) for f in findings if f.level == "warning"} <= {
        ("unbounded-visits", None), ("par-write-overlap", "coordination")}
    assert c.meta["default_child_cflow"] == "improv-worker-v2"
    assert c.meta.get("default_role") is None
    # Shared round data must never leak between dynamic issue members.
    assert not any(n.bind for n in c.nodes.values())


def test_two_issues_keep_owner_and_completion_separate(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
        d.choose("issue-action", "assign", issue)
    d.report("assign", "job-B", {"owner": "worker-B", "evidence": "board B"})
    d.report("assign", "job-A", {"owner": "worker-A", "evidence": "board A"})
    assert {j["issue"]: j["owner"] for j in d.jobs()} == {
        "job-A": "worker-A", "job-B": "worker-B"}
    d.choose("issue-action", "complete", "job-A")
    d.report("review-result", "job-A")
    d.report("closed", "job-A")
    assert {j["issue"]: j["status"] for j in d.jobs()} == {"job-A": "done", "job-B": "active"}
    assert d.find("tasks", "coordinate")
    assert d.find("choices", "issue-action", "job-B")


def test_duplicate_fork_does_not_reset_job(d):
    d.fork("job-A")
    d.report("register", "job-A")
    d.choose("issue-action", "assign", "job-A")
    d.report("assign", "job-A", {"owner": "worker-A", "evidence": "board A"})
    d.fork("job-A")
    assert len(d.jobs()) == 1
    assert d.jobs()[0]["owner"] == "worker-A"
    assert not [t for t in d.refresh()["tasks"] if t["label"] == "register"]


def test_same_issue_id_on_different_boards_is_independent(d, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    d.fork("job-A")
    d.fork("job-A", str(other))
    assert len(d.jobs()) == 2
    assert len({j["key"] for j in d.jobs()}) == 2


def test_direct_work_slot_blocks_second_writer_but_not_other_work(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
    d.choose("issue-action", "direct", "job-A")
    d.report("direct-prepare", "job-A")
    with pytest.raises(runtime.FlowError):
        d.choose("issue-action", "direct", "job-B")
    d.choose("issue-action", "investigate", "job-B")
    d.report("investigate", "job-B")
    d.report("direct-work", "job-A")
    assert d.find("tasks", "assign", "job-A")
    d.choose("issue-action", "direct", "job-B")


def test_failed_direct_work_keeps_slot_until_recovery(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
    d.choose("issue-action", "direct", "job-A")
    d.report("direct-prepare", "job-A")
    d.report("direct-work", "job-A", outcome="fail")
    with pytest.raises(runtime.FlowError):
        d.choose("issue-action", "direct", "job-B")
    d.report("direct-recover", "job-A")
    d.choose("issue-action", "direct", "job-B")


def test_user_gate_and_group_join_hold_shutdown(d):
    d.fork("job-A")
    d.report("register", "job-A")
    d.report("coordinate", value={"stop_request": "user asked to stop"})
    with pytest.raises(runtime.FlowError):
        d.choose("stop-intake", "drain")
    d.choose("stop-intake", "continue", caller="user")
    d.report("coordinate", value={"stop_request": "user asked again"})
    d.choose("stop-intake", "drain", caller="user")
    assert not [t for t in d.refresh()["tasks"] if t["label"] == "summary"]
    with pytest.raises(runtime.FlowError, match="joining"):
        d.fork("job-B")
    d.choose("issue-action", "complete", "job-A")
    d.report("review-result", "job-A")
    d.report("closed", "job-A")
    assert d.find("tasks", "summary")


def test_member_failure_does_not_cancel_other_members(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
        d.choose("issue-action", "investigate", issue)
    d.report("investigate", "job-A", outcome="fail")
    d.report("recover", "job-A")
    d.report("investigate", "job-B")
    assert d.find("tasks", "coordinate")
    assert d.find("choices", "issue-action", "job-B")
    with pytest.raises(coordinator.CheckError, match="not settled"):
        coordinator.check_settled(RunStore(d.cwd, d.kw["scope"]).load_state())


def test_closed_verify_failure_keeps_issue_active(d):
    d.fork("job-A")
    d.report("register", "job-A")
    d.choose("issue-action", "complete", "job-A")
    d.report("review-result", "job-A")
    d.report("closed", "job-A", runner=lambda *_: (1, "issue is open", False))
    assert d.jobs()[0]["status"] == "active"
    assert d.find("tasks", "closed", "job-A")


def test_deferral_requires_user_answer_while_other_member_proceeds(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
    d.choose("issue-action", "defer", "job-A")
    assert d.jobs()[0]["status"] == "active"
    # Asking has only reached framing: reporting a deferred task is impossible.
    assert not [t for t in d.refresh()["tasks"] if t["label"].endswith("deferred")]
    d.choose("issue-action", "investigate", "job-B")
    d.report("investigate", "job-B")
    assert d.find("choices", "issue-action", "job-B")


def brief(problem, choices):
    return {"brief": {"problem": problem, "recommendation": choices[0], "why": "test evidence",
                      "options": [{"name": name, "summary": name, "risk": "test risk",
                                   "outcome": "test outcome", "reversibility": "reversible",
                                   "reverse_how": "test recovery"} for name in choices]}}


def test_parallel_user_deferrals_and_final_confirmation(d):
    for issue in ("job-A", "job-B"):
        d.fork(issue)
        d.report("register", issue)
        d.choose("issue-action", "defer", issue)
        d.report("ask-frame", issue, brief(issue, ["defer", "continue"]))
    asks = d.refresh()["asks"]
    assert len(asks) == 2
    first = next(a for a in asks if a["brief"]["problem"] == "job-A")
    with pytest.raises(runtime.FlowError):
        runtime.approve(first["instance"], decision="defer", caller="agent", **d.kw)
    runtime.approve(first["instance"], decision="defer", caller="user", **d.kw)
    d.report("deferred", "job-A")
    assert {j["issue"]: j["status"] for j in d.jobs()} == {"job-A": "deferred", "job-B": "active"}
    second = d.refresh()["asks"][0]
    runtime.approve(second["instance"], decision="defer", caller="user", **d.kw)
    d.report("deferred", "job-B")
    d.report("coordinate", value={"stop_request": "user requested stop"})
    d.choose("stop-intake", "drain", caller="user")
    d.report("summary")
    d.report("ask-frame", value=brief("end", ["finish", "hold"]))
    runtime.approve(None, decision="hold", caller="user", **d.kw)
    assert d.find("tasks", "wait-finish")
    d.report("wait-finish")
    d.report("ask-frame", value=brief("end again", ["finish", "hold"]))
    runtime.approve(None, decision="finish", caller="user", **d.kw)
    assert d.find("tasks", "final-check")
    d.report("final-check")
    d.report("finish")
    assert d.refresh()["status"] == "done"


def test_board_probe_checks_identity_status_and_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "board_root", lambda path: path)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '[{"id":"job-A","status":"open"}]', "")
    monkeypatch.setattr(coordinator.subprocess, "run", run)
    coordinator.check_issue(str(tmp_path), "job-A", exists=True)
    with pytest.raises(coordinator.CheckError, match="expected closed"):
        coordinator.check_issue(str(tmp_path), "job-A")
    assert calls[0][0] == ["claunch", "beads", "show", "job-A", "--json"]
    assert calls[0][1]["cwd"] == str(tmp_path)
    assert "shell" not in calls[0][1]
    with pytest.raises(coordinator.CheckError, match="requested issue"):
        coordinator.check_issue(str(tmp_path), "job-B")
    with pytest.raises(coordinator.CheckError, match="key must"):
        coordinator.check_issue(str(tmp_path), "job-A", key="alias")
    with pytest.raises(coordinator.CheckError, match="invalid issue"):
        coordinator.check_issue(str(tmp_path), "--help")


@pytest.mark.parametrize("output", ["not json", "[]", "null", "{}"])
def test_unreadable_board_never_passes(tmp_path, monkeypatch, output):
    monkeypatch.setattr(coordinator, "board_root", lambda path: path)
    monkeypatch.setattr(coordinator, "_run", lambda *_: output)
    with pytest.raises(coordinator.CheckError):
        coordinator.check_issue(str(tmp_path), "job-A")


@pytest.mark.parametrize("failure", [FileNotFoundError("claunch"),
                                     subprocess.TimeoutExpired("claunch", 20)])
def test_unavailable_board_never_passes(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(coordinator, "board_root", lambda path: path)
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(coordinator.subprocess, "run", run)
    with pytest.raises(coordinator.CheckError, match="cannot read evidence"):
        coordinator.check_issue(str(tmp_path), "job-A")


def test_missing_or_fallback_board_is_rejected_before_command(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    monkeypatch.setattr(coordinator.cli_beads, "resolve", lambda _: SimpleNamespace(
        db_path=db, root_path=tmp_path))
    with pytest.raises(coordinator.CheckError, match="existing beads"):
        coordinator.read_issue(str(tmp_path), "job-A")
    db.touch()
    monkeypatch.setattr(coordinator.cli_beads, "resolve", lambda _: SimpleNamespace(
        db_path=db, root_path=tmp_path / "other"))
    with pytest.raises(coordinator.CheckError, match="actual board root"):
        coordinator.read_issue(str(tmp_path), "job-A")


def test_direct_probe_checks_repository_branch_and_clean_tree(tmp_path, monkeypatch):
    responses = {"common": str(tmp_path / ".git"), "branch": "worker-feature", "dirty": ""}
    def run(argv, cwd):
        if "--git-common-dir" in argv:
            return responses["common"] if cwd == str(tmp_path) else str(tmp_path / "other/.git")
        if "symbolic-ref" in argv:
            return responses["branch"]
        if "status" in argv:
            return responses["dirty"]
        return "a" * 40
    monkeypatch.setattr(coordinator, "_run", run)
    coordinator.check_direct(str(tmp_path), str(tmp_path))
    responses["branch"] = "master"
    with pytest.raises(coordinator.CheckError, match="feature branch"):
        coordinator.check_direct(str(tmp_path), str(tmp_path))
    coordinator.check_direct(str(tmp_path), str(tmp_path), ready=True)
    responses["dirty"] = " M file.py"
    with pytest.raises(coordinator.CheckError, match="uncommitted"):
        coordinator.check_direct(str(tmp_path), str(tmp_path), ready=True)
    with pytest.raises(coordinator.CheckError, match="repository"):
        coordinator.check_direct(str(tmp_path), str(tmp_path / "other"))


def test_settlement_rechecks_closed_issue_and_direct_slot(monkeypatch):
    state = {"collections": {"jobs": {"items": [{"key": "/repo#job-A", "board": "/repo",
              "issue": "job-A", "status": "done"}]}, "direct": {"items": []}}}
    monkeypatch.setattr(coordinator, "read_issue", lambda *_: {"id": "job-A", "status": "closed"})
    coordinator.check_settled(state)
    monkeypatch.setattr(coordinator, "read_issue", lambda *_: {"id": "job-A", "status": "open"})
    with pytest.raises(coordinator.CheckError, match="expected closed"):
        coordinator.check_settled(state)
    state["collections"]["jobs"]["items"][0]["status"] = "deferred"
    coordinator.check_settled(state)
    state["collections"]["direct"]["items"] = [{"slot": "checkout"}]
    with pytest.raises(coordinator.CheckError, match="checkout"):
        coordinator.check_settled(state)


def test_cli_probe_is_read_only_and_requires_context(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CFLOW2_SCOPE", raising=False)
    parser = argparse.ArgumentParser()
    cli_cflow2.register(parser.add_subparsers())
    args = parser.parse_args(["cflow2", "coordinator-check", "--settled"])
    assert args.func(args) == 1
    assert "verify environment" in capsys.readouterr().err
