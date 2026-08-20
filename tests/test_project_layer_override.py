"""This repository's own project-layer overrides stay loadable and armed.

The canonical improv pair ships verify-free (a suite command is a property
of one repository), and THIS repository's machine checks live in its
``.claunch/workflows/`` overrides instead. These tests pin that arrangement:
the two override files must parse, and each must carry exactly the one
verify its layer exists to add — the worker's simplified suite on ``review``,
the leader's full sweep on ``integrate``. A later edit that breaks the yaml
or drops a verify would otherwise only be discovered by a run blocking on it.

Both run under a *bounded* ``-n``: the suite is parallel safe (the heavy
e2e tests all take an OS-assigned port, and ``conftest`` gives every test
its own tmp home), but its cost is spawning processes rather than burning
CPU, so workers past a handful buy nothing —
:func:`test_the_verify_runs_bounded_parallel` keeps the width honest, and
:func:`test_the_verify_basetemp_leaves_room_for_xdist` keeps the paths it
builds under Windows' limit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claude_launcher.cflow import model

OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"

#: Which step each override arms, and how strictly this file may say so.
#: The worker's is pinned to the letter here because this file and it are
#: edited together. The leader's rules are only checked for existence: that
#: file is owned by the branch reworking the leader workflow, and asserting
#: its final form from here would paint this suite red until that lands —
#: the tightening belongs in the change that makes it true.
ARMED = {"improv-worker": "review", "improv-leader": "integrate"}


def test_the_worker_override_verifies_without_touching_the_environment():
    """The worker's gate command, spelled out — every word of it load-bearing.

    ``--no-sync`` because a verify that re-installs the venv is a verify with
    a side effect, and that side effect really did block the gate: sync
    cannot replace the ``claunch.exe`` a running daemon holds open. The
    basetemp is short (deep worktree paths under a long one hit Windows'
    path limit) and per-session (pytest empties its basetemp at startup, so
    a shared one has concurrent workers deleting each other's runs).
    """
    verify = model.load(OVERRIDES / "improv-worker.yaml").steps["review"].verify
    assert verify is not None, "the worker override lost its verify"
    assert verify.command == (
        'uv run --no-sync pytest tests -q -m "not worktree" '
        '--basetemp="C:/t/%CLAUNCH_SESSION%"'
    )


def test_each_override_still_arms_its_step(stem="improv-leader"):
    """The leader half, held to the one thing that is this file's business:
    the override exists to add a machine check, so it must have one."""
    verify = model.load(OVERRIDES / f"{stem}.yaml").steps[ARMED[stem]].verify
    assert verify is not None, f"{stem} lost its verify"
    assert "pytest" in verify.command and "--basetemp=" in verify.command


@pytest.mark.parametrize("stem", sorted(ARMED))
def test_the_override_adds_no_other_verify(stem):
    """The override's whole diff against canonical is its machine checks —
    every other step stays verify-free exactly like the file it shadows."""
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    armed = [s.id for s in wf.steps.values() if s.verify is not None]
    assert armed == [ARMED[stem]]


#: Windows refuses a path this long; the run that broke measured exactly 260.
MAX_PATH = 260

#: What the longest path under basetemp costs *besides* basetemp — xdist's
#: ``popen-gwN/``, the test directory, the transcript layout and the
#: conversation id. Measured against the path that actually raised.
PATH_CONSTANT = 162

#: The longest session name to budget for (``%CLAUNCH_SESSION%`` expands to
#: one; ``w-brief-be`` is the longest this mesh has used).
LONGEST_SESSION = "w" * 16


@pytest.mark.parametrize("stem", ["improv-worker", "improv-leader"])
def test_the_verify_basetemp_leaves_room_for_xdist(stem):
    """A short basetemp is a correctness requirement here, not tidiness.

    ``-n auto`` inserts ``popen-gwN/`` under basetemp, and the transcript
    tests re-encode their whole cwd into one filename (that is how claude
    files a conversation) — so every character of basetemp is spent twice
    and the path grows as ``2 * len(basetemp) + PATH_CONSTANT``. Measured on
    this suite: a 48-character basetemp lands on 258 and passes, 49 lands on
    260 and raises ``FileNotFoundError``. A gate that flips on one character
    is not a gate, so keep the room explicit.
    """
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    verify = wf.steps["review" if stem == "improv-worker" else "integrate"].verify
    basetemp = verify.command.split('--basetemp="')[1].split('"')[0]
    # cmd leaves %CLAUNCH_SESSION% standing when the daemon — which cannot
    # see it — runs the verify, and that literal is longer than most session
    # names, so budgeting for the longer of the two covers both writers.
    expanded = basetemp.replace("%CLAUNCH_SESSION%", LONGEST_SESSION)
    longest = 2 * max(len(expanded), len(basetemp)) + PATH_CONSTANT
    assert longest < MAX_PATH, (
        f"{stem}'s basetemp {basetemp!r} builds paths up to {longest} "
        f"characters, over Windows' {MAX_PATH}: xdist's popen-gwN/ and the "
        f"transcript slug under it spend every character of it twice"
    )


#: Measured on this suite (32 cores): serial 532s, ``-n 4`` 230s, ``-n 8``
#: 178s, ``-n auto`` (= 32 here) 184s. Past a handful of workers the curve is
#: flat, because the wall clock belongs to daemons and PTYs starting up, not
#: to arithmetic.
MAX_USEFUL_WORKERS = 8


@pytest.mark.parametrize("stem", ["improv-worker", "improv-leader"])
def test_the_verify_runs_bounded_parallel(stem):
    """Parallel, but with a ceiling — and the ceiling is the point.

    ``-n auto`` reads as the obvious choice and is the wrong one here. It
    measured no faster than ``-n 8`` while running four times the processes,
    and that contention starved the PTY-timing tests: one sweep in three
    failed ``test_delivery_holds_while_a_human_is_typing``, whose 20-second
    wait for a screen to render is generous until 32 workers are spawning
    daemons at once. A gate that fails one run in three teaches people to
    re-run it, which is worse than a slow gate. The ceiling also has to hold
    when *six* sessions verify at once, which is the normal state of this
    mesh: 6x8 fits this machine, 6x32 does not.
    """
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    verify = wf.steps["review" if stem == "improv-worker" else "integrate"].verify
    width = re.search(r" -n (\S+)", verify.command)
    assert width, f"{stem}'s verify lost its -n; the gate is serial again"
    assert width.group(1) != "auto", (
        "-n auto is one worker per core (32 here): no faster than -n 8 and "
        "flaky with it — see this test's docstring"
    )
    assert 2 <= int(width.group(1)) <= MAX_USEFUL_WORKERS
