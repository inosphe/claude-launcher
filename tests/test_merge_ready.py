"""The gate that answers what the leader's rejection used to answer.

``tools/merge_ready.py`` exists to take one round trip out of the loop: the
worker files a landing request, the target moves while it waits, and the
leader spends a turn measuring divergence by hand and sending it back. The
measurement is two git commands, so it belongs to a machine.

What these pin is the part that is easy to get wrong -- **which** answer comes
back. The leader's ruling (board ``claunch-mde``) splits the old single
"rebase first" into two, and the split is the whole value:

* a text conflict forces a rebase (``3``)
* a moved baseline does not -- it invalidates the *evidence*, and a
  re-measurement on the merge that will actually happen settles it (``1``,
  cleared by a preview merge)

The measured case behind the second one is from this repository: a branch
adding a rule for every gate under ``tools/``, and a fifth gate landing on
master meanwhile that broke it. Different files, clean ``merge-tree``, red
merged tree (fixed in ``90a07a9``). ``test_a_clean_merge_still_demands_a_re_measurement``
is that case's stand-in, and it is the test that would fail first if somebody
later "simplified" the two verdicts back into one.

The scenarios are branch pairs in one repository, built once, for the reason
``test_mergecheck`` gives: on Windows every git call is a process, and this
suite runs inside a sweep the whole fleet queues for.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "merge_ready.py"


def _load():
    spec = importlib.util.spec_from_file_location("merge_ready_under_test", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge_ready = _load()


# --------------------------------------------------------------------------- #
# one repository, every scenario
# --------------------------------------------------------------------------- #
def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _commit(repo, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _scenario(repo, name: str, *, target_adds: dict, branch_adds: dict) -> None:
    """``<name>-target`` and ``<name>-branch``, forked from a shared root.

    Each scenario gets its own orphan root so they cannot see each other's
    files. ``target_adds`` lands on the target *after* the fork, which is what
    makes the branch behind; empty means the target never moved.
    """
    _git(repo, "checkout", "-q", "--orphan", f"{name}-target")
    _git(repo, "rm", "-rqf", "--ignore-unmatch", ".")
    _write(repo, "shared.py", "def helper():\n    return 1\n")
    _commit(repo, f"{name}: root")
    _git(repo, "checkout", "-q", "-b", f"{name}-branch")
    for path, text in branch_adds.items():
        _write(repo, path, text)
    if branch_adds:
        _commit(repo, f"{name}: branch work")
    _git(repo, "checkout", "-q", f"{name}-target")
    for path, text in target_adds.items():
        _write(repo, path, text)
    if target_adds:
        _commit(repo, f"{name}: target moved")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("merge-ready")
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    # master has to be a real branch, not the unborn one `git init` names:
    # every scenario below starts with an orphan checkout, so without this
    # commit master never gets one and returning to it fails.
    _write(path, "README", "root\n")
    _commit(path, "root")

    # The branch is up to date with its target: nothing to ask for.
    _scenario(path, "aligned", target_adds={}, branch_adds={"mine.py": "x = 1\n"})

    # The target moved, but in another file. git is quiet; the evidence is not.
    _scenario(
        path,
        "clean",
        target_adds={"theirs.py": "y = 2\n"},
        branch_adds={"mine.py": "x = 1\n"},
    )

    # Both sides rewrote the same lines: only a rebase gets past this.
    _scenario(
        path,
        "conflict",
        target_adds={"shared.py": "def helper():\n    return 'theirs'\n"},
        branch_adds={"shared.py": "def helper():\n    return 'mine'\n"},
    )

    # Same shape as "clean" -- used by the preview-merge tests, which need a
    # scenario they can leave refs on without disturbing the others.
    _scenario(
        path,
        "preview",
        target_adds={"theirs.py": "y = 2\n"},
        branch_adds={"mine.py": "x = 1\n"},
    )

    # The branch was merged into the target: the target is ahead of the tip,
    # and that must not read as a moved baseline.
    _scenario(
        path,
        "landed",
        target_adds={},
        branch_adds={"mine.py": "x = 1\n"},
    )
    _git(path, "checkout", "-q", "landed-target")
    _git(path, "merge", "-q", "--no-ff", "-m", "landed: merged", "landed-branch")

    # A distance big enough for --max-behind to have something to exceed.
    _scenario(
        path,
        "far",
        target_adds={"theirs.py": "y = 2\n"},
        branch_adds={"mine.py": "x = 1\n"},
    )
    for n in range(3):
        _write(path, f"more{n}.py", f"z = {n}\n")
        _commit(path, f"far: target moved {n}")

    _git(path, "checkout", "-q", "master")
    return path


def _run(repo, *argv) -> int:
    return merge_ready.main(["--repo", str(repo), *argv])


def _verdict(repo, capsys, *argv):
    code = _run(repo, *argv)
    out = capsys.readouterr()
    return code, (out.out + out.err)


# --------------------------------------------------------------------------- #
# the two ready answers
# --------------------------------------------------------------------------- #
def test_aligned_is_ready(repo, capsys):
    """``behind == 0``: the target is an ancestor, so a --no-ff merge is safe.

    This is the case the leader agreed is an empty demand -- there is nothing
    on the target side left to collide with, and asking for a rebase here only
    changes the tip the reviewer signed off on.
    """
    code, out = _verdict(
        repo, capsys, "--branch", "aligned-branch", "--target", "aligned-target"
    )
    assert code == merge_ready.READY, out
    assert "aligned" in out


def test_a_landed_branch_is_not_a_moved_baseline(repo, capsys):
    """After a --no-ff landing the target IS ahead of the frozen tip.

    The naive "is the target ahead of me" probe flips to stale at the exact
    moment the work succeeded. Under ``awaits`` that flip is a notification,
    so this would wake a worker to rebase onto a target that already contains
    it -- and ``await-landing`` is precisely where the probe runs.
    """
    code, out = _verdict(
        repo, capsys, "--branch", "landed-branch", "--target", "landed-target"
    )
    assert code == merge_ready.READY, out
    assert "landed" in out


# --------------------------------------------------------------------------- #
# the split: conflict vs moved baseline
# --------------------------------------------------------------------------- #
def test_a_conflict_asks_for_a_rebase(repo, capsys):
    code, out = _verdict(
        repo, capsys, "--branch", "conflict-branch", "--target", "conflict-target"
    )
    assert code == merge_ready.REBASE, out
    assert "rebase" in out


def test_a_clean_merge_still_demands_a_re_measurement(repo, capsys):
    """The case ``90a07a9`` was: no text conflict, and the merged tree red.

    The two sides here touch different files, so ``merge-tree`` is clean and a
    conflict-only gate would pass. What is stale is the worker's targeted test
    numbers: they were taken on a base the target has since moved off. This is
    the verdict the leader added, and it is deliberately NOT ``REBASE`` -- the
    branch merges fine, so forcing it to move would cost the reviewed tip for
    nothing.
    """
    code, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "clean-target"
    )
    assert code == merge_ready.REMEASURE, out
    assert "re-measure" in out
    # The cheaper remedy is spelled out where the verdict is read, or the
    # worker rebases by default and the concession is worth nothing.
    assert "merge --no-ff" in out
    assert "update-ref" in out


def test_the_two_failures_do_not_share_an_exit_code(repo, capsys):
    """``awaits.probe`` compares exit codes and ignores output by contract.

    ``cflow/model.py``'s ``Awaits`` says so outright: "The exit code is the
    fact. Output is carried into the signal as evidence and is deliberately
    not part of the comparison." Folded into one code, a branch that went from
    stale to conflicted while it waited would flip nothing and the daemon
    would say nothing -- which is the exact silence this gate exists to break.
    """
    assert merge_ready.REMEASURE != merge_ready.REBASE
    assert merge_ready.CANNOT_TELL not in (merge_ready.REMEASURE, merge_ready.REBASE)


# --------------------------------------------------------------------------- #
# the preview merge: re-measuring without moving the branch
# --------------------------------------------------------------------------- #
def _preview(repo, branch: str, target: str, ref: str) -> str:
    """Build the merge that would actually happen and leave it at ``ref``.

    Exactly the recipe the gate prints, minus the tests -- done with a
    temporary branch rather than a worktree so the test does not depend on
    ``git worktree`` cleanup on Windows.
    """
    _git(repo, "checkout", "-q", "-B", "tmp-preview", target)
    _git(repo, "merge", "-q", "--no-ff", "-m", "preview", branch)
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", ref, head)
    _git(repo, "checkout", "-q", "master")
    _git(repo, "branch", "-q", "-D", "tmp-preview")
    return head


def test_a_preview_merge_of_this_pair_clears_the_re_measurement(repo, capsys):
    ref = "refs/claunch/preview/preview-branch"
    head = _preview(repo, "preview-branch", "preview-target", ref)
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "preview-branch",
        "--target",
        "preview-target",
        "--preview-ref",
        ref,
    )
    assert code == merge_ready.READY, out
    assert "re-measured" in out
    assert head[:12] in out


def test_a_preview_merge_of_the_old_pair_does_not(repo, capsys):
    """The property the design turns on: the preview goes stale by itself.

    The worker re-measures, the target moves again, and the numbers are stale
    again -- with nobody watching. Pinning the preview to the exact pair means
    the verdict returns to ``re-measure`` on its own, which is what makes the
    ``awaits`` probe worth running at all.
    """
    ref = "refs/claunch/preview/preview-branch"
    _preview(repo, "preview-branch", "preview-target", ref)
    _git(repo, "checkout", "-q", "preview-target")
    _write(repo, "later.py", "w = 3\n")
    _commit(repo, "preview: target moved again")
    _git(repo, "checkout", "-q", "master")

    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "preview-branch",
        "--target",
        "preview-target",
        "--preview-ref",
        ref,
    )
    assert code == merge_ready.REMEASURE, out


def test_a_ref_that_is_not_a_merge_of_the_pair_is_ignored(repo, capsys):
    """A gate satisfied by any ref at the right name would be no gate at all."""
    ref = "refs/claunch/preview/decoy"
    tip = _git(repo, "rev-parse", "clean-branch").strip()
    _git(repo, "update-ref", ref, tip)
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "clean-branch",
        "--target",
        "clean-target",
        "--preview-ref",
        ref,
    )
    assert code == merge_ready.REMEASURE, out


# --------------------------------------------------------------------------- #
# distance, when a leader wants it to count
# --------------------------------------------------------------------------- #
def test_distance_alone_is_not_a_rebase_by_default(repo, capsys):
    code, _ = _verdict(
        repo, capsys, "--branch", "far-branch", "--target", "far-target"
    )
    assert code == merge_ready.REMEASURE


def test_max_behind_escalates_a_clean_merge_to_a_rebase(repo, capsys):
    """The leader's threshold, with a number in it.

    ``improv-leader``'s written rule was "뒤처짐이 뚜렷하면 (예: master가 수 개
    이상 커밋 앞섬)" -- a threshold with no number, which is what cannot be
    moved into a machine as written. This is where the number goes when a
    leader wants one; off by default.
    """
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "far-branch",
        "--target",
        "far-target",
        "--max-behind",
        "1",
    )
    assert code == merge_ready.REBASE, out
    assert "max-behind" in out


# --------------------------------------------------------------------------- #
# not being able to tell is its own answer
# --------------------------------------------------------------------------- #
def test_an_unknown_target_cannot_tell(repo, capsys):
    """Never "ready", and never quietly "not yet" either.

    ``landed_check.py`` set this convention and it is worth keeping: a gate
    that reports "cannot tell" as "no" teaches people to append ``|| true``,
    and a gate that reports it as "yes" is the silent green.
    """
    code, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "no-such-branch"
    )
    assert code == merge_ready.CANNOT_TELL, out
    assert "cannot tell" in out


def test_an_unknown_branch_cannot_tell(repo, capsys):
    code, out = _verdict(
        repo, capsys, "--branch", "no-such-branch", "--target", "clean-target"
    )
    assert code == merge_ready.CANNOT_TELL, out


# --------------------------------------------------------------------------- #
# which target the gate asks about
# --------------------------------------------------------------------------- #
def test_the_upstream_names_the_target_when_no_flag_does(repo, capsys):
    """A static ``verify`` string cannot know a nested worker's target.

    A worker under the leader integrates into master; a worker stacked under a
    middle worker integrates into its parent's branch. Git already records
    "the branch mine is based on", so the gate asks that before falling back
    to master -- otherwise the one static command in the workflow file would
    demand every stacked worker rebase onto a branch it does not land in.
    """
    _git(repo, "branch", "-q", "--set-upstream-to", "clean-target", "clean-branch")
    try:
        code, out = _verdict(repo, capsys, "--branch", "clean-branch")
        assert code == merge_ready.REMEASURE, out
        assert "clean-target" in out
        assert "(upstream)" in out
    finally:
        _git(repo, "branch", "-q", "--unset-upstream", "clean-branch")


def test_master_is_the_target_when_nothing_says_otherwise(repo, capsys):
    code, out = _verdict(repo, capsys, "--branch", "clean-branch")
    assert "(default)" in out
    assert merge_ready.DEFAULT_TARGET in out
