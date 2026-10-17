"""The operator-v2 workflow and the watch probe it waits on (claunch-iw5i8.3).

What has to hold:
  - the definition compiles with no check finding but the declared
    ``unbounded-visits`` (a service loop with no round boundary);
  - answering the user is in every task (the inbox fragment and the
    ``inbox-anytime`` rule), so an operator whose watch is off still answers;
  - the run observes only while the watch probe says on (exit 0); with it off
    (exit 1) the run stays in ``watch-gate`` and no ``observe`` task opens;
    after an observe pass it rests on the 180s timer and gates again;
  - a probe that cannot decide (no record) ends in ``probe-failed`` and a
    back-off timer, not in a failed run;
  - ``claunch operators watch`` answers 0/1/2 from the record file, finding
    the operator by ``--id``, then the run's ``operator`` input, then the
    record bound to ``$CLAUNCH_SESSION``.
"""
from __future__ import annotations

import json

import pytest

from claude_launcher import cli
from claude_launcher.cflow2 import check, layers, runtime
from claude_launcher.daemon import operators_v2, paths

T0 = 1_000_000.0
OP = "0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    monkeypatch.delenv("CFLOW_DATA_INPUTS", raising=False)


def _chart():
    return layers.load("operator-v2")


# -- definition ---------------------------------------------------------------
def test_compiles_with_only_the_declared_unbounded_visits():
    assert [f.code for f in check.check(_chart())] == ["unbounded-visits"]


def test_structure_and_the_operator_input():
    chart = _chart()
    labels = {n.label for n in chart.nodes.values() if n.label}
    assert {"greet", "service", "watch-gate", "observe", "rest",
            "probe-failed", "probe-backoff"} <= labels
    assert chart.meta["inputs"]["operator"]["required"] is True
    assert operators_v2.WORKFLOW == chart.name == "operator-v2"


# -- runtime ------------------------------------------------------------------
class Run:
    def __init__(self, tmp_path):
        self.cwd, self.scope = str(tmp_path), "op"
        self.st = runtime.start(_chart(), cwd=self.cwd, scope=self.scope, caps={},
                                inputs={"operator": OP}, now=T0)
        self.probes = []

    def tasks(self):
        return [t["label"] for t in self.st.get("tasks", [])]

    def report(self, label, value, now):
        inst = [t["instance"] for t in self.st["tasks"] if t["label"] == label][0]
        self.st = runtime.report(inst, "ok", value, cwd=self.cwd, scope=self.scope, now=now)

    def tick(self, now, code):
        def runner(probe, cwd):
            self.probes.append(probe)
            return (code, "")
        self.st = runtime.wait_tick(now, runner, cwd=self.cwd, scope=self.scope)


def test_every_task_answers_the_user_first(tmp_path):
    run = Run(tmp_path)
    assert run.tasks() == ["greet"]
    prompt = run.st["tasks"][0]["prompt"]
    assert OP in prompt and "opv2_inbox" in prompt and "opv2_transcript_search" in prompt
    assert {r["id"] for r in run.st["tasks"][0]["rules"]} >= {
        "opv2-channel", "inbox-anytime", "dispatch-on-behalf"}


def test_watch_off_holds_the_gate_and_watch_on_observes(tmp_path):
    run = Run(tmp_path)
    run.report("greet", {"handled": 0}, T0 + 1)
    assert run.tasks() == []
    run.tick(T0 + 40, 1)          # watch off
    run.tick(T0 + 80, 1)
    assert run.tasks() == [] and run.st["status"] == "running"
    assert run.probes and run.probes[0] == ["claunch", "operators", "watch"]
    run.tick(T0 + 120, 0)         # watch on
    assert run.tasks() == ["observe"]
    run.report("observe", {"posted": 1, "asked": 0, "dispatched": 0}, T0 + 130)
    assert run.tasks() == []      # resting on the timer
    seen = len(run.probes)
    run.tick(T0 + 200, 0)
    assert run.tasks() == [] and len(run.probes) == seen  # the timer, not the gate
    run.tick(T0 + 311, 0)         # rest over: back at the gate
    run.tick(T0 + 342, 0)
    assert run.tasks() == ["observe"]


def test_an_undecidable_probe_reports_and_backs_off(tmp_path):
    run = Run(tmp_path)
    run.report("greet", {"handled": 0}, T0 + 1)
    for i in range(5):
        run.tick(T0 + 31 * (i + 1), 2)
    assert run.tasks() == ["probe-failed"] and run.st["status"] == "running"
    run.report("probe-failed", {"reason": "no operator record"}, T0 + 200)
    assert run.tasks() == []
    run.tick(T0 + 801, 0)         # back-off over: the gate again
    run.tick(T0 + 832, 0)
    assert run.tasks() == ["observe"]


# -- claunch operators watch ----------------------------------------------------
def _record(root, op_id=OP, **fields):
    folder = root / "operators"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{op_id}.json").write_text(
        json.dumps({"id": op_id, "name": "Alpha", **fields}), encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "daemon_dir", lambda: tmp_path)
    return tmp_path


def test_watch_command_exit_codes_by_id(home, capsys):
    assert cli.main(["operators", "watch", "--id", OP]) == 2   # no record
    _record(home, watch=True)
    assert cli.main(["operators", "watch", "--id", OP]) == 0
    _record(home, watch=False)
    assert cli.main(["operators", "watch", "--id", OP]) == 1
    assert cli.main(["operators", "watch", "--id", "../x"]) == 2
    assert "off: operator Alpha" in capsys.readouterr().out


def test_watch_command_reads_the_run_input_then_the_session(home, monkeypatch):
    _record(home, watch=False, session="s9")
    _record(home, "fedcba9876543210", watch=True, session="s8")
    monkeypatch.setenv("CLAUNCH_SESSION", "s8")
    assert cli.main(["operators", "watch"]) == 0                # bound to s8
    monkeypatch.setenv("CFLOW_DATA_INPUTS", json.dumps({"operator": OP}))
    assert cli.main(["operators", "watch"]) == 1                # the input wins
    monkeypatch.delenv("CFLOW_DATA_INPUTS")
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    assert cli.main(["operators", "watch"]) == 2                # nobody bound
    monkeypatch.delenv("CLAUNCH_SESSION")
    assert cli.main(["operators", "watch"]) == 2


def test_a_record_without_watch_counts_as_on(home):
    _record(home)
    assert operators_v2.watch_state(home, OP)[0] is True
