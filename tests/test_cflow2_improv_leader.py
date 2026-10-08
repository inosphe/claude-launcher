"""improv-leader-v2: the v2 port of improv-leader.yaml (claunch-yq80p.6).

Compile and check the package definition, then walk a round through the
runtime with stub probes, verify and restart: the integrate window, the
sweep verify, the daemon-run restart, the ``deployed`` checks, the retry
after an expired gate, the landed notice, recur auto into round 2, and the
wind-down that ends the loop."""

from __future__ import annotations

import pytest

from claude_launcher.cflow2 import check, executors, layers, runtime

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    executors.register(None)
    yield
    executors.register(None)


def _chart():
    return layers.load("improv-leader-v2")


def test_compiles_with_no_check_findings():
    findings = check.check(_chart())
    assert [str(f) for f in findings] == []


def test_meta_and_collection():
    chart = _chart()
    meta = chart.meta
    assert meta["recur"] == {"auto": True, "min_gap": 60.0}
    assert meta["filter_roles"] == {"type": "whitelist", "roles": ["leader"]}
    assert meta["default_role"] is None  # v1 improv-leader keeps the default
    assert meta["default_child_cflow"] == "improv-worker-v2"
    landing = meta["collections"]["landing"]
    assert landing["env"] == {"TARGET": "master"}
    # a remote worker's request arrives by mesh message: the leader pushes it
    assert "driver" in landing["access"]["push"]
    assert chart.returns == {"wound_down": {}}


def test_restart_interval_and_checks_are_declared():
    chart = _chart()
    by_label = {n.label: n for n in chart.nodes.values() if n.label}
    restart = by_label["reflect"].get("restart")
    assert restart["linux"] == "bash tools/restart_live.sh"
    assert restart["windows"].endswith("tools/restart_live.ps1")
    assert restart["timeout"] == 900
    standby = chart.node("standby")
    opts = {o["name"]: o for o in standby.get("options")}
    assert opts["integrate"]["interval"] == 300 and "interval" not in opts["wind-down"]
    wait = chart.node("deployed-wait")
    assert [c["id"] for c in wait.get("checks")] == ["deployed"]
    assert wait.get("seconds") == 600 and wait.get("poll") == 30
    assert by_label["sweep"].executor == ["subagent", "inline"]
    run = by_label["sweep"].get("verify")["run"]
    # the merged repository, never the working directory (claunch-6xg)
    assert run[4:6] == ["tools/sweep.py", "check"]
    assert run[-2:] == ["--merged-env", "CFLOW_DATA_MERGED"] and "--repo" not in run


class Leader:
    def __init__(self, tmp_path):
        self.cwd = str(tmp_path)
        self.now = T0
        self.st = runtime.start(_chart(), cwd=self.cwd, scope="lead", caps={}, now=T0,
                                driver="lead")

    def kw(self):
        return {"cwd": self.cwd, "scope": "lead", "now": self.now}

    def status(self):
        self.st = runtime.status(**self.kw())
        return self.st

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks") or []]

    def report(self, label, outcome="ok", value=None, verify=None):
        iid = next(t["instance"] for t in self.st["tasks"] if t["label"] == label)
        self.st = runtime.report(iid, outcome, value, verify_runner=verify, **self.kw())
        if verify is not None:
            runtime.verify_join(10)
            self.status()
        return self.st

    def select(self, option, reason="r"):
        self.st = runtime.select(None, option, reason, **self.kw())
        return self.st

    def tick(self, runner=None):
        self.st = runtime.wait_tick(self.now, runner, cwd=self.cwd, scope="lead")
        return self.st

    def restart(self, code=0):
        acts = runtime.claim_restarts(platform="Linux", boot="b", **self.kw())
        assert [a["kind"] for a in acts] == ["run"]
        runtime.complete_restart(acts[0]["instance"], acts[0]["token"], code, "out",
                                 **self.kw())
        return self.status()


def _deploy(code):
    def run(cmd, cwd):
        assert "deploy_check.py" in cmd
        return (code, f"deploy_check exit {code}")
    return run


def test_a_round_end_to_end(tmp_path):
    ld = Leader(tmp_path)
    assert ld.st["round"] == 1 and ld.tasks() == ["intake"]
    ld.report("intake")
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]

    # nothing ready: the preflight finds an empty batch and goes back to standby
    ld.select("integrate", "a candidate")
    ld.report("integrate-preflight", "empty", {"reason": "evidence incomplete"})
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]

    # the window opened at T0: a second take inside 300s is held
    ld.now = T0 + 60
    ld.select("integrate", "x-1 ready")
    assert ld.st["held"]["option"] == "integrate" and ld.tasks() == []
    ld.now = T0 + 300
    ld.tick()
    assert ld.tasks() == ["integrate-preflight"]

    ld.report("integrate-preflight", "ok", {"batch": ["x-1"], "repo": "/repo"})
    ld.report("integrate", "ok", {"merges": [["x-1", "a" * 40, "b" * 40]], "master": "b" * 40,
                                  "repo": "/repo"})
    assert ld.tasks() == ["sweep"]
    ran = []
    ld.report("sweep", "ok", {"tip": "b" * 40, "passed": 10, "failed": 0, "receipt": "r"},
              verify=lambda cmd, cwd, env, timeout: ran.append(cmd) or (0, "green"))
    assert ran and ran[0][4:6] == ["tools/sweep.py", "check"]
    assert ld.tasks() == ["reflect"]

    # the report waits for the daemon's restart
    with pytest.raises(runtime.FlowError, match="restart is pending"):
        ld.report("reflect")
    ld.restart(0)
    ld.report("reflect")
    assert ld.st["waits"][0]["node"] == "deployed-wait"

    # the gate does not open in 600s: the decision point; retry re-enters reflect
    ld.now += 1
    ld.tick(_deploy(1))
    ld.now += 600
    ld.tick()
    assert ld.tasks() == ["reflect-pregate"]
    ld.report("reflect-pregate")
    ld.select("retry", "exit 1: restart was refused; asked again")
    assert ld.tasks() == ["reflect"]
    assert ld.st["tasks"][0]["restart"]["status"] == "pending"  # a new visit
    ld.restart(0)
    ld.report("reflect")
    ld.now += 1
    ld.tick(_deploy(0))
    assert ld.tasks() == ["landed-notice"]
    ld.report("landed-notice")
    assert ld.st["status"] == "done" and ld.st["next_round"]["auto"] is True

    # recur auto: the daemon starts round 2 after min_gap; the window record carries over
    assert runtime.start_next_round(ld.cwd, "lead", ld.now) is None
    ld.now += 60
    started = runtime.start_next_round(ld.cwd, "lead", ld.now)
    assert started["round"] == 2
    ld.status()
    assert ld.st["round"] == 2 and ld.tasks() == ["intake"]
    state = runtime.RunStore(ld.cwd, "lead").load_state()
    assert list(state["windows"].values()) == [T0 + 300]
    ld.report("intake")
    ld.select("integrate", "y")  # 300s have passed since the last take
    assert ld.tasks() == ["integrate-preflight"]


def _to_sweep(ld):
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": ["x"], "repo": "/repo"})


def _deploy_ok(ld):
    ld.restart(0)
    ld.report("reflect")
    ld.now += 1
    ld.tick(_deploy(0))


def test_a_red_sweep_keeps_the_round_and_still_reflects(tmp_path):
    """X1: a sweep fail after the merge does not end the round failed; the
    follow-up is requested and the merged items go on to reflect and notice."""
    ld = Leader(tmp_path)
    _to_sweep(ld)
    ld.report("integrate", "ok", {"merges": [["x", "a" * 40, "b" * 40]], "master": "b" * 40,
                                  "repo": "/repo"})
    ld.report("sweep", "fail", {"reason": "2 failed: tests/test_x.py"})
    assert ld.st["status"] == "running" and ld.tasks() == ["batch-failed"]
    ld.report("batch-failed", "ok", {"failed": "sweep", "cause": "test_x after x",
                                     "followups": ["s9: x-2"]})
    assert ld.tasks() == ["reflect"]
    _deploy_ok(ld)
    ld.report("landed-notice")
    assert ld.st["status"] == "done" and ld.st["outcome"] == "ok"
    assert ld.st["next_round"]["round"] == 2


def test_an_integrate_fail_with_nothing_merged_returns_to_standby(tmp_path):
    ld = Leader(tmp_path)
    _to_sweep(ld)
    ld.report("integrate", "fail", {"reason": "ff-only refused", "merges": []})
    assert ld.tasks() == ["batch-failed"]
    ld.report("batch-failed", "nothing_merged", {"reason": "no merge before the stop"})
    assert ld.st["status"] == "running"
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


def test_an_integrate_fail_after_merges_reflects_them(tmp_path):
    ld = Leader(tmp_path)
    _to_sweep(ld)
    ld.report("integrate", "fail", {"reason": "ff-only refused", "merges": [["x", "a", "b"]]})
    ld.report("batch-failed", "ok", {"failed": "integrate", "cause": "local master ahead",
                                     "followups": []})
    assert ld.tasks() == ["reflect"]


def test_all_candidates_dropped_at_merge_time_skip_sweep(tmp_path):
    """Y11: integrate empty goes back to standby without a sweep or a restart."""
    ld = Leader(tmp_path)
    _to_sweep(ld)
    ld.report("integrate", "empty", {"reason": "both conflicted"})
    assert ld.st["status"] == "running" and ld.tasks() == []
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


def test_exhausted_reflect_retries_close_the_round(tmp_path):
    """Y12: five failed deploy attempts close the round without deploy (ok,
    so recur continues) instead of failing the run."""
    ld = Leader(tmp_path)
    _to_sweep(ld)
    ld.report("integrate", "ok", {"merges": [], "master": "m", "repo": "/repo"})
    ld.report("sweep", "ok", {"tip": "m", "passed": 1, "failed": 0, "receipt": "r"},
              verify=lambda *a: (0, ""))
    for attempt in range(5):
        assert ld.tasks() == ["reflect"], attempt
        ld.restart(1)
        ld.report("reflect")
        ld.now += 1
        ld.tick(_deploy(1))
        ld.now += 600
        ld.tick()
        assert ld.tasks() == ["reflect-pregate"], attempt
        ld.report("reflect-pregate")
        ld.select("retry", f"exit 1, attempt {attempt}")
    assert ld.st["status"] == "running" and ld.tasks() == ["reflect-closed"]
    ld.report("reflect-closed")
    ld.report("landed-notice")
    assert ld.st["status"] == "done" and ld.st["next_round"]["round"] == 2


def test_close_ends_the_round_without_deploy(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": ["x"], "repo": "/repo"})
    ld.report("integrate", "ok", {"merges": [], "master": "m", "repo": "/repo"})
    ld.report("sweep", "ok", {"tip": "m", "passed": 1, "failed": 0, "receipt": "r"},
              verify=lambda *a: (0, ""))
    ld.restart(1)
    ld.report("reflect")
    ld.now += 1
    for _ in range(3):  # exit 3 (dirty checkout) is undecided
        ld.tick(_deploy(3))
        ld.now += 30
    assert ld.tasks() == ["reflect-pregate"]
    ld.report("reflect-pregate")
    ld.select("close", "exit 3: another session's files; asked it to commit")
    assert ld.tasks() == ["reflect-closed"]
    ld.report("reflect-closed")
    ld.report("landed-notice")
    assert ld.st["status"] == "done" and ld.st["next_round"]["round"] == 2


WIND_DOWN = {
    "problem": "리더 교대를 지금 끝낼지 정한다",
    "options": [
        {"name": "end-shift", "summary": "유휴 워커를 종료하고 최종 보고", "risk": "남은 요청이 대기",
         "outcome": "wound_down", "reversibility": "costly", "reverse_how": "새 리더를 띄운다"},
        {"name": "keep", "summary": "관제 대기로 돌아간다", "risk": "없음",
         "outcome": "교대 계속", "reversibility": "reversible"},
    ],
    "recommendation": "end-shift",
    "why": "착지 큐가 비었다",
}


def _wind_down(ld, answer, reason="r"):
    ld.select("wind-down", "user said stop")
    assert ld.tasks() == ["ask-frame"]
    ld.report("ask-frame", value={"brief": WIND_DOWN})
    (a,) = ld.status()["asks"]
    assert a["resolution"] == "human" and a["format"] == "decision"
    runtime.approve(None, decision=answer, reason=reason, **ld.kw())
    return ld.status()


def test_wind_down_asks_the_person_and_ends_the_loop(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    # keep: back to standby
    _wind_down(ld, "keep", "not yet")
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]
    # the person may answer without a reason (docs 2.11)
    ld.select("wind-down", "again")
    ld.report("ask-frame", value={"brief": WIND_DOWN})
    runtime.approve(None, decision="end-shift", reason=None, **ld.kw())
    ld.status()
    assert ld.tasks() == ["wrapup"]
    ld.report("wrapup")
    assert ld.st["status"] == "done" and ld.st["outcome"] == "wound_down"
    assert "next_round" not in ld.st


def test_a_third_reframe_returns_to_standby(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    _wind_down(ld, "reframe", "why 0")
    for n in (1, 2):
        ld.report("ask-reframe", value={"brief": WIND_DOWN})
        runtime.approve(None, decision="reframe", reason=f"why {n}", **ld.kw())
        ld.status()
    assert ld.st["status"] == "running"
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


def test_no_user_choose_is_left():
    from claude_launcher.cflow2 import ir
    chart = _chart()
    assert not [n.label or n.id for n in chart.nodes.values()
                if n.type == ir.T_CHOOSE and n.get("chooser") == ir.CHOOSER_USER]
    assert not [n.label for n in chart.nodes.values()
                if n.label == "wrapup" and n.get("ask")]


def test_pr_candidates_follow_the_measured_merge_procedure():
    by_label = {n.label: n for n in _chart().nodes.values() if n.label}
    for label in ("integrate-preflight", "integrate"):
        prompt = by_label[label].get("prompt")
        for needle in ("headRefOid", "UNKNOWN", "gh pr diff", "gh pr review",
                       "--match-head-commit <tip>", "commit_check.py", "--ff-only",
                       "다시 조회한다",
                       # Y7 caps, Y9 same-account review, Y10 ff-only refusal
                       "세 번 조회해도 UNKNOWN", "두 번까지", "gh api user", "--comment",
                       "PR-REVIEW", "ff-only가 거절되면", "baseRefName이 master가"):
            assert needle in prompt, (label, needle)
    assert "git.push" in by_label["integrate"].get("effects")


def test_the_leader_registers_a_remote_workers_pr_request(tmp_path):
    ld = Leader(tmp_path)
    item = {"issue": "x-9", "branch": "origin/s9-x", "tip": "c" * 40, "requested_by": "s9",
            "medium": "pr", "pr_url": "https://github.example/o/r/pull/7", "pr_number": 7,
            "base": "master"}
    runtime.collection_op("push", "landing", cwd=ld.cwd, scope="lead",
                          principal=runtime.PR_DRIVER, session="lead", item=item)
    view = runtime.collection_view(ld.cwd, "lead")
    rows = view["landing"]["items"] if "items" in view.get("landing", {}) else view
    assert "x-9" in str(rows) and "pull/7" in str(rows)


# ---------------------------------------------------------------------------
# claunch-6xg: the sweep gate judges the repository the batch was merged into


def _load_sweep():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "sweep.py"
    spec = importlib.util.spec_from_file_location("sweep_6xg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit_repo(path):
    import subprocess
    for args in (["init", "-q"], ["config", "user.name", "t"],
                 ["config", "user.email", "t@t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "first"], ["branch", "-M", "master"]):
        subprocess.run(["git", *args], cwd=path, check=True)


def test_the_sweep_gate_judges_the_merged_repository(tmp_path, repo_template, monkeypatch):
    """The leader's run sits in one repository and the batch is merged into
    another (run-945d034a). The integrate step's `repo` is bound to
    data.merged, the verify reads it through CFLOW_DATA_MERGED, and the gate
    asks for THAT repository's receipt: red without it, green with it. The
    leader's own checkout holds a green receipt the whole time -- a fallback
    to the working directory would turn the first answer green."""
    import json
    import os
    import sys
    from unittest import mock

    sweep = _load_sweep()
    for key in (sweep.test_window.WINDOW_GRANT_ENV, sweep.test_window.WINDOW_CLASS_ENV,
                sweep.test_window.WINDOW_WORKERS_ENV):
        monkeypatch.delenv(key, raising=False)
    home = repo_template("leader-6xg", _commit_repo, tmp_path / "leader")
    merged = repo_template("leader-6xg", _commit_repo, tmp_path / "merged")
    (merged / "b.txt").write_text("two\n", encoding="utf-8")  # a different tip
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=merged, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "batch"], cwd=merged, check=True)
    receipts = tmp_path / "receipts"
    green = f'"{sys.executable}" -c "print(\'3 passed in 0.1s\')"'
    assert sweep.main(["run", "--repo", str(home), "--receipts", str(receipts),
                       "--command", green]) == 0

    tip = subprocess.run(["git", "rev-parse", "master"], cwd=merged, check=True,
                         capture_output=True, text=True).stdout.strip()
    ld = Leader(home)
    _to_sweep(ld)
    ld.report("integrate", "ok", {"merges": [["x", "a" * 40, tip]], "master": tip,
                                  "repo": str(merged)})
    assert ld.tasks() == ["sweep"]
    seen = []

    def gate(cmd, cwd, env, timeout):
        # the packaged argv after `uv run --no-sync python tools/sweep.py`
        assert cmd[:5] == ["uv", "run", "--no-sync", "python", "tools/sweep.py"]
        seen.append((cwd, json.loads(env["CFLOW_DATA_MERGED"])["repo"]))
        with mock.patch.dict(os.environ, env, clear=True):
            code = sweep.main([*cmd[5:], "--receipts", str(receipts)])
        return (code, f"sweep check exit {code}")

    value = {"tip": "b" * 40, "passed": 3, "failed": 0, "receipt": "r"}
    ld.report("sweep", "ok", value, verify=gate)
    assert seen == [(str(home), str(merged))]
    assert ld.tasks() == ["sweep"]  # red: no receipt for the merged repository's tip
    assert sweep.main(["run", "--repo", str(merged), "--receipts", str(receipts),
                       "--command", green]) == 0
    ld.report("sweep", "ok", value, verify=gate)
    assert ld.tasks() == ["reflect"]  # green: the merged repository's receipt
