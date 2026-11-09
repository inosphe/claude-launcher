"""A wait probe reads the run's start inputs (claunch-b847d).

sweep-job-v2's ``sweep-wait`` probe is a fixed argv that references the run's
start inputs: the daemon fills ``job`` and ``database`` as ``cflow_inputs``
(``delayed_sweep.watch_body``), and the engine stores them in
``data.inputs``, which the probe reaches as ``$CFLOW_IN_<NAME>``.

The command part of the probe is a literal now (claunch-sr6ex): it used to
start with ``$CFLOW_IN_PYTHON $CFLOW_IN_TOOL``, and an empty value made
``argv[0]`` the empty string, which on Windows failed with ``[WinError 87]``
and everywhere else counted as an undecidable probe (claunch-dt5cr). Only the
two remaining references can be empty, and a literal ``claunch`` is what the
package installs.

The first test runs the real chart and the daemon clock's ``wait_tick`` with a
capturing runner, so the probe's argv and the environment it is given are both
measured; the second measures the ``$CFLOW_IN_*`` expansion itself.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from claude_launcher.cflow2 import frontend, runtime
from claude_launcher.daemon import cflow2_clock

T0 = 1_000_000.0
SCOPE = "s9"
WORKFLOW = Path(runtime.__file__).resolve().parents[1] / "workflows" / "v2" / "sweep-job-v2.yaml"
JOB = "c758d48e06ce10b3a90cc4ad"


def _chart():
    return frontend.compile_text(WORKFLOW.read_text(encoding="utf-8"))


def _probe():
    """The ``sweep-wait`` probe argv as the chart declares it."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    loop = doc["body"]["seq"][1]["loop"]
    return loop["do"]["seq"][0]["wait"]["probe"]


def _watch(tmp_path):
    """The sweep-job-v2 run the daemon starts for a batch tip, past ``attend``."""
    inputs = {"job": JOB, "database": str(tmp_path / "delayed-sweeps.sqlite3")}
    runtime.start(_chart(), cwd=str(tmp_path), scope=SCOPE, inputs=inputs, now=T0)
    runtime.report("attend", value={"state": "QUEUED"}, cwd=str(tmp_path), scope=SCOPE,
                   now=T0 + 1)
    return inputs


def _polls(tmp_path, polls, runner):
    """``polls`` clock ticks one ``poll: 60`` apart, run with ``runner``."""
    for i in range(1, polls + 1):
        runtime.wait_tick(T0 + 1 + 61 * i, runner, cwd=str(tmp_path), scope=SCOPE)


def _undecided(tmp_path):
    return [e for e in runtime.journal(cwd=str(tmp_path), scope=SCOPE)
            if e["event"] == "probe_undecided"]


def test_the_sweep_wait_probe_runs_the_packaged_command_with_the_start_inputs(tmp_path):
    inputs = _watch(tmp_path)
    seen = []

    def runner(probe, cwd, *args, env=None, **kwargs):
        seen.append((list(probe), dict(env or {})))
        return (1, "")  # 1 = still waiting (job-state)

    _polls(tmp_path, 3, runner)

    assert _undecided(tmp_path) == []
    assert len(seen) == 3, "every tick ran the probe"
    probe, env = seen[0]
    assert probe[:2] == ["claunch", "delayed-sweep"]
    assert env["CFLOW_IN_DATABASE"] == inputs["database"]
    assert env["CFLOW_IN_JOB"] == inputs["job"]


def test_the_durable_queue_is_read_with_the_expanded_start_inputs(tmp_path):
    inputs = _watch(tmp_path)
    expanded = runtime.expand_argv(_probe(), {
        "CFLOW_IN_DATABASE": inputs["database"], "CFLOW_IN_JOB": inputs["job"]})
    assert expanded == ["claunch", "delayed-sweep", "job-state", "--job", inputs["job"],
                        "--database", inputs["database"]]


def test_the_probe_command_is_a_literal_so_no_empty_value_can_be_argv0():
    """claunch-dt5cr: ``$CFLOW_IN_PYTHON $CFLOW_IN_TOOL`` opened the argv, and an
    empty value there was exit 127 for every tick. The command is a literal now,
    and only the database and the job id are substituted."""
    probe = _probe()
    assert probe[0] == "claunch" and not probe[0].startswith("$")
    references = [a for a in probe if a.startswith("$CFLOW_IN_")]
    assert set(references) == {"$CFLOW_IN_DATABASE", "$CFLOW_IN_JOB"}


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


def test_run_probe_returns_127_for_a_command_that_does_not_exist(tmp_path):
    """The daemon's clock reports a missing command as 127 (undecidable), not as
    a crash; that is what an empty ``argv[0]`` used to produce on Windows."""
    assert cflow2_clock.run_probe(["no-such-command-claunch-sr6ex"], str(tmp_path))[0] == 127
