"""This repository's own project-layer overrides stay loadable and armed.

The canonical improv pair ships verify-free (a suite command is a property
of one repository), and THIS repository's machine checks live in its
``.claunch/workflows/`` overrides instead. These tests pin that arrangement:
the two override files must parse, and each must carry exactly the one
verify its layer exists to add — the worker's simplified suite on ``review``,
the leader's full sweep on ``integrate``. A later edit that breaks the yaml
or drops a verify would otherwise only be discovered by a run blocking on it.

Both run under ``-n auto``: measured 532s -> 154s, and the suite is parallel
safe (the heavy e2e tests all take an OS-assigned port, and ``conftest``
gives every test its own tmp home). That speed comes with a Windows string
attached, which :func:`test_the_verify_basetemp_leaves_room_for_xdist` pins.
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
    # One worker per core, or the sweep is a 9-minute gate for no reason.
    assert " -n auto" in verify.command


@pytest.mark.parametrize("stem", ["improv-worker", "improv-leader"])
def test_the_override_adds_no_other_verify(stem):
    """The override's whole diff against canonical is its machine checks —
    every other step stays verify-free exactly like the file it shadows."""
    wf = model.load(OVERRIDES / f"{stem}.yaml")
    armed = [s.id for s in wf.steps.values() if s.verify is not None]
    assert armed == (["review"] if stem == "improv-worker" else ["integrate"])


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
