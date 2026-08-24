"""``tools/gate_bench.py``: the harness that measures whether the gate is
deterministic.

It is a measuring instrument, so its own defects are the expensive kind — a
wrong number here does not fail, it gets believed and quoted. Two of its
defaults have already been wrong in exactly that way, and both are pinned
here: a gate string that silently ran no tests at all, and a record file that
landed in the working tree the workflow requires to be clean.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "gate_bench.py"


def _load():
    # tools/ is not on pythonpath (pyproject sets src/ only), and it is a
    # script rather than a package — load it by path.
    spec = importlib.util.spec_from_file_location("gate_bench", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default(dest):
    for action in _load().build_parser()._actions:
        if action.dest == dest:
            return action.default
    raise AssertionError(f"no --{dest}")


def test_the_gate_string_survives_splitting_with_its_quotes_intact():
    """``-m "not worktree"`` must reach pytest as ONE argument.

    Naive ``str.split()`` hands pytest a ``-m`` of ``'"not'`` and an extra
    path argument ``worktree"``; pytest then collects nothing and exits 5.
    That failure is nasty because it looks like success in the only way most
    people check — zero failures — while having run zero tests.
    """
    mod = _load()
    argv = mod.gate_argv(_default("gate"))

    assert "not worktree" in argv, argv
    assert argv[argv.index("-m") + 1] == "not worktree"
    assert '"not' not in argv  # the naive-split signature
    # and the split the tool must NOT use really does produce it
    assert '"not' in _default("gate").split()


def test_the_default_gate_still_names_the_width_under_test():
    """The harness exists to answer a question about ``-n 8``; a default that
    quietly lost the width would answer a different question under the same
    name."""
    argv = _load().gate_argv(_default("gate"))
    assert argv[argv.index("-n") + 1] == "8"
    assert argv[:3] == ["uv", "run", "--no-sync"]  # never mutates the env


def test_the_record_file_defaults_outside_the_working_tree():
    """The records are machine-local measurement data, not a deliverable.

    A relative default lands them in whatever directory the tool is run from
    — in practice the repo root, untracked and unignored. The workflow whose
    gate this tool measures refuses to wrap up on a dirty tree, so the tool
    would break the procedure it belongs to, one run per use.
    """
    out = Path(_default("out"))

    assert out.is_absolute(), out
    assert REPO not in out.parents and out.parent != REPO, out


def test_burner_aliveness_counts_its_own_processes():
    """The load evidence must be this run's burners, counted directly.

    The old proxy -- a python-proc count against a pre-run baseline -- could
    never certify the load it was started to prove: main() launches the
    burners before the gate, they sit in the baseline, and the delta that
    results tracks whatever other python processes the machine happens to
    run. ``poll()`` returning None is a burner alive, and that is the whole
    instrument.
    """
    import subprocess
    import sys

    live = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()  # read as not-alive without killing anything
    try:
        alive, total = _load()._burners_alive([live, done])
    finally:
        live.kill()
        live.wait()
    assert (alive, total) == (1, 2)


def test_burner_aliveness_with_no_burners_is_absent_not_zero():
    """No burners must read as a missing probe, not as a measurement of zero.

    A quiet-phase record of {"alive": 0, "total": 0} would look like measured
    certainty that no load sat at 0 -- absence dressed as a reading.
    """
    assert _load()._burners_alive(None) == (None, None)
    assert _load()._burners_alive([]) == (None, None)


def test_run_gate_samples_burners_passed_to_it():
    """The sampler must be told which processes are this run's load.

    The instrument only exists to answer questions about runs under a load it
    stands up itself; a sampling point that cannot see that load would answer
    a quieter question under the same name.
    """
    import inspect

    params = inspect.signature(_load().run_gate).parameters
    assert "burners" in params
