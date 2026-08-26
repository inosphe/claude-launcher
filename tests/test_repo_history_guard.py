"""The guard that keeps tree-reuse honest -- and the proof that it is armed.

``tools/sweep.py`` and ``tools/changed_tests.py`` both accept a green receipt
recorded for a *different commit with the same tree*. That is only sound
while no test in this suite can tell two same-tree commits apart, i.e. while
nothing reads this repository's HEAD or its refs.

That premise used to be a paragraph in ``tools/sweep.py``, grepped by hand.
``tests/_repo_history_guard.py`` makes it a machine's job; this file is the
broken-variant check for the machine. The first test IS the dummy violation
the premise's failure mode would look like -- a test reading the real
repository's ``git log`` -- and it passes only because the guard stops it.
Take the autouse fixture out of ``tests/conftest.py`` and this file is the
one that goes red.

The rest pins the line the guard draws, because the interesting part is not
that it refuses things: it is *which* things. Naming an object by its hash is
allowed on purpose, and one real test depends on that
(``tests/test_mergecheck.py::test_the_real_commits``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _repo_history_guard import Guard, RepoHistoryRead, offending

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# the broken variant: a test that really does read this repository's history
# --------------------------------------------------------------------------- #
def test_reading_this_repositorys_log_is_stopped_where_it_happens():
    """The guard is installed in this session, and it fires on a real call.

    This is deliberately a live ``subprocess.run``, not a call to
    :func:`offending`. A pure function that returns the right answer while
    nothing is wired to it is the exact shape of a check that looks present
    and watches nothing, and this repository has been bitten by that before
    (``tools/changed_tests.py``'s rule 3, first version).

    Failing here means the suite is once again free to read HEAD, and every
    green receipt reused across a same-tree pair is a guess.
    """
    with pytest.raises(RepoHistoryRead) as caught:
        subprocess.run(["git", "log", "-1"], cwd=str(ROOT), capture_output=True)

    assert "read" in str(caught.value)
    assert "tests/_repo_history_guard.py" in str(caught.value)


def test_the_fixture_hands_over_the_guard_it_installed(repo_history_guard):
    """The session fixture yields the live Guard, rooted at this checkout.

    It records what it refused, which is how a violation is known to have
    gone *through* the guard rather than round it. The violation is raised
    here rather than borrowed from the test above: under ``-n`` the two do
    not share a process, and under ``-p randomly`` they do not share an
    order either.
    """
    assert repo_history_guard.root == ROOT
    before = len(repo_history_guard.seen)
    with pytest.raises(RepoHistoryRead):
        subprocess.run(["git", "describe"], cwd=str(ROOT), capture_output=True)
    assert any(
        "git describe" in seen for seen in repo_history_guard.seen[before:]
    )


# --------------------------------------------------------------------------- #
# where the line is
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log", "-1"],                       # implicit HEAD
        ["git", "rev-parse", "HEAD"],
        ["git", "rev-parse", "master^{tree}"],      # a branch, not an object
        ["git", "describe", "--tags"],
        ["git", "status", "--porcelain"],
        ["git", "diff", "--name-only", "master...HEAD"],
        ["git", "merge-base", "master", "HEAD"],
        ["git", "for-each-ref"],
        ["git", "show", "HEAD~1"],
    ],
)
def test_history_reads_against_this_repository_are_refused(argv):
    problem = offending(argv, str(ROOT), ROOT)
    assert problem is not None, argv
    assert " ".join(argv) in problem


@pytest.mark.parametrize(
    "argv",
    [
        # object names: the object database is shared by every commit of this
        # repository, so two same-tree commits cannot disagree about these.
        ["git", "cat-file", "-e", "41fcfc8^{commit}"],
        ["git", "merge-base", "41fcfc8", "744e88d"],
        ["git", "diff", "41fcfc8", "744e88d"],
        ["git", "log", "41fcfc8"],
        # questions about the checkout's shape, not its history
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "rev-parse", "--absolute-git-dir"],
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        ["git", "ls-files", "--others", "--exclude-standard"],
        ["git", "diff", "--name-only"],
        # not git at all
        ["python", "-m", "pytest", "tests", "-q"],
    ],
)
def test_reads_that_two_same_tree_commits_agree_about_are_allowed(argv):
    assert offending(argv, str(ROOT), ROOT) is None, argv


def test_the_real_commits_test_is_still_legal():
    """``test_mergecheck.py::test_the_real_commits`` must keep working.

    It is the counter-example to the sentence ``tools/sweep.py`` used to
    carry -- a test that reads the *real* repository on purpose -- and it is
    safe for a reason worth pinning rather than remembering: it names its two
    commits by hash. ``mergecheck.check_pair`` then asks ``git merge-base``
    of those two fixed objects, which is a walk over a subgraph every commit
    of this repository holds identically.

    If a future tightening of the guard makes this red, that test goes red
    with it, and the tightening is wrong rather than that test.
    """
    for argv in (
        ["git", "cat-file", "-e", "41fcfc8^{commit}"],
        ["git", "merge-base", "41fcfc8", "744e88d"],
    ):
        assert offending(argv, str(ROOT), ROOT) is None, argv


def test_a_throwaway_repository_is_not_this_repository(tmp_path):
    """The good case: the same reads, dug under ``tmp_path``, are fine.

    Both spellings the suite uses -- ``-C <repo>`` and ``cwd=<repo>`` -- and
    the guard must recognise each, or the five git-touching test modules go
    red for doing the right thing.
    """
    scratch = tmp_path / "repo"
    scratch.mkdir()
    assert offending(["git", "-C", str(scratch), "log", "-1"], str(ROOT), ROOT) is None
    assert offending(["git", "log", "-1"], str(scratch), ROOT) is None


def test_a_worktree_of_this_repository_counts_as_this_repository(tmp_path):
    """Worktrees live under ``.claude/worktrees/`` and share the history.

    Every worker runs in one, so a guard that only knew the main checkout
    would be off in every session that matters.
    """
    inside = ROOT / ".claude" / "worktrees" / "whatever"
    assert offending(["git", "log", "-1"], str(inside), ROOT) is not None


def test_the_guard_comes_back_off(tmp_path):
    """``uninstall`` restores the real ``Popen``, so the patch is not sticky.

    The session fixture removes it in a ``finally``; without this, a leak
    would only ever show up as somebody else's mysterious failure.
    """
    real = subprocess.Popen
    guard = Guard(tmp_path).install()
    assert subprocess.Popen is not real
    guard.uninstall()
    assert subprocess.Popen is real
