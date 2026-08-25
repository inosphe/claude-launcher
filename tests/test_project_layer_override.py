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

from claude_launcher.cflow import model, state as state_mod

OVERRIDES = Path(__file__).resolve().parents[1] / ".claunch" / "workflows"

#: Which step each override arms, and how strictly this file may say so.
#: The worker's is pinned to the letter here because this file and it are
#: edited together; the leader's is checked by prefix, so a change to its
#: workflow does not paint this suite red before it lands.
ARMED = {"improv-worker": "review", "improv-leader": "integrate"}

# Both gates run against a venv that is already there. A worker's worktree
# builds its venv once during the work ('uv sync --extra test'), and that one
# sync also installs the xdist that -n 8 needs; after it, re-syncing inside a
# verify is a side effect that really did block the gate — sync cannot replace
# the claunch.exe a running daemon holds open (os error 5). So both are
# --no-sync, and --extra test goes with the sync it belonged to.
WORKER_SUITE = 'uv run --no-sync pytest tests -q -m "not worktree"'
LEADER_SUITE = 'uv run --no-sync pytest tests -q'


def test_the_worker_override_verifies_without_touching_the_environment():
    """The worker's gate command, spelled out — every word of it load-bearing.

    ``--no-sync`` keeps the verify from re-installing the venv out from under
    a running daemon. ``-n 8`` is the measured ceiling (see
    ``test_the_verify_runs_bounded_parallel``). The basetemp is short (deep
    worktree paths under a long one hit Windows' path limit) and per-session
    (pytest empties its basetemp at startup, so a shared one has concurrent
    workers deleting each other's runs).
    """
    verify = model.load(OVERRIDES / "improv-worker.yaml").steps["review"].verify
    assert verify is not None, "the worker override lost its verify"
    assert verify.command == (
        'uv run --no-sync pytest tests -q -m "not worktree" -n 8 '
        '--basetemp="C:/t/%CLAUNCH_SESSION%"'
    )


@pytest.mark.parametrize(
    "stem, step_id, suite",
    [
        ("improv-worker", "review", WORKER_SUITE),
        ("improv-leader", "integrate", LEADER_SUITE),
    ],
)
def test_the_override_carries_this_repos_suite_as_its_verify(
    stem, step_id, suite
):
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    assert wf.name == stem
    verify = wf.steps[step_id].verify
    assert verify is not None, f"{stem}:{step_id} lost its verify"
    assert verify.command.startswith(suite)
    # basetemp keeps concurrent verifies out of each other's temp trees (and
    # off the default %TEMP%, which has bitten this machine's permissions).
    assert "--basetemp=" in verify.command


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


def test_the_leader_override_is_canonical_plus_verify():
    """Field for field the bundled leader, verify excluded — no other drift.

    The override is a whole copy with a verify grafted on, so it goes stale
    silently the moment the canonical file is edited alone. It did: commit
    6dc8602 added the ``integrate-preflight`` step — the "who else is
    standing in this tree" check, and the rebase screening for drifted
    branches — to the canonical leader only. Every merge in THIS repository
    runs the override, so every merge for the days after it ran with no
    preflight step at all, and nothing was red: the two files simply said
    different things under one name. Comparing them is the only check that
    sees that, because each file on its own is valid.

    The worker pair drifted the same way and is deliberately NOT covered
    here yet — resyncing it would rewrite a workflow other sessions are
    mid-run on, so it is reported rather than fixed in passing.
    """
    from dataclasses import replace

    canonical = model.load(dict(state_mod.bundled_workflows())["improv-leader"])
    override = model.load(OVERRIDES / "improv-leader.yaml")

    assert override.name == canonical.name
    assert override.description == canonical.description
    assert override.start == canonical.start
    assert override.recur == canonical.recur
    assert override.default_role == canonical.default_role
    assert override.filter_roles == canonical.filter_roles
    assert list(override.steps) == list(canonical.steps), (
        "the override gained or lost a step against the canonical leader"
    )
    for step_id, canonical_step in canonical.steps.items():
        assert replace(override.steps[step_id], verify=None) == replace(
            canonical_step, verify=None
        ), (
            f"{step_id!r} differs from the canonical leader by more than its "
            f"verify — the override drifted, and a run here would follow the "
            f"override's version of the rule"
        )


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


# --------------------------------------------------------------------------- #
# The naming rule an agent reads: a worker prefixes its own worktree/branch
# names with its session. The wizard path already does (``default_name``
# falls back to $CLAUNCH_SESSION), so the convention is prompting — it has to
# live where an agent learns it when it names a checkout itself: the intake
# of the worker workflow. A bare name like ``worktree-session-click-cache``
# minted inside a fleet of sessions is the accident these pin.
# --------------------------------------------------------------------------- #
def test_worker_override_intake_insists_on_session_prefixed_names():
    """The override this repo runs tells a worker to prefix its own names."""
    text = model.load(OVERRIDES / "improv-worker.yaml").steps["intake"].instructions
    assert "$CLAUNCH_SESSION" in text    # the prefix source is named
    assert "<세션>-<요지>" in text         # and its shape is spelled out


def test_bundled_worker_workflow_intake_insists_on_session_prefixed_names():
    """A repo with no project override runs the bundled copy — same rule."""
    bundled = dict(state_mod.bundled_workflows())["improv-worker"]
    text = model.load(bundled).steps["intake"].instructions
    assert "$CLAUNCH_SESSION" in text
    assert "<세션>-<요지>" in text
