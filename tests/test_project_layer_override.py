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

# The two run in different places, so they take uv differently. A worker runs
# in a freshly made worktree, where the first verify legitimately has to build
# the venv — it keeps the syncing form. The leader's gate runs in the live
# checkout, where uv re-syncs after any pyproject change and cannot replace
# ``.venv/Scripts/claunch.exe`` while the daemon and its sessions are running
# it (os error 5); a gate's verify has no business mutating the environment
# anyway, so it is ``--no-sync`` against the venv that is already there.
WORKER_SUITE = 'uv run --extra test pytest tests -q -m "not worktree"'
LEADER_SUITE = 'uv run --no-sync pytest tests -q'


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


@pytest.mark.parametrize("stem", ["improv-worker", "improv-leader"])
def test_the_override_adds_no_other_verify(stem):
    """The override's whole diff against canonical is its machine checks —
    every other step stays verify-free exactly like the file it shadows."""
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    armed = [s.id for s in wf.steps.values() if s.verify is not None]
    assert armed == (["review"] if stem == "improv-worker" else ["integrate"])
