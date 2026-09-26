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
import json
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

    # Cut and never committed on, and the target moved: the tip is an ancestor
    # of the target exactly as a landed one is, and nothing ever landed.
    _scenario(path, "empty", target_adds={"theirs.py": "y = 2\n"}, branch_adds={})

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
    # ``reset`` first, and it is not belt-and-braces: a test that staged
    # something leaves the index ahead of HEAD, and ``checkout -- .`` restores
    # the working tree *from the index*, so it would hand the next test the
    # staged content back as if it were clean.
    _git(where, "reset", "-q")
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


def test_landed_exit_gives_the_landing_its_own_code(repo, capsys):
    """Under ``awaits`` only a changed exit code is heard (claunch-dlq0o).

    Ready before the merge and landed after it were both ``0``, so the
    await-landing probe never told the waiting worker that it had landed.
    With ``--landed-exit`` the landing is ``5``; a branch not yet in its
    target keeps answering what it answered before.
    """
    code, out = _verdict(
        repo, capsys, "--landed-exit",
        "--branch", "landed-branch", "--target", "landed-target",
    )
    assert code == merge_ready.LANDED, out
    assert "landed" in out

    code, out = _verdict(
        repo, capsys, "--landed-exit",
        "--branch", "aligned-branch", "--target", "aligned-target",
    )
    assert code == merge_ready.READY, out


def test_a_branch_with_nothing_on_it_is_not_a_landing(repo, capsys):
    """Ancestry alone cannot tell a landing from a branch never committed on.

    Both leave the tip inside the target once the target moves (claunch-gzi50).
    Calling the second one landed sent the worker's rebase gate to 5 and its
    instructions to ``request_goto landed`` for work that never existed. The
    landed tip came in through a merge's second parent; this one is on the
    target's own first-parent line.
    """
    code, out = _verdict(
        repo, capsys, "--landed-exit",
        "--branch", "empty-branch", "--target", "empty-target",
    )
    assert code == merge_ready.READY, out
    assert "nothing to land" in out
    assert "landed --" not in out

    code, out = _verdict(
        repo, capsys, "--landed-exit",
        "--branch", "landed-branch", "--target", "landed-target",
    )
    assert code == merge_ready.LANDED, out


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


def test_a_green_preview_accepts_an_additive_target_advance(repo, capsys):
    """A target commit may land during a window without spending it twice."""
    ref = "refs/claunch/preview/preview-branch"
    preview = _preview(repo, "preview-branch", "preview-target", ref)
    preview_tree = _git(repo, "rev-parse", f"{preview}^{{tree}}").strip()
    _git(repo, "checkout", "-q", "preview-target")
    _write(repo, "accepted_advance.py", "w = 3\n")
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
        "--allow-target-advance",
    )
    assert code == merge_ready.READY, out
    assert "target advance is additive" in out


def test_an_interacting_target_advance_still_requires_a_rebase(repo, capsys):
    ref = "refs/claunch/preview/preview-branch"
    preview = _preview(repo, "preview-branch", "preview-target", ref)
    preview_tree = _git(repo, "rev-parse", f"{preview}^{{tree}}").strip()
    _git(repo, "checkout", "-q", "preview-target")
    _write(repo, "mine.py", "changed = True\n")
    _commit(repo, "preview: target changes branch file")
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
        "--allow-target-advance",
    )
    assert code == merge_ready.REBASE, out


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
# the sweep receipt: the re-measurement was already run
# --------------------------------------------------------------------------- #
def _landing_tree(repo, branch: str, target: str) -> str:
    """The tree ``git merge-tree --write-tree`` says the merge would write."""
    out = _git(repo, "merge-tree", "--write-tree", branch, target)
    return out.strip().splitlines()[0]


def _file_receipt(repo, sha_name: str, **fields) -> Path:
    """A receipt the way ``sweep.py run`` files it, under the test's home.

    The name has to be a full sha plus ``.json``: any other name is refused
    on sight as hand-written, which is the property being borrowed here.
    """
    sweep = merge_ready.sweep
    directory = sweep.receipts_dir(Path(repo))
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {
        "commit": sha_name,
        "tree": fields.pop("tree"),
        "code_tree": fields.pop("code_tree", None),
        "branch": "preview",
        "exit_code": 0,
        "counts": {"passed": 12, "skipped": 1},
        "failures": [],
        "dirty": False,
        "session": "t",
        "finished_at": "2026-08-29T00:00:00+00:00",
        **fields,
    }
    path = directory / f"{sha_name}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


def test_a_green_sweep_receipt_for_the_landing_tree_clears_the_re_measurement(
    repo, capsys
):
    """Board ``claunch-ol3b``: wide answers narrow.

    The leader's preview sweep ran the full suite on the merge commit's tree;
    the branch's targeted selection is a subset of that suite, so the moved
    baseline's question is already answered and asking for it again burns a
    measurement window on a number nobody needs.
    """
    tree = _landing_tree(repo, "clean-branch", "clean-target")
    sha = "a" * 40
    _file_receipt(repo, sha, tree=tree)
    code, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "clean-target"
    )
    assert code == merge_ready.READY, out
    assert "swept green" in out
    assert f"via the receipt for {sha[:12]}" in out
    assert f"same tree {tree[:12]}" in out


def test_a_receipt_for_a_board_only_difference_still_answers(repo, capsys):
    """The weaker rung: the trees differ, but only in ``.beads``.

    The board commit lands after the sweep every round by construction, so a
    gate that demanded byte-identical trees would send the fleet to re-measure
    over a file the suite never reads (the ``claunch-etr7`` instance).
    """
    sweep = merge_ready.sweep
    tree = _landing_tree(repo, "clean-branch", "clean-target")
    code = sweep.code_tree(Path(repo), tree)
    sha = "b" * 40
    _file_receipt(repo, sha, tree="c" * 40, code_tree=code)
    code_, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "clean-target"
    )
    assert code_ == merge_ready.READY, out
    assert "identical outside" in out


def test_a_receipt_for_another_tree_does_not_answer(repo, capsys):
    """A green run of some other content is not evidence about this landing."""
    _file_receipt(repo, "d" * 40, tree="e" * 40, code_tree="f" * 40)
    code, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "clean-target"
    )
    assert code == merge_ready.REMEASURE, out
    # The empty answer prints its denominator (claunch-peyn).
    assert "no green sweep receipt for the landing tree" in out


def test_a_red_receipt_does_not_answer(repo, capsys):
    """A receipt that found failures is a verdict, and the verdict is red --
    not a pass for the branch side.
    """
    tree = _landing_tree(repo, "clean-branch", "clean-target")
    _file_receipt(repo, "1" * 40, tree=tree, counts={"passed": 11, "failed": 1})
    code, out = _verdict(
        repo, capsys, "--branch", "clean-branch", "--target", "clean-target"
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


def test_a_staged_change_to_an_untouched_file_stops_the_merge_all_the_same(
    repo, checkout_tree, capsys
):
    """The index is not a per-path question, and the intersection missed that.

    Measured on git 2.48.1, the same tree and the same file, twice:

    * ``untouched.py`` modified in the working tree -> the merge runs
      (the test above)
    * ``untouched.py`` modified **and staged** -> ``error: Your local changes
      to the following files would be overwritten by merge``, exit 2

    ``untouched.py`` is not in the merge's path set either time, so the
    intersection this check was built on answers ``0 of 2 blocked`` for both
    -- and the second one is a merge that cannot start. Board
    ``claunch-tsh7``: the gate answered ``0`` for ``s390-codex-enter`` while
    another session had two unrelated files staged in the shared checkout,
    and ``git merge`` refused on exactly those two.

    A ``--no-ff`` merge is what this repository lands with (``improv-leader``
    and ``improv-mid`` both spell it out), and it requires the whole index to
    match ``HEAD`` before it will begin. So a staged entry blocks whatever
    path it sits on.
    """
    _write(checkout_tree, "untouched.py", "y = LOCAL\n")
    _git(checkout_tree, "add", "untouched.py")
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
    assert "untouched.py" in out
    # and it says *why* this one blocks, because the reader who goes looking
    # for untouched.py in the merge's diff will not find it there
    assert "staged" in out
    # the branch side really was ready -- same as the positive control above
    assert "ready: aligned" in out


def test_a_staged_entry_is_reported_apart_from_the_files_the_merge_writes(
    repo, checkout_tree, capsys
):
    """Two blockers, two reasons, and the report keeps them apart.

    ``written.py`` is in the merge's path set and ``untouched.py`` is not.
    Both stop the merge and the remedies are identical, but a report that
    merged them into one list would tell the reader that ``untouched.py`` is
    a file this merge writes -- which is the fact they would go and check.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    _write(checkout_tree, "untouched.py", "y = LOCAL\n")
    _git(checkout_tree, "add", "untouched.py")
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
    assert "untouched.py" in out
    # the merge writes two files and exactly one of them is dirty; the staged
    # entry is counted on its own line rather than folded into that ratio
    assert "1 of 2" in out
    assert "1 staged" in out


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


def test_a_target_no_checkout_holds_is_not_measured_and_does_not_pass(repo, capsys):
    """No worktree on the target: the check did not run, so it is not green.

    This answered ``READY`` first, with "no working tree can refuse this
    merge", and that sentence claims more than was measured -- a tree that
    does not hold the target *now* can be switched onto it later and refuse
    then. Rule 3 of ``claunch-peyn`` is the one that settles it: a check that
    did not run does not count towards a green, and the count of what did not
    run belongs in the output.

    The denominator survives the change (rule 1): "none of N worktree(s)"
    says how many were examined to reach the empty answer.
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
    assert code == merge_ready.CANNOT_TELL, out
    assert "worktree(s) has aligned-target checked out" in out
    assert "none of 2" in out
    # and it says what would answer it, rather than only what it could not do
    assert "--checkout <path>" in out


def test_a_merge_that_writes_nothing_needs_no_tree(repo, capsys):
    """The one case where a missing checkout really is a pass, measured.

    ``landed-branch`` is already inside its target, so the merge writes no
    files at all -- and a merge that writes nothing cannot be refused by any
    working tree, whether or not one is standing on the target. Separating
    this from the case above keeps the honest ``2`` from firing on every
    already-landed branch, which is common enough that folding the two would
    make the verdict noise.
    """
    code, out = _verdict(
        repo,
        capsys,
        "--branch",
        "landed-branch",
        "--target",
        "landed-target",
        "--checkout",
        merge_ready.DERIVE_FROM_TARGET,
    )
    assert code == merge_ready.READY, out
    assert "writes 0 files" in out


def test_removing_the_worktree_changes_the_answer_on_the_same_commits(
    repo, checkout_tree, capsys
):
    """This verdict is a function of live trees, not of commits.

    Every other answer this gate gives is decided by two commits, so quoting
    one later is quoting something that is either still true or visibly stale
    from the hashes. This one is decided by who is standing where and what
    they have not committed, so the same code and the same two commits give
    different answers minutes apart. Measured in the real repository during
    the round this was written: ``4``, then ``0``, because a worktree was
    removed in between.

    The test pins the property rather than a remedy, because there is no
    remedy -- what it demands is that a ``checkout:`` line be cited with the
    time it was printed.
    """
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    blocked, blocked_out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert blocked == merge_ready.DIRTY_CHECKOUT, blocked_out

    _git(checkout_tree, "checkout", "-q", "--", ".")
    cleared, cleared_out = _verdict(
        repo,
        capsys,
        "--branch",
        "tree-aligned",
        "--target",
        "tree-target",
        "--checkout",
        str(checkout_tree),
    )
    assert cleared == merge_ready.READY, cleared_out


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


def test_the_five_checkout_answers_are_all_distinguishable_in_the_record(
    repo, checkout_tree
):
    """Every state this check can reach leaves a different record.

    The exit code is not enough on its own: two of these five are ``0`` and
    two are ``2``, and that is correct -- ``0`` means the merge can start and
    ``2`` means this gate did not measure it, whatever the reason. What must
    not collapse is the *record*, because that is what somebody reads back
    later when they are asking why a green was green.

    The shape of the question comes from ``claunch-64hs`` (worker-64hs, this
    round): a red-because-the-write-failed receipt and a red-because-it-was-
    always-red receipt came out byte-identical, so the gate answered both the
    same way and the only trace of the difference was a warning on stderr --
    which the gate does not read and which dies with the terminal. Checking
    "does it go green" would not have found that; checking "do the two
    records differ" is what found it.

    Limit, stated rather than papered over: this gate writes no file, so its
    whole record is the exit code and the printed lines. There is no
    persisted artifact to compare byte-for-byte the way 64hs could, and
    nothing here pins what a *reader* does with the lines.
    """
    tip = _git(repo, "rev-parse", "tree-aligned").strip()
    seen = {}

    def record(label, where, target):
        code, out, err = merge_ready._checkout_check(
            repo, where, "tree-aligned", target, tip
        )
        seen[label] = (code, "\n".join(out + err))

    # 1. a tree holds the target and is dirty on a file the merge writes
    _write(checkout_tree, "written.py", "x = LOCAL\n")
    record("found-dirty", str(checkout_tree), "tree-target")

    # 2. the same tree, clean
    _git(checkout_tree, "checkout", "-q", "--", ".")
    record("found-clean", str(checkout_tree), "tree-target")

    # 3. no tree holds the target, and the merge does write files
    record("absent-writes", merge_ready.DERIVE_FROM_TARGET, "aligned-target")

    # 4. no tree holds the target, and the merge writes nothing
    landed = _git(repo, "rev-parse", "landed-branch").strip()
    code, out, err = merge_ready._checkout_check(
        repo, merge_ready.DERIVE_FROM_TARGET, "landed-branch", "landed-target", landed
    )
    seen["absent-empty"] = (code, "\n".join(out + err))

    # 5. a path was named and it is not a checkout at all
    record("not-a-checkout", str(repo / "nowhere"), "tree-target")

    assert seen["found-dirty"][0] == merge_ready.DIRTY_CHECKOUT, seen["found-dirty"]
    assert seen["found-clean"][0] == merge_ready.READY, seen["found-clean"]
    assert seen["absent-writes"][0] == merge_ready.CANNOT_TELL, seen["absent-writes"]
    assert seen["absent-empty"][0] == merge_ready.READY, seen["absent-empty"]
    assert seen["not-a-checkout"][0] == merge_ready.CANNOT_TELL, seen["not-a-checkout"]

    texts = {label: text for label, (_code, text) in seen.items()}
    for label, text in texts.items():
        assert text.strip(), f"{label} left no record at all"
    assert len(set(texts.values())) == len(texts), (
        "two states left the same record: "
        + repr({k: v[:80] for k, v in texts.items()})
    )

    # and the two pairs that share an exit code say which is which in words,
    # since the code alone cannot carry it
    assert "blocked" in texts["found-clean"]
    assert "writes 0 files" in texts["absent-empty"]
    assert "none of" in texts["absent-writes"]
    assert "not a git checkout" in texts["not-a-checkout"]


# --------------------------------------------------------------------------- #
# --fetch: the target is a remote base that moves on the remote
# --------------------------------------------------------------------------- #
def _build_remote_base(tmp_path):
    """A worker clone with upstream ``ghe/master``, and master advanced on the
    remote by somebody else -- what improv-worker-remote's wait probe faces."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(bare))
    worker = tmp_path / "worker"
    _git(tmp_path, "init", "-b", "master", str(worker))
    _write(worker, "base.txt", "base\n")
    _git(worker, "add", "base.txt")
    _git(worker, "commit", "-q", "-m", "base")
    _git(worker, "remote", "add", "ghe", str(bare))
    _git(worker, "push", "-q", "ghe", "master:refs/heads/master")
    _git(worker, "checkout", "-q", "-b", "feature")
    _write(worker, "work.txt", "work\n")
    _git(worker, "add", "work.txt")
    _git(worker, "commit", "-q", "-m", "work")
    _git(worker, "fetch", "-q", "ghe")
    _git(worker, "branch", "-q", "--set-upstream-to=ghe/master")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    _write(other, "other.txt", "other\n")
    _git(other, "add", "other.txt")
    _git(other, "commit", "-q", "-m", "other")
    _git(other, "push", "-q", "origin", "master:refs/heads/master")
    return worker


def test_fetch_sees_a_remote_base_that_moved(tmp_path, capsys):
    worker = _build_remote_base(tmp_path)
    # without a fetch the stale ghe/master still reads as aligned
    assert _run(worker) == merge_ready.READY
    capsys.readouterr()
    # with it the moved baseline is what the probe reports
    code = _run(worker, "--fetch")
    out = capsys.readouterr().out
    assert code == merge_ready.REMEASURE, out
    assert "ghe/master (upstream)" in out


def test_fetch_of_a_local_only_target_cannot_tell(repo, capsys):
    code = _run(repo, "--branch", "clean-branch", "--target", "clean-target", "--fetch")
    assert code == merge_ready.CANNOT_TELL
    assert "not a remote-tracking branch" in capsys.readouterr().err
