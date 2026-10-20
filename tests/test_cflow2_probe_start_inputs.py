"""A wait probe reads the run's start inputs (claunch-b847d).

sweep-job-v2's ``sweep-wait`` probe is an argv of ``$CFLOW_IN_*`` references.
The daemon fills ``python``, ``tool``, ``database`` and ``job`` as start
inputs (``delayed_sweep.watch_body``'s ``cflow_inputs``), and the engine
stores them in ``data.inputs``. A probe got only the run's params as
``CFLOW_IN_<NAME>``, so every reference expanded to an empty string and
Windows refused the argv ``['', '', '--database', '', 'job-state', '--job', '']``
with ``[WinError 87]``; the wait counted three undecided probes and left by
``undecidable`` after about two minutes, nudging the driver for nothing.

The tests run the real chart and the daemon clock's real ``run_probe``;
only the queue tool is a script that records the argv it was given.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_launcher.cflow2 import frontend, runtime
from claude_launcher.daemon import cflow2_clock

T0 = 1_000_000.0
SCOPE = "s9"
WORKFLOW = Path(runtime.__file__).resolve().parents[1] / "workflows" / "v2" / "sweep-job-v2.yaml"

TOOL = """\
import json, pathlib, sys
pathlib.Path(sys.argv[0]).with_name("argv.json").write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"status": "queued", "done": False}))
sys.exit(1)
"""


def _watch(tmp_path):
    """The sweep-job-v2 run the daemon starts for a batch tip, past ``attend``."""
    tool = tmp_path / "delayed_sweep.py"
    tool.write_text(TOOL, encoding="utf-8")
    chart = frontend.compile_text(WORKFLOW.read_text(encoding="utf-8"))
    inputs = {"job": "c758d48e06ce10b3a90cc4ad", "python": sys.executable,
              "tool": str(tool), "database": str(tmp_path / "delayed-sweeps.sqlite3")}
    runtime.start(chart, cwd=str(tmp_path), scope=SCOPE, inputs=inputs, now=T0)
    runtime.report("attend", value={"state": "QUEUED"}, cwd=str(tmp_path), scope=SCOPE,
                   now=T0 + 1)
    return inputs


def _polls(tmp_path, polls):
    """``polls`` clock ticks one ``poll: 60`` apart, run with the daemon's runner."""
    for i in range(1, polls + 1):
        runtime.wait_tick(T0 + 1 + 61 * i, cflow2_clock.run_probe, cwd=str(tmp_path),
                          scope=SCOPE)


def _undecided(tmp_path):
    return [e for e in runtime.journal(cwd=str(tmp_path), scope=SCOPE)
            if e["event"] == "probe_undecided"]


def test_the_sweep_wait_probe_runs_the_queue_tool_with_the_start_inputs(tmp_path):
    inputs = _watch(tmp_path)

    _polls(tmp_path, 3)

    assert _undecided(tmp_path) == []
    assert json.loads((tmp_path / "argv.json").read_text()) == [
        "--database", inputs["database"], "job-state", "--job", inputs["job"]]


def test_a_param_wins_over_a_start_input_of_the_same_name(tmp_path):
    chart = frontend.compile_text(
        "cflow: 2\nname: t\n"
        "inputs:\n  target: {type: string, default: ''}\n"
        "params:\n  target: {type: string, default: ''}\n"
        "nodes:\n  a: {prompt: A}\n"
        "body:\n  seq:\n    - wait: {probe: ['$CFLOW_IN_TARGET'], poll: 10, seconds: 100}\n    - a\n")
    runtime.start(chart, cwd=str(tmp_path), scope=SCOPE, inputs={"target": "from-input"},
                  params={"target": "from-param"}, now=T0)
    seen = []

    def runner(probe, cwd, *args, env=None, **kwargs):
        seen.append((env or {}).get("CFLOW_IN_TARGET"))
        return (1, "")

    runtime.wait_tick(T0 + 11, runner, cwd=str(tmp_path), scope=SCOPE)
    assert seen == ["from-param"]


@pytest.mark.skipif(sys.platform != "win32", reason="WinError 87 is the Windows spelling")
def test_an_all_empty_argv_is_what_windows_refuses():
    code, out = cflow2_clock.run_probe(["", "", "--database", "", "job-state", "--job", ""], ".")
    assert code == 127 and "WinError 87" in out
