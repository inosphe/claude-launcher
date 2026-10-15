"""workflows/v2/sim-run.yaml driven through the v2 runtime with scripted
reports: the coverage reinforcement ends after two passes and goes on to
the report instead of waiting on a loop limit (claunch-efnbt.6,
claunch-4avda CALL-CONTRACT-3), and ok carries assessment with its grounds."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from claude_launcher.cflow2 import frontend, runtime

T0 = 1_000_000.0
CALLER = textwrap.dedent("""
    cflow: 2
    name: t
    returns:
      ok: {assessment: str, adequacy: str}
    body:
      - call: sim-run
        with:
          question: a locked door cannot be opened
          sources: [door.md]
      - return: ok
        value:
          assessment: "$result['sim-report'].value.assessment"
          adequacy: "$result['sim-report'].value.adequacy"
""")


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


class Drv:
    def __init__(self, tmp_path):
        self.kw = {"cwd": str(tmp_path), "scope": "t", "now": T0}
        self.st = runtime.start(frontend.compile_text(CALLER, source="flow.yaml"), params=None, caps={}, **self.kw)

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def report(self, label, outcome="ok", value=None):
        iid = next(t["instance"] for t in self.st["tasks"] if t["label"] == label)
        self.st = runtime.report(iid, outcome, value, **self.kw)


def cov(met):
    return {"run_seq": 1, "model_hash": "h", "coverage": {"met": met}}


REPORT = {"assessment": "inconclusive", "findings": "clear", "adequacy": "inadequate",
          "open": {}, "exhaustive": None, "controls": None, "limits": ["전수 탐색 미지원"]}


def start(d):
    d.report("sim-scope", value={"sim_id": "sim-abc123", "limits": []})
    d.report("sim-build", value={"model_hash": "h"})


def test_coverage_met_goes_straight_to_the_report(tmp_path):
    d = Drv(tmp_path)
    start(d)
    d.report("sim-walk", value=cov(True))
    assert d.tasks() == ["sim-report"]


def test_two_reinforcements_then_the_report_without_a_wait(tmp_path):
    d = Drv(tmp_path)
    start(d)
    for _ in range(2):
        d.report("sim-walk", value=cov(False))
        assert d.tasks() == ["sim-reinforce"]
        d.report("sim-reinforce", value={})
    d.report("sim-walk", value=cov(False))
    # Still short after two passes: on to the report, not a loop_limit approval.
    assert d.tasks() == ["sim-report"], d.st.get("status")
    d.report("sim-report", value=REPORT)
    assert d.st["status"] == "done", d.st.get("status")
    assert d.st["outcome"] == "ok"
    assert d.st["value"] == {"assessment": "inconclusive", "adequacy": "inadequate"}


def test_a_failed_walk_is_the_procedures_fail(tmp_path):
    d = Drv(tmp_path)
    start(d)
    d.report("sim-walk", "fail", {"reason": "runner_failed", "partial": {}})
    assert not d.tasks() and d.st["outcome"] == "fail"


# -- a dynamic call (claunch-z17c9): a task with calls: [sim-run] ----------------

DYN_CALLER = textwrap.dedent("""
    cflow: 2
    name: review
    nodes:
      check:
        prompt: C
        calls: [sim-run]
    body:
      - check
""")
SIM_RUN = Path(frontend.__file__).resolve().parent.parent / "workflows" / "v2" / "sim-run.yaml"


class Dyn:
    """The caller run ``w1``; sim-run is resolved by name from the project
    layer (a copy of the package file, so no other layer decides)."""

    def __init__(self, tmp_path):
        self.cwd = tmp_path / "proj"
        v2 = self.cwd / ".claunch" / "workflows" / "v2"
        v2.mkdir(parents=True)
        (v2 / "sim-run.yaml").write_text(SIM_RUN.read_text(encoding="utf-8"), encoding="utf-8")
        self.kw = {"cwd": str(self.cwd), "now": T0}
        runtime.start(frontend.compile_text(DYN_CALLER, source="flow.yaml"), scope="w1", caps={},
                      driver="w1", **self.kw)
        out = runtime.call("sim-run", "check", {"question": "q", "purpose": "design"}, scope="w1", **self.kw)
        self.sub = out["called"]["scope"]

    def status(self, scope):
        return runtime.status(scope=scope, tick=False, **self.kw)

    def report(self, label, outcome="ok", value=None):
        iid = next(t["instance"] for t in self.status(self.sub)["tasks"] if t["label"] == label)
        runtime.report(iid, outcome, value, scope=self.sub, **self.kw)

    def result(self):
        return next(t for t in self.status("w1")["tasks"] if t["label"] == "check")["call_result"]


def test_a_dynamic_call_gets_ok_and_its_value_back(tmp_path):
    d = Dyn(tmp_path)
    # The purpose input reaches the step that sets settings.purpose.
    scope_task = next(t for t in d.status(d.sub)["tasks"] if t["label"] == "sim-scope")
    assert "settings={purpose: design}" in scope_task["prompt"]
    d.report("sim-scope", value={"sim_id": "sim-abc123", "limits": []})
    d.report("sim-build", value={"model_hash": "h"})
    d.report("sim-walk", value=cov(True))
    d.report("sim-report", value={**REPORT, "assessment": "consistent", "adequacy": "adequate"})
    res = d.result()
    assert res["procedure"] == "sim-run" and res["outcome"] == "ok"
    assert res["value"]["assessment"] == "consistent" and res["value"]["sim_id"] == "sim-abc123"
    assert res["value"]["coverage"] == {"met": True} and res["value"]["exhaustive"] is None


@pytest.mark.parametrize("reason", ["no-profile", "profile-unusable", "no-runtime", "sims_denied"])
def test_a_refused_start_comes_back_as_fail_with_its_reason(tmp_path, reason):
    """sim_start answered 409 (a fault of efnbt.8 or a sims: deny step of
    efnbt.9): sim-walk reports fail, and the caller task gets that reason
    and the partial back."""
    d = Dyn(tmp_path)
    d.report("sim-scope", value={"sim_id": "sim-abc123", "limits": []})
    d.report("sim-build", value={"model_hash": "h"})
    partial = {"sim_id": "sim-abc123", "run_seq": None, "error": "refused"}
    d.report("sim-walk", "fail", {"reason": reason, "partial": partial})
    assert d.result()["outcome"] == "fail"
    assert d.result()["value"] == {"reason": reason, "partial": partial}
