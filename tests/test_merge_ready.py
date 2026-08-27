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

    # The tree the merge would run in. Three files with three fates: the
    # merge rewrites `written.py`, creates `made.py`, and never touches
    # `untouched.py` -- which is what makes the negative controls below
    # possible at all.
    _git(path, "checkout", "-q", "--orphan", "tree-target")
    _git(path, "rm", "-rqf", "--ignore-unmatch", ".")
    _write(path, "written.py", "x = 1\n")
    _write(path, "untouched.py", "y = 1\n")
    _commit(path, "tree: root")
    # Behind the target, clean merge: this one gets the "re-measure" verdict,
    # and it is there so the tree check can be watched on a branch the gate
    # is NOT passing. It has to fork from the same root as the target -- two
    # orphan histories have no merge base, and the tree check would answer
    # "cannot tell" for a reason that has nothing to do with what is dirty.
    _git(path, "checkout", "-q", "-b", "tree-branch")
    _write(path, "written.py", "x = 2\n")
    _write(path, "made.py", "z = 1\n")
    _commit(path, "tree: branch work")
    _git(path, "checkout", "-q", "tree-target")
    _write(path, "theirs.py", "t = 1\n")
    _commit(path, "tree: target moved")
    # Forked from the same root and colliding with the target's move: this
    # one gets the "rebase" verdict, the other not-ready code the leader
    # reads. Both sides add `theirs.py` with different contents.
    _git(path, "branch", "-q", "tree-conflict", "tree-branch")
    _git(path, "checkout", "-q", "tree-conflict")
    _write(path, "theirs.py", "t = 'mine'\n")
    _commit(path, "tree: conflicting work")
    _git(path, "checkout", "-q", "tree-target")

    # Ready on every check above the tree one: this is the branch the old
    # gate answered 0 for while the merge could not start.
    _git(path, "checkout", "-q", "-b", "tree-aligned")
    _write(path, "written.py", "x = 2\n")
    _write(path, "made.py", "z = 1\n")
    _commit(path, "tree: aligned work")

    _git(path, "checkout", "-q", "master")

    # A second checkout, sitting on the target -- the shape this repository is
    # actually in: one tree with master checked out, many sessions sharing it.
    _git(path, "worktree", "add", "-q", str(path.parent / "tree-checkout"), "tree-target")
    return path


@pytest.fixture
def checkout_tree(repo):
    """The worktree on ``tree-target``, handed back clean after each test."""
    where = repo.parent / "tree-checkout"
    yield where
    _git(where, "checkout", "-q", "--", ".")
    _git(where, "clean", "-qfd")


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


# --------------------------------------------------------------------------- #
# whose branch the gate asks about
# --------------------------------------------------------------------------- #
# The same premise ``landed_check`` carried, failing in the opposite
# direction. Both gates read ``--repo`` default ``.`` -- the directory the
# cflow run was keyed to -- as "my branch". When the run was keyed at the
# repository root while the worker stood in a worktree, ``landed_check``
# answered exit 1 and stopped the round; this one answered exit 0 about a
# branch it had never read, and so stayed quiet. Measured at the root of a
# live repository, by two different routes to the same wrong pass:
#
#     ready: nothing to land -- HEAD is master (3cf8ced92a10)
#     ready: aligned -- target origin/master (upstream) +0 / branch +660
#
# The second is why the check compares branches rather than names: with the
# upstream set, the target arrives spelled ``origin/master`` and an equality
# test against ``master`` does not fire.
from claude_launcher.cflow import checkout  # noqa: E402


def _root_and_worktree(tmp_path_factory, label):
    """A repo on ``master``; the candidate branch is in a linked worktree."""
    root = tmp_path_factory.mktemp(label)
    _git(root, "init", "-b", "master")
    _write(root, "base.py", "base = 1\n")
    _commit(root, "base")
    wt = root.parent / (root.name + "-wt")
    _git(root, "worktree", "add", "-b", "feature", str(wt))
    _write(wt, "mine.py", "x = 1\n")
    _commit(wt, "work")
    return root, wt


def test_standing_on_the_integration_target_is_not_a_pass(
    tmp_path_factory, capsys
):
    root, _ = _root_and_worktree(tmp_path_factory, "on-target")
    code, out = _verdict(root, capsys)
    assert code == merge_ready.CANNOT_TELL, out
    assert "integration target itself" in out


def test_the_target_arriving_as_a_remote_name_still_fires(
    tmp_path_factory, capsys
):
    """``master`` tracking ``origin/master`` is one branch under two names.

    This spelling is what a real checkout has, and it is the one the first
    version of the check missed -- it compared the names for equality, so the
    gate went on to read ``behind == 0`` and print ``ready: aligned``.
    """
    root, _ = _root_and_worktree(tmp_path_factory, "upstream")
    clone = root.parent / (root.name + "-clone")
    _git(root, "clone", str(root), str(clone))
    assert "origin/master" in _git(clone, "rev-parse", "--abbrev-ref", "master@{upstream}")
    code, out = _verdict(clone, capsys)
    assert code == merge_ready.CANNOT_TELL, out
    assert "integration target itself" in out


def test_a_branch_cut_and_not_committed_on_is_still_ready(
    tmp_path_factory, capsys
):
    """The case the ``tip == target_tip`` shortcut was written for.

    It must keep answering ``ready``: this checkout holds a real candidate
    branch, it simply has nothing on it yet. Only a checkout standing on the
    target itself is the ill-posed question.
    """
    root, _ = _root_and_worktree(tmp_path_factory, "fresh-cut")
    _git(root, "checkout", "-b", "fresh")
    code, out = _verdict(root, capsys)
    assert code == merge_ready.READY, out
    assert "nothing to land" in out


def test_the_run_directory_is_not_taken_for_the_workers_tree(
    tmp_path_factory, monkeypatch, capsys
):
    """Keyed at the root, standing in the worktree: read the worktree."""
    root, wt = _root_and_worktree(tmp_path_factory, "keyed-away")
    monkeypatch.setattr(
        checkout, "own_checkout", lambda *a, **k: (str(wt), checkout.SESSION)
    )
    monkeypatch.chdir(root)
    code = merge_ready.main([])
    out = capsys.readouterr()
    assert code == merge_ready.READY, out.out + out.err
    assert "aligned" in out.out


def test_an_explicit_repo_survives_the_lookup_failing(
    tmp_path_factory, monkeypatch, capsys
):
    _, wt = _root_and_worktree(tmp_path_factory, "explicit")

    def boom(*a, **k):
        raise RuntimeError("no daemon")

    monkeypatch.setattr(checkout, "own_checkout", boom)
    code, out = _verdict(wt, capsys)
    assert code == merge_ready.READY, out


# --------------------------------------------------------------------------- #
# the tree the merge runs in
#
# Everything above asks about two commits. A merge happens in a working tree,
# and git refuses to start one whose result would write a file that tree is
# dirty on -- no conflict, nothing the branch's owner can fix, and the gate
# used to answer 0 straight through it. It bit two consecutive integration
# rounds in this repository (board
# ``claunch-merge-ready-dirty-checkout-7w7g``), found by hand both times.
#
# The refusal rule these pin was measured, not assumed (git 2.48.1): a merge
# that updates ``a.py`` is refused when ``a.py`` is locally modified, runs
# when an untouched ``b.py`` is modified, is refused when an untracked file
# sits where the merge would create one, and is refused even when the local
# edit is byte-identical to what the merge would write. So the predicate is a
# path-set intersection and nothing else.
# --------------------------------------------------------------------------- #
def test_a_dirty_file_the_merge_writes_stops_a_branch_that_is_otherwise_ready(
    repo, checkout_tree, capsys
):
    """The positive control, and the whole reason this option exists.

    ``tree-branch`` is aligned -- the target never moved, so every check above
    this one passes and the old gate answered ``0``. The merge cannot start.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.DIRTY_CHECKOUT, out
    assert "written.py" in out
    # the branch side really was ready: this is not a rebase or a re-measure
    assert "ready: aligned" in out


def test_an_untracked_file_in_the_way_stops_it_too(repo, checkout_tree, capsys):
    """git refuses on untracked files as well, with a different message.

    Reading only tracked modifications would pass this, and the merge would
    still be refused -- ``The following untracked working tree files would be
    overwritten by merge``.
    """
    _write(checkout_tree, "made.py", "something else\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.DIRTY_CHECKOUT, out
    assert "made.py" in out


def test_a_dirty_file_the_merge_never_touches_is_not_in_the_way(
    repo, checkout_tree, capsys
):
    """First negative control: dirty is not the question, *which files* is.

    A gate that answered ``4`` for any dirty tree would be useless here --
    this repository's shared checkout is almost never completely clean.
    """
    _write(checkout_tree, "untouched.py", "y = LOCAL\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.READY, out
    assert "untouched.py" not in out
    # rule 1 of claunch-peyn: the empty answer carries its denominator, so
    # "nothing blocked" cannot be confused with "nothing was looked at"
    assert "0 of 2 blocked" in out
    assert "1 dirty entry" in out


def test_the_workers_own_dirty_worktree_does_not_change_the_verdict(
    repo, checkout_tree, capsys
):
    """Second negative control, and the one that decides the default.

    This same script is the worker's alignment gate, run from the worker's
    own worktree -- which is dirty *because the worker is working*. If the
    tree check were on by default, every worker in the mesh would be answered
    ``4`` for doing its job and the step this gate exists to open would never
    open. The same tree, the same dirt, the same branch: the only difference
    is the flag.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")

    without = _verdict(
        checkout_tree, capsys, "--branch", "tree-aligned", "--target", "tree-target"
    )
    assert without[0] == merge_ready.READY, without[1]
    assert "dirty" not in without[1]

    with_flag = _verdict(
        checkout_tree,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert with_flag[0] == merge_ready.DIRTY_CHECKOUT, with_flag[1]


def test_the_live_shape_only_the_overlapping_files_are_named(
    repo, checkout_tree, capsys
):
    """Third negative control: the condition that was actually standing.

    The round this was filed in had another session holding 19 uncommitted
    entries in the checkout master sits in, of which four were files the
    pending merge writes. What the gate has to do there is name those four
    and not the other fifteen -- a verdict that dumped the whole dirty list
    would send the reader after files nobody has to touch.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    _write(checkout_tree, "made.py", "in the way\n")
    _write(checkout_tree, "untouched.py", "y = LOCAL\n")
    _write(checkout_tree, "unrelated.py", "nobody cares\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.DIRTY_CHECKOUT, out
    assert "2 of 2" in out
    assert "4 dirty entry" in out
    assert "written.py" in out and "made.py" in out
    assert "untouched.py" not in out and "unrelated.py" not in out


def test_the_tree_is_found_from_the_target_when_no_path_is_given(
    repo, checkout_tree, capsys
):
    """``--checkout`` bare: git knows which worktree holds the target.

    Passing a path means the caller is remembering where the target lives,
    and that memory is the thing that goes stale -- master moves between
    checkouts and an integration branch is often checked out nowhere at all.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        merge_ready.DERIVE_FROM_TARGET,
    )
    assert code == merge_ready.DIRTY_CHECKOUT, out
    assert "written.py" in out


def test_a_target_no_checkout_holds_passes_and_says_out_of_how_many(repo, capsys):
    """No worktree on the target: nothing can refuse the merge, and why.

    A real pass rather than a shrug -- the leader's own merge worktree is
    made fresh and is clean by construction. But it is an *empty* answer, so
    rule 1 of ``claunch-peyn`` applies: it carries how many checkouts were
    examined to reach it.
    """
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "aligned-branch",
        "--target",
        "aligned-target",
        "--checkout",
        merge_ready.DERIVE_FROM_TARGET,
    )
    assert code == merge_ready.READY, out
    assert "worktree(s) has aligned-target checked out" in out
    assert "none of 2" in out


def test_a_checkout_that_is_not_one_is_refused_rather_than_passed(
    repo, tmp_path, capsys
):
    """Rule 2 of ``claunch-peyn``: an input it cannot read is not a pass.

    The cheap failure here would be an unreadable path producing an empty
    dirty set and therefore an empty intersection -- exit ``0``, output
    indistinguishable from a clean tree.
    """
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(tmp_path / "no-such-tree"),
    )
    assert code == merge_ready.CANNOT_TELL, out
    assert "not a git checkout" in out


def test_a_not_ready_verdict_keeps_its_code_and_still_reports_the_tree(
    repo, checkout_tree, capsys
):
    """A conflict is still a conflict, and the dirty tree still gets said.

    The two are independent problems with different owners: the branch's
    owner fixes the conflict, and only the session holding the uncommitted
    work can clear the tree. Holding the tree finding back until the branch
    side happens to be ready would rebuild the delay this whole option exists
    to remove -- both times the gap bit, the cost was the hours between the
    gate answering and somebody running ``git status`` on a hunch.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-branch",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.REMEASURE, out
    assert "also, the working tree this merge would run in" in out
    assert "written.py" in out
    assert "second, independent problem" in out


def test_the_note_beside_a_not_ready_verdict_is_never_an_empty_heading(
    repo, checkout_tree, capsys
):
    """A clean tree and an unexamined tree must not print the same thing.

    The note prints a heading and then what it found, and the heading is on
    stdout. So a body that went to stderr -- or that was empty because there
    was nothing to say -- would leave a reader with a heading and nothing
    under it, which reads as a tree that was examined and found clean. That
    is rule 1 of ``claunch-peyn`` (an empty answer carries its denominator)
    landing one step away from where it was first fixed.

    Three states, three outputs: not asked -> no heading at all; asked and
    clean -> the heading and a count out of a total; asked and blocked ->
    the heading and the files.
    """
    not_asked = _verdict(
        repo, capsys, "--branch", "tree-branch", "--target", "tree-target"
    )
    assert not_asked[0] == merge_ready.REMEASURE, not_asked[1]
    assert "also, the working tree" not in not_asked[1]

    # asked, and the tree is clean: still says so, with the denominator
    clean = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-branch",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert clean[0] == merge_ready.REMEASURE, clean[1]
    assert "also, the working tree" in clean[1]
    assert "blocked" in clean[1]
    assert "0 dirty entry" in clean[1]


def test_every_answer_the_tree_check_can_give_says_something(repo, checkout_tree):
    """No route through the tree check returns without a line to print.

    The property the test above pins for one route, held for all of them:
    the pair of line lists is never both empty, so the heading can never be
    the whole output.
    """
    tip = _git(repo, "rev-parse", "tree-aligned").strip()
    cases = [
        (str(checkout_tree), "tree-target"),  # a real tree
        (merge_ready.DERIVE_FROM_TARGET, "tree-target"),  # found from the target
        (merge_ready.DERIVE_FROM_TARGET, "aligned-target"),  # held by no tree
        (str(repo / "not-a-tree"), "tree-target"),  # unreadable
    ]
    for where, target in cases:
        _code, out, err = merge_ready._checkout_check(
            repo, where, "tree-aligned", target, tip
        )
        assert out or err, f"{where} / {target} answered with nothing"


def test_a_conflict_keeps_exit_three_and_the_tree_check_says_it_cannot_answer(
    repo, checkout_tree, capsys
):
    """The other not-ready code: ``3`` stays ``3``, and the tree check says why
    it has nothing.

    Measured, and not what was expected when this test was written. A
    conflicting merge has no result tree, so "which files would this merge
    write" has no answer for it -- the check cannot be run here at all,
    however dirty the tree is. What matters is that it says so instead of
    printing nothing: a silent note under its own heading would read as a
    tree that was examined and found clean, which is the exact defect this
    whole option was written against.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-conflict",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert code == merge_ready.REBASE, out
    assert "also, the working tree this merge would run in" in out
    assert "could not work out which files the merge writes" in out
    assert "reports the merge conflicts" in out
