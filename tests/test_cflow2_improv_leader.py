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
    assert meta["recur"] == {"auto": True}
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
    assert "sweep.py check" in by_label["sweep"].get("verify")["run"]


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

    ld.report("integrate-preflight", "ok", {"batch": ["x-1"]})
    ld.report("integrate", "ok", {"merges": [["x-1", "a" * 40, "b" * 40]], "master": "b" * 40})
    assert ld.tasks() == ["sweep"]
    ran = []
    ld.report("sweep", "ok", {"tip": "b" * 40, "passed": 10, "failed": 0, "receipt": "r"},
              verify=lambda cmd, cwd, env, timeout: ran.append(cmd) or (0, "green"))
    assert ran and "sweep.py check" in ran[0]
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

    # recur auto: the daemon starts round 2; the window record carries over
    started = runtime.start_next_round(ld.cwd, "lead", ld.now)
    assert started["round"] == 2
    ld.status()
    assert ld.st["round"] == 2 and ld.tasks() == ["intake"]
    state = runtime.RunStore(ld.cwd, "lead").load_state()
    assert list(state["windows"].values()) == [T0 + 300]
    ld.report("intake")
    ld.select("integrate", "y")  # 300s have passed since the last take
    assert ld.tasks() == ["integrate-preflight"]


def test_close_ends_the_round_without_deploy(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    ld.select("integrate")
    ld.report("integrate-preflight", "ok", {"batch": ["x"]})
    ld.report("integrate", "ok", {"merges": [], "master": "m"})
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


def test_wind_down_needs_a_person_and_ends_the_loop(tmp_path):
    ld = Leader(tmp_path)
    ld.report("intake")
    ld.select("wind-down", "user said stop")
    assert ld.st["asks"] and ld.st["asks"][0]["resolution"] in ("routing", "human")
    # declined: back to standby
    from claude_launcher.cflow2 import asks
    asks.human_approve(None, decline=True, reason="not yet", cwd=ld.cwd, scope="lead")
    ld.status()
    assert [c["label"] for c in ld.st["choices"]] == ["standby"]
    ld.select("wind-down", "now")
    asks.human_approve(None, reason="ok", cwd=ld.cwd, scope="lead")
    ld.status()
    ld.report("wrapup")
    assert ld.st["status"] == "done" and ld.st["outcome"] == "wound_down"
    assert "next_round" not in ld.st


def test_pr_candidates_follow_the_measured_merge_procedure():
    by_label = {n.label: n for n in _chart().nodes.values() if n.label}
    for label in ("integrate-preflight", "integrate"):
        prompt = by_label[label].get("prompt")
        for needle in ("headRefOid", "UNKNOWN", "gh pr diff", "gh pr review",
                       "--match-head-commit <tip>", "commit_check.py", "--ff-only",
                       "다시 조회한다"):
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
