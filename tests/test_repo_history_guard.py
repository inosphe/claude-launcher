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
from collections import namedtuple
from pathlib import Path

import pytest

from _repo_history_guard import Guard, RepoHistoryRead, decide, offending

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
        # the shapes mergecheck uses, aimed at a *ref* instead of a hash
        ["git", "show", "master:tests/test_x.py"],
        ["git", "diff", "--unified=0", f"{'0' * 40}...master"],
        ["git", "diff", "--unified=0", "..41fcfc8"],   # empty side means HEAD
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
    base = "0" * 40
    for argv in (
        # what the test itself runs
        ["git", "cat-file", "-e", "41fcfc8^{commit}"],
        # ...and what mergecheck.check_pair runs underneath it, in order
        ["git", "merge-base", "41fcfc8", "744e88d"],
        ["git", "diff", "--unified=0", f"{base}...41fcfc8"],
        ["git", "show", "41fcfc8:tests/test_session_queued_api.py"],
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


# --------------------------------------------------------------------------- #
# the one exemption, and the fact that it is on the caller
# --------------------------------------------------------------------------- #
def test_the_exemption_table_is_short_and_says_why():
    """An exception you can count is not one that is absent because nobody looked.

    The size assertion is the point: a table that grows without anybody
    noticing is how the premise stops being watched while still looking
    watched. If a second entry is genuinely needed, this line is where the
    person adding it has to say so out loud.
    """
    from _repo_history_guard import EXEMPT_CALLERS

    assert len(EXEMPT_CALLERS) == 1, EXEMPT_CALLERS
    module, func, why = EXEMPT_CALLERS[0]
    assert (module, func) == ("claude_launcher/worktree.py", "current_branch")
    assert "claunch-l8lh" in why, "an exemption has to point at its own follow-up"


def test_the_same_command_from_a_test_is_still_refused():
    """The exemption is on the caller, not on the command.

    Without this, adding ``current_branch`` to the table would quietly bless
    ``rev-parse --abbrev-ref HEAD`` everywhere, and the next test that asserts
    on this repository's branch name would sail through.
    """
    with pytest.raises(RepoHistoryRead):
        subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
        )
def test_a_test_that_asks_for_the_branch_itself_is_not_exempt():
    """The exemption's ground, held by machinery rather than by a paragraph.

    ``EXEMPT_CALLERS`` rests on a claim about the six tests the sweep found:
    none of them asks for the branch -- they drive ``cli``/``attach``/``api``
    and the product code reads it to build a label. That claim was first
    established by grep, and a grep result is precisely the kind of evidence
    this file exists to stop trusting: it was a hand-grepped premise going
    stale that put the guard here.

    So the claim is enforced instead. Calling ``current_branch`` on THIS
    repository from a test module is a test asking for the branch, and it is
    refused even though ``current_branch`` is the exempted frame. A test that
    wants a branch name digs a repository under ``tmp_path`` -- which never
    reaches the guard at all, because the target is not this repository. That
    is why ``tests/test_worktree.py`` and ``tests/test_attach.py`` can go on
    naming these helpers freely.
    """
    from claude_launcher import worktree

    with pytest.raises(RepoHistoryRead):
        worktree.current_branch(ROOT)


def test_a_test_that_asks_for_the_branch_itself_is_not_exempt():
    """The exemption's ground, held by machinery rather than by a paragraph.

    ``EXEMPT_CALLERS`` rests on a claim about the six tests the sweep found:
    none of them asks for the branch -- they drive ``cli``/``attach``/``api``
    and the product code reads it to build a label. That claim was first
    established by grep, and a grep result is precisely the kind of evidence
    this file exists to stop trusting: it was a hand-grepped premise going
    stale that put the guard here.

    So the claim is enforced instead. Calling ``current_branch`` on THIS
    repository from a test module is a test asking for the branch, and it is
    refused even though ``current_branch`` is the exempted frame. A test that
    wants a branch name digs a repository under ``tmp_path`` -- which never
    reaches the guard at all, because the target is not this repository. That
    is why ``tests/test_worktree.py`` and ``tests/test_attach.py`` can go on
    naming these helpers freely.
    """
    from claude_launcher import worktree

    with pytest.raises(RepoHistoryRead):
        worktree.current_branch(ROOT)


def test_product_code_reading_the_branch_for_a_label_is_let_through(
    repo_history_guard,
):
    """The six the sweep found: a test DRIVING product code, not asking it.

    ``pane_label`` is the frame those six actually go through
    (``cli.py:497`` -> ``rename_pane(pane_label(...))``), so the chain here is
    the real one: test -> ``pane_label`` -> ``current_branch`` -> ``_git``.
    The frame directly beneath ``current_branch`` is product code, so the
    exemption holds and the label gets built.

    Contrast with the test below, where the test itself is that frame.
    """
    from claude_launcher import worktree

    before = len(repo_history_guard.exempted)
    label = worktree.pane_label("sX", str(ROOT))

    assert len(repo_history_guard.exempted) == before + 1
    _, problem = repo_history_guard.exempted[-1]
    assert "rev-parse --abbrev-ref HEAD" in problem
    assert "sX" in label


def test_a_test_that_asks_for_the_branch_itself_is_not_exempt():
    """One hop is the whole line, and this is the side that must stay red.

    ``current_branch`` is the exempted frame, and it is still refused here --
    because the frame beneath it is this test. That is the difference between
    product code reading its own checkout for a label and a test whose result
    could turn on which branch is out.

    A test that genuinely wants a branch name digs a repository under
    ``tmp_path``; that never reaches the guard at all, since the target is not
    this repository. Which is why ``tests/test_worktree.py`` and
    ``tests/test_attach.py`` go on naming these helpers freely.
    """
    from claude_launcher import worktree

    with pytest.raises(RepoHistoryRead):
        worktree.current_branch(ROOT)


# --------------------------------------------------------------------------- #
# the exemption's rule, as stack shapes
# --------------------------------------------------------------------------- #
_GUARD = "F:/x/tests/_repo_history_guard.py"
_WT = "F:/x/src/claude_launcher/worktree.py"
_CLI = "F:/x/src/claude_launcher/cli.py"

_Frame = namedtuple("_Frame", "filename function")


def _stack(*pairs):
    """A stack as ``decide`` sees it: nearest frame first, plumbing on top."""
    plumbing = [
        _Frame(_GUARD, "exempt"),
        _Frame(_GUARD, "__init__"),
        _Frame(subprocess.__file__, "run"),
    ]
    return plumbing + [_Frame(f, n) for f, n in pairs]


@pytest.mark.parametrize(
    "exempted, name, frames",
    [
        (
            True,
            "the six: a test drives cli, which builds a label",
            _stack(
                (_WT, "_git"),
                (_WT, "current_branch"),
                (_WT, "pane_label"),
                (_CLI, "_cmd_run"),
                ("F:/x/tests/test_cli.py", "test_run"),
            ),
        ),
        (
            True,
            # tests/test_cli.py really does this, and reading its shim as a
            # foreign caller turned three green tests red once already.
            "...with the test's own subprocess.run shim in the middle",
            _stack(
                ("F:/x/tests/test_cli.py", "fake_launch"),
                (_WT, "_git"),
                (_WT, "current_branch"),
                (_WT, "pane_label"),
                (_CLI, "_cmd_run"),
                ("F:/x/tests/test_cli.py", "test_run"),
            ),
        ),
        (
            False,
            # The label embeds the branch, so a test asserting on it depends
            # on which branch is out. Measuring the hop to current_branch's
            # immediate caller let this through, because pane_label lives in
            # the same module.
            "a test calls pane_label itself: the same-module wrapper leak",
            _stack(
                (_WT, "_git"),
                (_WT, "current_branch"),
                (_WT, "pane_label"),
                ("F:/x/tests/test_x.py", "test_label"),
            ),
        ),
        (
            False,
            "a test calls current_branch itself",
            _stack(
                (_WT, "_git"),
                (_WT, "current_branch"),
                ("F:/x/tests/test_x.py", "test_branch"),
            ),
        ),
        (
            False,
            # Helpers are where people actually put this call.
            "a test asks through a tests/ helper",
            _stack(
                (_WT, "_git"),
                (_WT, "current_branch"),
                (_WT, "pane_label"),
                ("F:/x/tests/conftest.py", "helper"),
                ("F:/x/tests/test_x.py", "test_branch"),
            ),
        ),
        (
            False,
            # What (a) is for: the exemption must not spread to whatever else
            # runs git while current_branch happens to be on the stack.
            "a different module's git call, under an exempt frame",
            _stack(
                ("F:/x/src/claude_launcher/other.py", "sneak"),
                (_WT, "current_branch"),
                (_CLI, "_cmd_run"),
                ("F:/x/tests/test_cli.py", "test_run"),
            ),
        ),
    ],
)
def test_the_exemption_reads_the_stack_shape(exempted, name, frames):
    """Every shape the rule has to tell apart, as data.

    A table rather than live calls, because these are *stack shapes* and the
    interesting ones are awkward to produce for real -- the shim case only
    appears when a test has monkeypatched ``subprocess.run``, and two of the
    refusals cannot be staged at all without writing the very test they
    forbid. Each row here is a mistake this rule made before it stopped
    making it.
    """
    assert (decide(frames) is not None) is exempted, name
