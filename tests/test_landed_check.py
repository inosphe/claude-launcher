"""The landed gate: did the parent actually merge this worker's branch?

``improv-worker`` used to end a round at "requested". ``integration-request``
froze the branch, filed the request, and routed straight to ``wrapup`` --
which tells the session to kill itself in the same turn it files its report.
The run reached ``done`` and the session ceased to exist while the branch was
still sitting in somebody else's queue, so a request that was rejected, sent
back for a rebase, or quietly dropped from a batch had nobody left to notice.
The ``landed`` step is the gate that closes it, and ``tools/landed_check.py``
is what the gate runs.

The interesting half is *which question it asks*. "Does any branch contain my
tip" is the obvious one and it is wrong: a child branch stacked on the tip
contains it while integrating nothing, and in a nested formation children
stacked on a parent are the normal shape rather than an edge case. That false
green was measured on a live mesh before this test existed, which is why
:func:`test_a_child_stacked_on_the_tip_is_not_a_landing` is the case this file
exists for. The contract everywhere in the workflow is a ``--no-ff`` merge, so
the exact question is whether a *merge commit* on another branch lists this tip
among its parents.

Exit codes are the interface (0 landed, 1 not yet, 2 cannot tell), and the
"cannot tell" cases matter for the same reason they do in
``test_deploy_check``: a gate that reports a missing branch as "not landed"
teaches people to pass it with a trailing "or true".
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parents[1] / "tools" / "landed_check.py"


def _load():
    """``tools/`` is not a package -- load the script the way a script is."""
    spec = importlib.util.spec_from_file_location("landed_check", CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


landed_check = _load()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)


def _base(tmp_path_factory, label: str) -> Path:
    """A repo on ``master`` with one commit and a ``feature`` branch on top."""
    repo = tmp_path_factory.mktemp(label)
    _git(repo, "init", "-b", "master")
    _commit(repo, "base")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "work")
    return repo


def _run(repo: Path, *extra: str) -> int:
    return landed_check.main(["--repo", str(repo), *extra])


# --------------------------------------------------------------------------- #
# not yet
# --------------------------------------------------------------------------- #
def test_a_frozen_branch_nobody_merged_is_not_landed(tmp_path_factory):
    repo = _base(tmp_path_factory, "unlanded")
    assert _run(repo) == 1
    assert _run(repo, "--target", "master") == 1


def test_a_child_stacked_on_the_tip_is_not_a_landing(tmp_path_factory, capsys):
    """The false green this gate exists to avoid.

    A worker under a nested worker gets children of its own stacked on its
    branch. Those children *contain* its tip -- they were cut from it -- while
    nothing has been integrated anywhere. Measured on a live mesh: a worker
    froze a tip with nothing landed, and a containment test answered "landed"
    because its own child branch sat on top of it.
    """
    repo = _base(tmp_path_factory, "stacked")
    _git(repo, "checkout", "-b", "child")
    _commit(repo, "child-work")
    _git(repo, "checkout", "feature")

    # `child` contains feature's tip ...
    tip = _git(repo, "rev-parse", "HEAD").strip()
    assert "child" in _git(repo, "branch", "--contains", tip)
    # ... and that is still not a landing.
    assert _run(repo) == 1
    out = capsys.readouterr().out
    assert "not yet" in out
    assert "child" in out, "the descendant that made it look landed is named"


# --------------------------------------------------------------------------- #
# landed
# --------------------------------------------------------------------------- #
def test_a_no_ff_merge_into_the_target_is_a_landing(tmp_path_factory):
    repo = _base(tmp_path_factory, "landed")
    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(repo, "checkout", "feature")
    assert _run(repo) == 0
    assert _run(repo, "--target", "master") == 0


def test_a_landing_is_seen_past_a_child_that_also_contains_the_tip(
    tmp_path_factory,
):
    """Both shapes at once: the real merge is found, the descendant ignored."""
    repo = _base(tmp_path_factory, "both")
    _git(repo, "checkout", "-b", "child")
    _commit(repo, "child-work")
    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(repo, "checkout", "feature")
    assert _run(repo) == 0


def test_a_nested_worker_measures_against_its_parents_branch(tmp_path_factory):
    """A stacked worker's target is the parent branch, never master."""
    repo = _base(tmp_path_factory, "nested")
    _git(repo, "checkout", "-b", "parent-area")
    _commit(repo, "area")
    _git(repo, "checkout", "-b", "leaf")
    _commit(repo, "leaf-work")
    # The parent takes the leaf in; master knows nothing about any of it.
    _git(repo, "checkout", "parent-area")
    _git(repo, "merge", "--no-ff", "leaf", "-m", "merge leaf")
    _git(repo, "checkout", "leaf")
    assert _run(repo, "--target", "parent-area") == 0
    assert _run(repo, "--target", "master") == 1


# --------------------------------------------------------------------------- #
# cannot tell -- distinct from "not yet" on purpose
# --------------------------------------------------------------------------- #
def test_an_unknown_target_cannot_tell_rather_than_denying(tmp_path_factory):
    repo = _base(tmp_path_factory, "notarget")
    assert _run(repo, "--target", "no-such-branch") == landed_check.CANNOT_TELL


def test_a_directory_that_is_not_a_repository_cannot_tell(tmp_path):
    assert _run(tmp_path) == landed_check.CANNOT_TELL


# --------------------------------------------------------------------------- #
# the way the verify actually calls it
# --------------------------------------------------------------------------- #
def test_the_exit_status_reaches_a_shell(tmp_path_factory):
    """The ``verify:`` line runs this as a script, so the status must escape."""
    repo = _base(tmp_path_factory, "asscript")
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 1, proc.stderr

    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo), "--target", "master"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
