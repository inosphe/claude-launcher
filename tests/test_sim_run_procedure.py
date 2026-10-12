"""workflows/v2/sim-run.yaml driven through the v2 runtime with scripted
reports: the coverage reinforcement ends after two passes and goes on to
the report instead of waiting on a loop limit (claunch-efnbt.6,
claunch-4avda CALL-CONTRACT-3), and ok carries assessment with its grounds."""
from __future__ import annotations

import textwrap

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
