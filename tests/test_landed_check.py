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

import pytest
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


def _build_base(repo: Path) -> None:
    _git(repo, "init", "-b", "master")
    _commit(repo, "base")
    _git(repo, "checkout", "-b", "feature")
    _commit(repo, "work")


@pytest.fixture
def base(tmp_path, repo_template) -> Path:
    """A repo on ``master`` with one commit and a ``feature`` branch on top."""
    return repo_template("landed-base", _build_base, tmp_path / "base")


def _run(repo: Path, *extra: str) -> int:
    return landed_check.main(["--repo", str(repo), *extra])


# --------------------------------------------------------------------------- #
# not yet
# --------------------------------------------------------------------------- #
def test_a_frozen_branch_nobody_merged_is_not_landed(base):
    repo = base
    assert _run(repo) == 1
    assert _run(repo, "--target", "master") == 1


def test_a_child_stacked_on_the_tip_is_not_a_landing(base, capsys):
    """The false green this gate exists to avoid.

    A worker under a nested worker gets children of its own stacked on its
    branch. Those children *contain* its tip -- they were cut from it -- while
    nothing has been integrated anywhere. Measured on a live mesh: a worker
    froze a tip with nothing landed, and a containment test answered "landed"
    because its own child branch sat on top of it.
    """
    repo = base
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
def test_a_no_ff_merge_into_the_target_is_a_landing(base):
    repo = base
    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(repo, "checkout", "feature")
    assert _run(repo) == 0
    assert _run(repo, "--target", "master") == 0


def test_a_landing_is_seen_past_a_child_that_also_contains_the_tip(
    base,
):
    """Both shapes at once: the real merge is found, the descendant ignored."""
    repo = base
    _git(repo, "checkout", "-b", "child")
    _commit(repo, "child-work")
    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(repo, "checkout", "feature")
    assert _run(repo) == 0


def test_a_nested_worker_measures_against_its_parents_branch(base):
    """A stacked worker's target is the parent branch, never master."""
    repo = base
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
def test_an_unknown_target_cannot_tell_rather_than_denying(base):
    repo = base
    assert _run(repo, "--target", "no-such-branch") == landed_check.CANNOT_TELL


def test_a_directory_that_is_not_a_repository_cannot_tell(tmp_path):
    assert _run(tmp_path) == landed_check.CANNOT_TELL


# --------------------------------------------------------------------------- #
# the way the verify actually calls it
# --------------------------------------------------------------------------- #
def test_the_exit_status_reaches_a_shell(base):
    """The ``verify:`` line runs this as a script, so the status must escape."""
    repo = base
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 1, proc.stderr

    # Merge, then step back onto the branch that landed. Leaving HEAD on
    # master was how this test used to reach exit 0, and that reading was
    # empty: the tip it compared was master's own, so "landed: master
    # contains <master tip>" is true of every repository at every moment.
    # A worker is on its own branch when the gate runs, which is the case
    # worth pinning here.
    _git(repo, "checkout", "master")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    _git(repo, "checkout", "feature")
    proc = subprocess.run(
        [sys.executable, str(CHECK), "--repo", str(repo), "--target", "master"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# whose branch the gate asks about
# --------------------------------------------------------------------------- #
# A cflow run is keyed to the directory it was started in, and its ``verify``
# executes there. Nothing required that directory to be the worker's own
# checkout, and when it was not, this gate read the wrong HEAD and said so
# with confidence. Measured on a live mesh (run ``run-1a318163``): branch
# ``s192-persist-session-flags``, tip ``b847cb7``, merged as ``774c964``
# (``^2 b847cb7``) -- landed by every test git has. Called in the branch's
# worktree the gate said "landed", exit 0; called at the repository root,
# where the run happened to be keyed, it said
#
#     not yet: no merge on any branch but branch master took 0505c8dd7dc3 in
#
# exit 1, and the round could not leave the step. The sentence names the
# worker's branch nowhere and is read as "your branch did not land", which is
# where that round's time went. No test drew "run directory != the worker's
# tree", which is why the defect survived.
from claude_launcher.cflow import checkout  # noqa: E402


def _root_and_worktree(tmp_path_factory, label):
    """A repo on ``master``; the work is on ``feature`` in a linked worktree.

    ``feature`` is merged with ``--no-ff``, so the true answer is "landed" --
    and the repository root, where the run is keyed, is standing on master.
    """
    root = tmp_path_factory.mktemp(label)
    _git(root, "init", "-b", "master")
    _commit(root, "base")
    wt = root.parent / (root.name + "-wt")
    _git(root, "worktree", "add", "-b", "feature", str(wt))
    _commit(wt, "work")
    _git(root, "merge", "--no-ff", "-m", "Merge branch 'feature'", "feature")
    return root, wt


def test_the_run_directory_is_not_taken_for_the_workers_tree(
    tmp_path_factory, monkeypatch, capsys
):
    """The rescue: the gate asks the daemon where this session stands."""
    root, wt = _root_and_worktree(tmp_path_factory, "keyed-away")
    monkeypatch.setattr(
        checkout, "own_checkout", lambda *a, **k: (str(wt), checkout.SESSION)
    )
    monkeypatch.chdir(root)
    assert landed_check.main([]) == 0
    assert "landed" in capsys.readouterr().out


def test_standing_on_the_integration_target_is_not_an_answer(
    tmp_path_factory, capsys
):
    """The half no lookup can rescue, so it must not be answered confidently.

    A session whose recorded directory is not the tree it works in leaves no
    machine fact linking the run to its branch: the daemon records a session's
    cwd when it is created, and only an operator migration (stop, relaunch)
    changes it. Naming that beats "not yet", which sends the reader to look
    for a fault in a branch the gate never read.
    """
    root, _ = _root_and_worktree(tmp_path_factory, "on-target")
    assert _run(root) == landed_check.CANNOT_TELL
    err = capsys.readouterr().err
    assert "integration target itself" in err
    assert "master" in err


def test_naming_this_very_branch_as_the_target_is_not_a_landing(
    tmp_path_factory, capsys
):
    """``--target`` made the same mistake green rather than red.

    Ancestry is reflexive: ``merge-base --is-ancestor master master`` exits 0,
    so the gate printed "landed: master contains <tip>" and passed a step
    whose branch it had never looked at. Confidently wrong in the other
    direction, and worse -- a red gate stops the run and gets investigated.
    """
    root, _ = _root_and_worktree(tmp_path_factory, "target-self")
    assert _run(root, "--target", "master") == landed_check.CANNOT_TELL
    assert "integration target itself" in capsys.readouterr().err


def test_an_explicit_repo_survives_the_lookup_failing(
    tmp_path_factory, monkeypatch, capsys
):
    """``--repo`` is the escape the gate tells people to use.

    It may not depend on a daemon: the case where someone reaches for it is
    the case where the machine could not work the tree out by itself.
    """
    _, wt = _root_and_worktree(tmp_path_factory, "explicit")

    def boom(*a, **k):
        raise RuntimeError("no daemon")

    monkeypatch.setattr(checkout, "own_checkout", boom)
    assert _run(wt) == 0
    assert "landed" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# --fetch: a landing that happened as a pull request lives on the remote
# --------------------------------------------------------------------------- #
def _build_pr_landing(tmp_path: Path):
    """A worker clone whose upstream is ``<remote>/master``, and a merge of its
    tip that exists ONLY on the remote -- the shape improv-worker-remote's
    landed gate stands in front of."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(bare))
    worker = tmp_path / "worker"
    _git(tmp_path, "init", "-b", "master", str(worker))
    _commit(worker, "base")
    _git(worker, "remote", "add", "ghe", str(bare))
    _git(worker, "push", "-q", "ghe", "master:refs/heads/master")
    _git(worker, "checkout", "-q", "-b", "feature")
    _commit(worker, "work")
    _git(worker, "push", "-q", "ghe", "HEAD:refs/heads/feature")
    _git(worker, "fetch", "-q", "ghe")
    _git(worker, "branch", "-q", "--set-upstream-to=ghe/master")
    # the "leader" merges the PR elsewhere: a second clone, --no-ff, pushed
    leader = tmp_path / "leader"
    _git(tmp_path, "clone", "-q", str(bare), str(leader))
    _git(leader, "merge", "-q", "--no-ff", "-m", "merge feature", "origin/feature")
    _git(leader, "push", "-q", "origin", "master:refs/heads/master")
    return worker


def test_a_pr_landing_is_seen_only_after_a_fetch(tmp_path, capsys):
    worker = _build_pr_landing(tmp_path)
    # stale remote-tracking ref: nothing here knows the merge happened
    assert _run(worker, "--target", "@{upstream}") == 1
    # --fetch refreshes the upstream's remote before asking, and the merge is there
    assert _run(worker, "--fetch", "--target", "@{upstream}") == 0
    out = capsys.readouterr().out
    assert "landed" in out


def test_fetch_of_a_local_only_target_cannot_tell(base, capsys):
    """A local branch tracks no remote; refreshing it is a wrong question,
    not a silent no-op -- the gate says so instead of measuring stale state."""
    assert _run(base, "--fetch", "--target", "master") == landed_check.CANNOT_TELL
    assert "not a remote-tracking branch" in capsys.readouterr().err


def test_fetch_without_a_target_cannot_tell(base, capsys):
    assert _run(base, "--fetch") == landed_check.CANNOT_TELL
    assert "--target" in capsys.readouterr().err
