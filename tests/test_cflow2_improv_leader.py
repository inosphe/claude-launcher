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


def test_async_verification_gates_and_merge_interval_are_declared():
    chart = _chart()
    by_label = {n.label: n for n in chart.nodes.values() if n.label}
    assert not [n for n in chart.nodes.values() if n.get("restart")]
    assert "sweep" not in by_label and "reflect" not in by_label
    opts = {o["name"]: o for o in chart.node("standby").get("options")}
    assert opts["integrate"]["interval"] == 300
    assert "handle-regression" in opts
    for label, action in (("admission", "check-batch"), ("checkpoint-check", "check-enqueued")):
        run = by_label[label].get("verify")["run"]
        assert run[4:6] == ["tools/delayed_sweep.py", action]
    assert by_label["checkpoint-check"].get("verify")["run"][-1] == "CFLOW_DATA_MERGED"


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
        iid = next(t["instance"] for t in self.st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)
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


def _allow(cmd, cwd, env, timeout):
    return 0, "queue admission/checkpoint verified"


def _preflight(ld):
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": [{"issue": "x"}], "repo": "/repo"})
    ld.report("admission", verify=_allow)
    assert ld.tasks() == ["integrate"]


def _merged(ld):
    ld.report("integrate", "ok", {"merges": [{"issue": "x", "tip": "a" * 40,
        "merge_sha": "b" * 40}], "master": "b" * 40, "repo": "/repo"})


def test_round_finishes_without_waiting_for_sweep_or_deployment(tmp_path):
    ld = Leader(tmp_path)
    _preflight(ld)
    _merged(ld)
    assert ld.tasks() == ["checkpoint-check"]
    seen = []
    def check(cmd, cwd, env, timeout):
        import json
        seen.append(json.loads(env["CFLOW_DATA_MERGED"]))
        return 0, "two queued checkpoint records"
    ld.report("checkpoint-check", verify=check)
    assert seen[0]["repo"] == "/repo"
    assert ld.tasks() == ["landed-notice"]
    assert not ld.st.get("waits") and not ld.st.get("delegations")
    ld.report("landed-notice")
    assert ld.st["status"] == "done" and ld.st["next_round"]["auto"]
    ld.now += 60
    assert runtime.start_next_round(ld.cwd, "lead", ld.now)["round"] == 2
    ld.status()
    ld.report("intake")
    ld.select("integrate")
    assert ld.st["held"]["option"] == "integrate"
    ld.now = T0 + 300
    ld.tick()
    assert ld.tasks() == ["integrate-preflight"]


def test_missing_checkpoint_holds_the_report_without_running_tests(tmp_path):
    ld = Leader(tmp_path)
    _preflight(ld)
    _merged(ld)
    ld.report("checkpoint-check", verify=lambda *args: (2, "merge has no queued checkpoint"))
    assert ld.tasks() == ["checkpoint-check"]
    assert ld.st["status"] == "running"
    ld.report("checkpoint-check", verify=_allow)
    assert ld.tasks() == ["landed-notice"]


def test_a_pause_between_preflight_and_merge_returns_to_standby(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": [{"issue": "x"}], "repo": "/repo"})
    ld.report("admission", "paused", {"reason": "regression detected"})
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]
    ld.select("handle-regression")
    assert ld.tasks() == ["handle-regression"]
    ld.report("handle-regression")
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


def test_an_empty_preflight_or_dropped_batch_returns_to_standby(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "empty", {"reason": "all candidates paused"})
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]
    ld.now += 300
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": [{"issue": "x"}], "repo": "/repo"})
    ld.report("admission", verify=_allow)
    ld.report("integrate", "empty", {"reason": "head changed"})
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


def test_partial_merge_failure_preserves_checkpoints_before_notice(tmp_path):
    ld = Leader(tmp_path)
    _preflight(ld)
    merges = [{"issue": "x", "tip": "a" * 40, "merge_sha": "b" * 40}]
    ld.report("integrate", "fail", {"reason": "ff-only refused", "merges": merges, "repo": "/repo"})
    assert ld.tasks() == ["batch-failed"]
    ld.report("batch-failed", "ok", {"repo": "/repo", "merges": merges,
              "failed": "integrate", "cause": "ff-only refused", "followups": []})
    assert ld.tasks() == ["checkpoint-check"]
    ld.report("checkpoint-check", verify=_allow)
    assert ld.tasks() == ["landed-notice"]


def test_no_merge_before_failure_returns_to_standby(tmp_path):
    ld = Leader(tmp_path)
    _preflight(ld)
    ld.report("integrate", "fail", {"reason": "git error", "merges": [], "repo": "/repo"})
    ld.report("batch-failed", "nothing_merged", {"reason": "no successful merges"})
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]


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
# claunch-6xg: the checkpoint gate judges the repository the batch was merged into


def _load_sweep():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "sweep.py"
    spec = importlib.util.spec_from_file_location("sweep_6xg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checkpoint_gate_receives_the_merged_repository_not_leader_cwd(tmp_path):
    import json
    ld = Leader(tmp_path)
    _preflight(ld)
    _merged(ld)
    seen = []
    def check(cmd, cwd, env, timeout):
        seen.append((cwd, json.loads(env["CFLOW_DATA_MERGED"])["repo"]))
        return 2, "no queued checkpoint for merged repo"
    ld.report("checkpoint-check", verify=check)
    assert seen == [(str(tmp_path), "/repo")]
    assert ld.tasks() == ["checkpoint-check"]


# claunch-4zc51.1.1: the leader's wrapup said "basetemp는 지우지 않는다 --
# 포렌식 기록" and nothing removed the leader's sweep basetemps afterwards
# (C:/t held s469w, s469w1..w22 and more). Both leader canonicals now have the
# leader remove its own name shapes after moving the four values into the
# final report, the way the worker's wrapup does since claunch-4zc51.1.
_LEADER_OWN_ARTIFACT_LINES = (
    "**이 리더 세션이 직접 만든 테스트 부산물은 이 세션이 전부 지운다**",
    "── 테스트 부산물 정리 (이 리더 세션이 만든 것만, 전부) ──",
    "`ls -d C:/t/${CLAUNCH_SESSION}w C:/t/${CLAUNCH_SESSION}w[0-9]* "
    "C:/t/${CLAUNCH_SESSION}c[0-9]* C:/t/${CLAUNCH_SESSION}-* 2>/dev/null`",
    "접두사만으로 고르지 않는다: `C:/t/s46*`은 s469의 것까지 잡는다.",
    "`git worktree remove --force <경로>`",
    "미리보기 ref(`refs/claunch/preview/*`)는 이 절의 대상이 아니다",
    "워커나 다른 세션의 항목은 그 세션이 이미 종료됐어도 여기서 지우지 않는다",
    # s469's three conditions (msg-81cfb74ff39f)
    "지우는 자리는 교대 종료(이 스텝) 하나다. 회차 도중에는 지우지 않는다",
    "네 값을 최종 보고에 옮긴 뒤에 지운다",
    "아직 도는 sweep grant가 있으면",
    "교대를 넘어 디렉터리를 보존할 이유가 없다",
)


def _leader_wrapup(which):
    import yaml
    from pathlib import Path
    wf = Path(__file__).resolve().parents[1] / "src" / "claude_launcher" / "workflows"
    if which == "v1":
        step = yaml.safe_load((wf / "improv-leader.yaml").read_text(encoding="utf-8"))
        step = step["steps"]["wrapup"]
        return step["instructions"], step["done_when"], None
    node = yaml.safe_load((wf / "v2" / "improv-leader-v2.yaml").read_text(encoding="utf-8"))
    node = node["nodes"]["wrapup"]
    return node["prompt"], node["done_when"], node["effects"]


def _flat(text):
    return " ".join(text.split())


@pytest.mark.parametrize("which", ["v1", "v2"])
def test_leader_wrapup_removes_the_test_artifacts_its_own_session_made(which):
    words, done, effects = _leader_wrapup(which)
    words, done = _flat(words), _flat(done)
    assert "basetemp는 지우지 않는다" not in words
    for line in _LEADER_OWN_ARTIFACT_LINES:
        assert _flat(line) in words, line
    assert "(시작 · 소요 · 워커 수 · 실행량)" in done
    assert "`C:/t/<세션>w`·`C:/t/<세션>w<숫자>…`·" in done
    if effects is not None:
        assert "fs.write" in effects


def test_leader_wrapup_names_cover_the_sweep_basetemp():
    # tools/sweep.py's default command is where `<세션>w` comes from
    sweep = _load_sweep()
    assert '--basetemp="{root}/{session}w"' in sweep.DEFAULT_COMMAND
