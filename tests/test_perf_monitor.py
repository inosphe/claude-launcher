"""``perf-monitor-v2`` and the measurement tool its gates run.

The workflow measures the live daemon once per round, proposes improvements,
and re-measures after the user restarts the daemon on the merged code. What
these pin:

* the workflow compiles with no check findings, and every decision that
  commits work (delegate, next round) is a question to the user;
* ``tools/perf_probe.py`` aggregates a py-spy raw profile the way the prompts
  describe, matches frames across commits without line numbers, and flags a
  before/after pair whose conditions differ (the 2026-10-14 pair ran at load
  1 against load 40 and three minutes after a restart);
* ``check`` refuses a measurement older than ``--max-age``, which is what stops
  a round from passing its measure gate on the previous round's files.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

from claude_launcher.cflow2 import check, layers

TOOL = Path(__file__).resolve().parent.parent / "tools" / "perf_probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("perf_probe_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


perf_probe = _load()


# -- workflow ----------------------------------------------------------------


def _chart():
    return layers.load("perf-monitor-v2")


def test_workflow_compiles_with_no_findings():
    assert [str(f) for f in check.check(_chart())] == []


def test_workflow_delegates_to_improv_worker():
    assert _chart().meta["default_child_cflow"] == "improv-worker-v2"


def test_workflow_has_the_round_structure():
    labels = {n.label for n in _chart().nodes.values() if n.label}
    assert {"rounds", "round-work", "warm-baseline", "measure-baseline",
            "proposal-loop", "proposal", "file-issues", "delegate",
            "landed-wait", "restart-request", "serving-wait", "warm-after",
            "measure-after", "compare-report", "round-end",
            "wrapup"} <= labels


def test_measure_gates_run_the_check_command():
    text = (Path(__file__).resolve().parent.parent / "src" / "claude_launcher"
            / "workflows" / "v2" / "perf-monitor-v2.yaml").read_text(encoding="utf-8")
    assert "perf_probe.py check --label baseline" in text
    assert "perf_probe.py check --label after" in text
    # No restart command is ever the agent's to run (AGENTS.md).
    assert "claunch daemon restart`를 실행" not in text


# -- perf_probe ----------------------------------------------------------------

LOOP = "_run_once (asyncio/base_events.py:1999)"
API = "list_sessions (/x/src/claude_launcher/daemon/api.py:7585)"
PTY = "pump (/x/src/claude_launcher/daemon/pty.py:120)"
LEAF = "read (/usr/lib/python3.12/selectors.py:10)"


def _raw(tmp_path: Path) -> Path:
    raw = tmp_path / "profile.raw"
    raw.write_text("\n".join([
        f"main;{LOOP};{API};{LEAF} 30",
        f"main;{LOOP};{PTY} 10",
        f"worker;{LEAF} 60",
        "garbage line without count",
    ]) + "\n", encoding="utf-8")
    return raw


def test_aggregate_counts_views_and_event_loop(tmp_path):
    s = perf_probe._aggregate(_raw(tmp_path), top=5)
    assert s["total"] == 100
    assert s["event_loop"] == 40
    outer = {r["frame"]: r["samples"] for r in s["outermost"]}
    assert outer == {"list_sessions (daemon/api.py:7585)": 30,
                     "pump (daemon/pty.py:120)": 10}
    leaf = {r["frame"]: r["share"] for r in s["leaf"]}
    assert leaf["read (selectors.py:10)"] == 90.0


def test_noline_matches_a_frame_that_moved():
    assert (perf_probe._noline("f (daemon/api.py:7585)")
            == perf_probe._noline("f (daemon/api.py:7633)"))


def _meta(**over):
    meta = {"seconds": 60, "rate": 100, "load_before": [1.0, 1.0, 1.0],
            "daemon": {"uptime": 3600}, "sessions": {"total": 20},
            "connections": {"open": 5}}
    meta.update(over)
    return meta


def test_same_conditions_have_no_notes():
    assert perf_probe._conditions_differ(_meta(), _meta()) == []


def test_differing_conditions_are_named():
    notes = perf_probe._conditions_differ(
        _meta(), _meta(load_before=[40.3], daemon={"uptime": 180},
                       sessions={"total": 21}, connections={"open": 6}, rate=50))
    text = "\n".join(notes)
    for part in ("rate differs", "load average differs: 1.0 vs 40.3",
                 "after daemon was up 180s", "session rows differ",
                 "open daemon connections differ"):
        assert part in text


def _record(d: Path, age: float) -> None:
    d.mkdir(parents=True)
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)
    (d / "meta.json").write_text(json.dumps({"recorded_at": when.isoformat()}),
                                 encoding="utf-8")
    (d / "profile.raw").write_text("", encoding="utf-8")
    (d / "summary.json").write_text("{}", encoding="utf-8")


def test_check_accepts_a_fresh_measurement(tmp_path):
    _record(tmp_path / "m", age=10)
    assert perf_probe.main(["check", str(tmp_path / "m")]) == 0


def test_check_refuses_a_previous_rounds_measurement(tmp_path):
    _record(tmp_path / "m", age=7200)
    assert perf_probe.main(["check", str(tmp_path / "m"), "--max-age", "3600"]) == 1


def test_check_refuses_a_missing_summary(tmp_path):
    _record(tmp_path / "m", age=10)
    (tmp_path / "m" / "summary.json").unlink()
    assert perf_probe.main(["check", str(tmp_path / "m")]) == 1


def test_check_reads_the_label_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUNCH_SCRATCH", str(tmp_path))
    _record(tmp_path / "m", age=10)
    ptr = tmp_path / "perf-monitor" / "last-baseline"
    ptr.parent.mkdir()
    ptr.write_text(str(tmp_path / "m"), encoding="utf-8")
    assert perf_probe.main(["check", "--label", "baseline"]) == 0
    assert perf_probe.main(["check", "--label", "after"]) == 1


# -- user permission every round ---------------------------------------------

#: The goal's central property: each round, a person decides what happens to
#: the proposal and whether another round runs. These are ask-user calls with
#: fixed choices and no other responder; a change that let the driver or a
#: session answer them would fail here.
USER_QUESTIONS = {
    "proposal": ["delegate", "file-only", "revise", "skip"],
    "round-end": ["next-round", "end"],
    "wait-unclear-choice": ["proceed", "stop"],
}


def test_round_decisions_are_user_questions_with_fixed_choices():
    chart = _chart()
    seen = set()
    for n in chart.nodes.values():
        if not n.get("cases"):
            continue
        assert n.type == "seq", n.id
        call = chart.node(n.get("children")[0])
        assert call.type == "scope" and call.get("proc") == "ask-user", n.id
        w = call.get("with_")
        assert n.label in USER_QUESTIONS, n.id
        assert n.get("cases") == USER_QUESTIONS[n.label], n.label
        assert w["choices"] == {"lit": USER_QUESTIONS[n.label]}, n.label
        assert w["require_reason"] == {"lit": True}, n.label
        assert "from" not in w, f"{n.label}: a user question has no other responder"
        seen.add(n.label)
    assert seen == set(USER_QUESTIONS)


def test_no_choice_is_left_to_the_driver():
    chart = _chart()
    assert [n.label or n.id for n in chart.nodes.values() if n.type == "choose"] == []


# -- wait gates ----------------------------------------------------------------


def _env_issues(monkeypatch, ids):
    monkeypatch.setenv("CFLOW_DATA_DELEGATION", json.dumps({"issues": ids}))


def _rows(monkeypatch, rows):
    monkeypatch.setattr(perf_probe, "_show", lambda issue: rows.get(issue))


def test_issues_closed_reads_ids_from_bound_data(monkeypatch):
    _env_issues(monkeypatch, ["a", "b"])
    _rows(monkeypatch, {"a": {"status": "closed"}, "b": {"status": "closed"}})
    assert perf_probe.main(["issues-closed", "--from-env", "delegation"]) == 0


def test_issues_closed_waits_on_an_open_issue(monkeypatch):
    _env_issues(monkeypatch, ["a", "b"])
    _rows(monkeypatch, {"a": {"status": "closed"}, "b": {"status": "in_progress"}})
    assert perf_probe.main(["issues-closed", "--from-env", "delegation"]) == 1


def test_issues_closed_without_ids_or_row_is_undecidable(monkeypatch):
    monkeypatch.delenv("CFLOW_DATA_DELEGATION", raising=False)
    assert perf_probe.main(["issues-closed", "--from-env", "delegation"]) == 2
    _env_issues(monkeypatch, ["a"])
    _rows(monkeypatch, {})
    assert perf_probe.main(["issues-closed", "--from-env", "delegation"]) == 2


class _Proc:
    def __init__(self, rc):
        self.returncode = rc


def _serving(monkeypatch, rows, rc_by_hash, head="feedbeef"):
    _rows(monkeypatch, rows)
    monkeypatch.setattr(perf_probe, "_daemon_doc", lambda: {"code": {"head": head}})
    monkeypatch.setattr(perf_probe.subprocess, "run",
                        lambda cmd, **kw: _Proc(rc_by_hash[cmd[-2]]))


def test_serving_when_every_merge_is_in_the_daemon_head(monkeypatch):
    _env_issues(monkeypatch, ["a"])
    _serving(monkeypatch, {"a": {"close_reason": "merged abc1234 (sweep 1500)"}},
             {"abc1234": 0})
    assert perf_probe.main(["serving", "--from-env", "delegation"]) == 0


def test_serving_waits_for_a_merge_the_daemon_does_not_run(monkeypatch):
    _env_issues(monkeypatch, ["a", "b"])
    _serving(monkeypatch, {"a": {"close_reason": "merged abc1234"},
                           "b": {"close_reason": "merged def5678"}},
             {"abc1234": 0, "def5678": 1})
    assert perf_probe.main(["serving", "--from-env", "delegation"]) == 1


def test_serving_without_a_merge_hash_is_undecidable(monkeypatch):
    _env_issues(monkeypatch, ["a"])
    _serving(monkeypatch, {"a": {"close_reason": "done"}}, {})
    assert perf_probe.main(["serving", "--from-env", "delegation"]) == 2


def test_serving_with_an_unknown_hash_is_undecidable(monkeypatch):
    # git merge-base exits 128 for an object this checkout does not have.
    _env_issues(monkeypatch, ["a"])
    _serving(monkeypatch, {"a": {"close_reason": "merged abc1234"}}, {"abc1234": 128})
    assert perf_probe.main(["serving", "--from-env", "delegation"]) == 2


def test_serving_without_ids_is_undecidable(monkeypatch):
    monkeypatch.delenv("CFLOW_DATA_DELEGATION", raising=False)
    _serving(monkeypatch, {}, {})
    assert perf_probe.main(["serving", "--from-env", "delegation"]) == 2


def _uptime(monkeypatch, seconds):
    monkeypatch.setattr(perf_probe, "_daemon_doc", lambda: {"pid": 1})
    monkeypatch.setattr(perf_probe, "_uptime", lambda doc: seconds)


def test_warm_reads_the_floor_from_run_inputs(monkeypatch):
    monkeypatch.setenv("CFLOW_DATA_INPUTS", json.dumps({"warmup_seconds": 600}))
    _uptime(monkeypatch, 600)
    assert perf_probe.main(["warm"]) == 0
    _uptime(monkeypatch, 599)
    assert perf_probe.main(["warm"]) == 1


def test_warm_defaults_to_900_and_unreadable_uptime_is_undecidable(monkeypatch):
    monkeypatch.delenv("CFLOW_DATA_INPUTS", raising=False)
    _uptime(monkeypatch, 899)
    assert perf_probe.main(["warm"]) == 1
    _uptime(monkeypatch, 900)
    assert perf_probe.main(["warm"]) == 0
    _uptime(monkeypatch, None)
    assert perf_probe.main(["warm"]) == 2
