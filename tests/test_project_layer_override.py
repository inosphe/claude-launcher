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

SUITE = 'uv run --extra test pytest tests -q'


@pytest.mark.parametrize(
    "stem, step_id, marker",
    [
        ("improv-worker", "review", ' -m "not worktree"'),
        ("improv-leader", "integrate", ""),
    ],
)
def test_the_override_carries_this_repos_suite_as_its_verify(
    stem, step_id, marker
):
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    assert wf.name == stem
    verify = wf.steps[step_id].verify
    assert verify is not None, f"{stem}:{step_id} lost its verify"
    assert verify.command.startswith(SUITE + marker)
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
