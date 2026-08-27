"""Would the parent accept this branch right now -- and if not, which fix?

The round trip this closes, as it actually runs. A worker rebases onto the
integration target, files ``LANDING REQUEST @ <tip>``, freezes its branch and
sits in ``await-landing`` doing nothing. Meanwhile the leader integrates in
*batches*: ``improv-leader`` opens a window every 300s and merges whatever has
collected in it, then sweeps. So the wait is long by design, and the target
moves during it -- other workers land. The leader's ``preflight`` then measures
the divergence by hand and sends the branch back with ``REBASE REQUESTED``.

Count what that rejection costs: a leader turn spent measuring, a mesh round
trip, a board transition (``in_review`` -> ``in_progress``) and back, and a
worker woken to redo a step it already passed. Now count what the rejection
*is*: two git commands. It is a measurement, not a judgement -- and cflow
already runs measurements without spending anyone's turn. ``verify`` is one
(the machine gate on leaving a step); ``awaits.probe`` is the other (the
daemon re-measures while the run sits still, and speaks only when the answer
changes). This script is the measurement both of them call, and the leader's
``preflight`` calls the same one, so the two sides cannot disagree about the
same branch any more.

Two verdicts, not one
---------------------
``improv-leader`` used to give one answer -- "rebase first" -- for two
different facts, and that conflation is the friction. They come apart:

* **Conflict.** ``git merge-tree`` says the two sides collide. Nothing but a
  rebase (or a merge somebody resolves) gets past that. Exit ``3``.
* **A moved baseline.** The target is ahead of the fork point but the merge is
  clean. The branch *merges*; what is stale is the **evidence**. A worker's
  targeted test numbers were taken on its own base, and if the base moved they
  do not describe the tree that will land. Exit ``1``.

Distance alone is not the first one. With ``behind == 0`` the target is an
ancestor of this branch and a ``--no-ff`` merge cannot collide with anything;
demanding a rebase there is an empty demand. That much is now settled between
worker and leader (board ``claunch-mde``, leader ruling of 2026-08-26).

But a clean ``merge-tree`` is not a pass either, and the measured case is from
this very round: ``s130``'s branch added a test requiring every gate under
``tools/`` to put this checkout's ``src`` first, while master meanwhile landed
a fifth gate (``landed_check.py``) that did not. Different files, so the text
does not collide and ``merge-tree`` reports clean -- and the merged tree is
red. It took a human rebasing to meet it; the fix is ``90a07a9``. A gate that
passed on "no conflict" would have shipped that red. So ``behind > 0`` demands
a re-measurement even when the merge is clean.

Re-measuring without rebasing
-----------------------------
The leader accepts the cheaper remedy: build the merge that will actually
happen, run the targets on *that*, and report the numbers -- the branch itself
never moves, so the tip the reviewer signed off on stays the tip that lands.
This script makes that remedy a **fact it can check** rather than a claim in a
report. The worker leaves the preview merge behind as a ref:

    git worktree add --detach <tmp> <target>
    git -C <tmp> merge --no-ff <branch>
    <run the targeted tests in <tmp>>
    git update-ref refs/claunch/preview/<branch> $(git -C <tmp> rev-parse HEAD)

and this asks one question of it: are its two parents exactly this tip and the
target's tip? If they are, the numbers were taken on the pair being judged
now. If the target moves again, the second parent stops matching and the
verdict goes back to ``1`` by itself -- which is the property the whole design
turns on, because that is the moment a stale baseline is created and nobody is
looking.

What it does not prove, kept honest in the same spirit as
``landed_check.py``: that anyone actually ran the tests on that preview. It
proves the tree existed and which two commits it merged. The numbers in the
report remain the worker's word, judged by the leader, as they always were.
Nor does it know whether the merged tree runs -- ``git`` being quiet means the
lines did not collide. The leader's post-merge sweep is the authority there,
and ``mergecheck.py`` covers the one silent case in between (one side deletes
a symbol the other started calling).

Exit codes -- ``0`` ready, ``1`` re-measure, ``2`` could not tell, ``3``
rebase. Two failure codes rather than one because ``awaits.probe`` compares
**exit codes only** and deliberately ignores output (``cflow/model.py``,
:class:`Awaits`): folded into a single ``1``, a branch that went from stale to
conflicted would flip nothing and the daemon would say nothing. ``2`` keeps
the meaning ``landed_check.py`` gave it so the two gates read alike.

Output is ASCII only, for the reason ``mergecheck`` gives: it prints into a
Windows console as often as a Unix one, and a character outside the code page
raises rather than garbles.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Every gate script under tools/ puts this checkout first. This one imports
# nothing from the package -- it asks git and nothing else -- and keeps the
# rule anyway, for the reason landed_check.py gives: the line that makes an
# import resolve against the tree being checked is worth more sitting here
# than remembered. tests/test_gates_run_this_checkout.py holds them all to it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

READY = 0
REMEASURE = 1
CANNOT_TELL = 2
REBASE = 3

#: The target a branch integrates into when nothing says otherwise. A nested
#: worker's target is its parent's branch, not this -- see :func:`_target`.
DEFAULT_TARGET = "master"

#: Where a preview merge is left for this gate to find. Under ``refs/claunch``
#: rather than ``refs/heads`` so it is not a branch: it must not show up in
#: ``git branch``, must not be pushed by default, and must never be something
#: the leader could merge by mistake.
PREVIEW_NS = "refs/claunch/preview"


def _resolve_repo(explicit: Optional[str]) -> Tuple[Path, str]:
    """Which checkout to ask about, and how we came to think so.

    Same resolution as ``landed_check.py``, and for the same defect: the run's
    directory was assumed to be this session's own tree, and it is not
    whenever the run was keyed at the repository root while the worker stands
    in a worktree. This gate failed that shape in the other direction --
    standing on ``master`` it read ``tip == target_tip`` and answered
    ``ready: nothing to land``, exit 0, about a branch it never looked at.

    ``--repo`` wins outright so tests and hand-runs never depend on a daemon;
    that precedence lives in ``own_checkout`` so all three answers are decided
    in one place. Every failure of the lookup falls back to the working
    directory, the answer this always gave.
    """
    try:
        from claude_launcher.cflow import checkout

        where, how = checkout.own_checkout(explicit)
        return Path(where), how
    except Exception:
        # No package, no daemon, no managed session: the answer every caller
        # gave before this existed.
        return Path(explicit or ".").resolve(), "named" if explicit else "run cwd"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _target(repo: Path, branch: str) -> Tuple[str, str]:
    """The branch this one integrates into, and how we came to think so.

    A ``verify`` command is a static string in a workflow file, and the target
    is not static: a worker reporting to the leader integrates into master, a
    worker stacked under a middle worker integrates into its parent's branch.
    Git already has a name for "the branch mine is based on", so that is asked
    first -- ``git branch --set-upstream-to=<base>`` costs the worker one
    command and turns the base into a fact the machine can read instead of a
    sentence in a report.
    """
    up = _git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        f"{branch}@{{upstream}}",
    )
    if up.returncode == 0 and up.stdout.strip():
        return up.stdout.strip(), "upstream"
    return DEFAULT_TARGET, "default"


def _same_branch(branch: str, target: str) -> bool:
    """Are these two names the same branch?

    ``master`` and ``origin/master`` are one branch under two names, and
    :func:`_target` hands back whichever one git records as the upstream. The
    first version of this check compared the two names for equality and so
    missed exactly the case it was written for: standing on ``master`` with
    the upstream set, the target came back ``origin/master``, the check did
    not fire, and the gate answered ``ready: aligned`` exit 0 about a branch
    it had never looked at.

    The suffix test is not a convenience about remotes. Asking "is X ready to
    merge into X" has no answer whichever spelling each side arrives in, and a
    branch tracking its own remote counterpart is that question too.
    """
    return branch == target or target.endswith("/" + branch)


def _count(repo: Path, rng: str) -> Optional[int]:
    proc = _git(repo, "rev-list", "--count", rng)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _resolve(repo: Path, rev: str) -> Optional[str]:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _conflicts(repo: Path, branch: str, target: str) -> Tuple[Optional[bool], str]:
    """Does merging the two collide? (``None`` = could not ask.)

    ``git merge-tree --write-tree`` (2.38+) answers with its exit code: 0
    clean, 1 conflicted, above that an error. Older git has only the
    three-argument form, which prints conflict hunks and exits 0 either way,
    so there the answer is read out of the output. Both are supported because
    the mesh is not one machine.
    """
    modern = _git(repo, "merge-tree", "--write-tree", branch, target)
    if modern.returncode in (0, 1):
        return bool(modern.returncode), "merge-tree --write-tree"
    base = _git(repo, "merge-base", branch, target)
    if base.returncode != 0:
        return None, "no merge base"
    legacy = _git(repo, "merge-tree", base.stdout.strip(), branch, target)
    if legacy.returncode != 0:
        return None, "merge-tree failed"
    return ("<<<<<<<" in legacy.stdout), "merge-tree (legacy)"


def _preview_covers(repo: Path, ref: str, tip: str, target_tip: str) -> Optional[str]:
    """The preview merge at ``ref`` if it merges exactly this pair, else None.

    Parents are compared as a set of two: which side was merged into which
    does not change what tree was measured, and pinning the order would make
    the gate depend on how the worker happened to spell the merge.
    """
    head = _resolve(repo, ref)
    if head is None:
        return None
    proc = _git(repo, "rev-list", "--parents", "--max-count=1", head)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    parents = proc.stdout.split()[1:]
    if len(parents) == 2 and set(parents) == {tip, target_tip}:
        return head
    return None


def _recipe(branch: str, target: str, ref: str) -> str:
    """The cheaper remedy, spelled out where the verdict is read."""
    return (
        "  re-measure on the merge that will actually happen (the branch does "
        "not move):\n"
        f"    git worktree add --detach <tmp> {target}\n"
        f"    git -C <tmp> merge --no-ff {branch}\n"
        "    <run the targeted tests in <tmp>, report the numbers>\n"
        f"    git update-ref {ref} $(git -C <tmp> rev-parse HEAD)\n"
        f"  or rebase: git rebase {target}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Is this branch ready to be merged, or does it need a rebase "
            "(conflict) or a re-measurement (moved baseline) first?"
        )
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "repository to ask about. Omitted: this session's own checkout as "
            "the daemon records it, falling back to the working directory"
        ),
    )
    parser.add_argument(
        "--branch", default="HEAD", help="the branch asking to land (default: HEAD)"
    )
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "the branch it integrates into. Default: this branch's git "
            f"upstream if one is set, else {DEFAULT_TARGET!r}. A worker "
            "stacked under a middle worker must name its parent's branch "
            "(or set it as the upstream), not master."
        ),
    )
    parser.add_argument(
        "--preview-ref",
        default=None,
        help=(
            f"where a preview merge is left for this gate to find (default: "
            f"{PREVIEW_NS}/<branch>). A merge commit whose two parents are "
            f"this tip and the target's tip answers the moved baseline."
        ),
    )
    parser.add_argument(
        "--max-behind",
        type=int,
        default=None,
        metavar="N",
        help=(
            "escalate a moved baseline to a rebase once the target is more "
            "than N commits ahead of the fork point, clean merge or not. Off "
            "by default: distance alone does not force a rebase."
        ),
    )
    args = parser.parse_args(argv)
    repo, repo_how = _resolve_repo(args.repo)

    tip = _resolve(repo, args.branch)
    if tip is None:
        print(
            f"cannot tell: no commit {args.branch!r} in {repo} "
            f"(not a repository?)",
            file=sys.stderr,
        )
        return CANNOT_TELL

    target, how = (args.target, "named") if args.target else _target(repo, args.branch)
    target_tip = _resolve(repo, target)
    if target_tip is None:
        print(
            f"cannot tell: no branch {target!r} in {repo} ({how}) -- name the "
            f"integration target with --target, or set it as this branch's "
            f"upstream",
            file=sys.stderr,
        )
        return CANNOT_TELL

    branch_name = args.branch if args.branch != "HEAD" else _head_name(repo) or "HEAD"

    # Standing on the integration target is not a landing candidate, and the
    # ancestry shortcut below cannot tell that apart from "branch cut, nothing
    # committed yet": both reach tip == target_tip. The shortcut's own comment
    # says calling that "landed" would be a gate telling a lie, and it is the
    # same lie here with none of the same excuse -- this checkout holds no
    # branch that is asking to land. Measured at run cwd = repository root,
    # HEAD = master, with the worker's actual branch unexamined in its own
    # worktree: exit 0 either way, by two different routes -- "ready: nothing
    # to land" when the target resolved to `master`, and "ready: aligned --
    # target origin/master (upstream) +0 / branch +660" when it resolved
    # through the upstream. Hence _same_branch rather than ==.
    if _same_branch(branch_name, target):
        print(
            f"cannot tell: this checkout ({repo}, {repo_how}) is on "
            f"{branch_name}, which is the integration target itself "
            f"({target}, {how}) -- it is not a landing candidate, so there is "
            f"no readiness to report here. Ask about the candidate branch: "
            f"run this in that branch's worktree, or pass --repo <that "
            f"worktree> / --branch <it>.",
            file=sys.stderr,
        )
        return CANNOT_TELL

    # Ancestry first: after a --no-ff landing the target IS ahead of this tip,
    # and reading that as a moved baseline would wake a worker whose work
    # succeeded and send it to rebase onto a target that already contains it.
    if _git(repo, "merge-base", "--is-ancestor", tip, target_tip).returncode == 0:
        if tip == target_tip:
            # A branch cut and not yet committed on passes ancestry too, and
            # calling that "landed" would be a gate telling a lie -- the thing
            # these scripts exist not to do.
            print(f"ready: nothing to land -- {args.branch} is {target} ({tip[:12]})")
        else:
            print(f"ready: landed -- {target} already contains {tip[:12]}")
        return READY

    behind = _count(repo, f"{tip}..{target_tip}")
    ahead = _count(repo, f"{target_tip}..{tip}")
    if behind is None or ahead is None:
        print(
            f"cannot tell: git could not walk {args.branch}..{target}",
            file=sys.stderr,
        )
        return CANNOT_TELL

    where = f"target {target} ({how}) +{behind} / branch +{ahead}"
    if behind == 0:
        print(f"ready: aligned -- {where}")
        return READY

    conflicted, asked_by = _conflicts(repo, tip, target_tip)
    if conflicted is None:
        print(f"cannot tell: {asked_by} -- {where}", file=sys.stderr)
        return CANNOT_TELL

    ref = args.preview_ref or f"{PREVIEW_NS}/{branch_name}"

    if conflicted:
        print(f"rebase: pre-merge conflicts -- {where} [{asked_by}]")
        print(f"  git rebase {target}")
        return REBASE
    if args.max_behind is not None and behind > args.max_behind:
        print(
            f"rebase: {behind} behind, over --max-behind {args.max_behind} -- "
            f"{where} (the merge itself is clean)"
        )
        print(f"  git rebase {target}")
        return REBASE

    preview = _preview_covers(repo, ref, tip, target_tip)
    if preview:
        print(
            f"ready: re-measured on {preview[:12]} -- {where} [{asked_by}, "
            f"preview {ref}]"
        )
        return READY

    print(f"re-measure: baseline moved, merge is clean -- {where} [{asked_by}]")
    print(
        f"  the targeted numbers were taken on a base {behind} commit(s) "
        f"behind {target}, so they do not describe the tree that will land"
    )
    print(_recipe(branch_name, target, ref))
    return REMEASURE


def _head_name(repo: Path) -> Optional[str]:
    proc = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


if __name__ == "__main__":
    raise SystemExit(main())
