"""This repository's own project-layer overrides stay loadable and armed.

The canonical improv pair ships verify-free (a suite command is a property
of one repository), and THIS repository's machine checks live in its
``.claunch/workflows/`` overrides instead. These tests pin that arrangement:
the two override files must parse, and each must carry exactly the one
verify its layer exists to add — the worker's simplified suite on ``review``,
the leader's full sweep on ``integrate``. A later edit that breaks the yaml
or drops a verify would otherwise only be discovered by a run blocking on it.
"""

from __future__ import annotations

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
