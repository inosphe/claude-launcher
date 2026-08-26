"""Did this worker's branch actually land, or is it only *asked* to land?

``improv-worker`` used to end a round the moment the request was filed.
``integration-request`` froze the branch, sent the parent a nudge, and went
straight to ``wrapup`` -- which then told the session to kill itself. The
round was ``done`` while the branch was still sitting in somebody else's
queue, and ``wrapup`` said so out loud: "착지 확인은 이 회차의 완료 조건이
아니다". So a round could be filed as finished and the work never reach the
integration target at all -- a rejected request, a rebase asked for and never
answered, a leader that swept the batch and dropped this one branch. Nothing
noticed, because the session that would have noticed was already dead.

This is the machine half of closing that hole. The fact is already in git.

"Some other branch contains my tip" is the obvious question and it is the
wrong one -- measured, on a live mesh: worker s133 froze ``1a7b9dd`` with
nothing landed, and its own child s140 was stacked on top of it, so a
containment test answered "landed". A branch that *descends from* this tip has
integrated nothing; in a nested arrangement, children stacked on a parent are
the normal shape rather than an edge case, so that reading is false green
exactly where the workflow is most complicated.

The contract this workflow states everywhere is a ``--no-ff`` merge, and that
is what makes the question exact: a merge took this branch in iff some
**merge commit** on another branch lists this tip among its parents. A child
stacked on the tip adds an ordinary single-parent commit and is excluded.

    git rev-list --parents --merges <my tip>..<B>   ->  a parent == <my tip>?

Exit code 0 = landed, 1 = not yet, 2 = could not tell.

``--target`` narrows the question to one branch and asks plain ancestry
instead, which is what a nested worker wants (its target is the parent's
branch, not master) and what answers a fast-forward landing. Left off, the
merge-parent scan runs over every other local branch: the check has to be
correct from a static ``verify`` string that cannot know whether this
particular run reports to the leader or to a middle worker.

What this does NOT prove, kept honest in the same spirit as
``deploy_check.py``: that what landed is what was *reviewed*. A leader who
merges and then reverts still leaves the merge in history. A squash or a
rewrite lands the same change under a different sha and reads here as "not
yet" -- the worker then has a rewritten history to explain, which is exactly
the case the step's ``done_when`` asks it to spell out by hand. A landing
fast-forwarded instead of merged (which the contract forbids) leaves no merge
commit and also reads as "not yet"; ``--target`` is the escape. What this
closes is the cheaper failure, the one that kept happening: the round ends
with the branch nowhere.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# Every gate script under tools/ puts this checkout first, and this one
# keeps the rule even though it imports nothing from the package: it asks
# git and nothing else. The rule is worth more without an exemption list --
# the moment this file grows an import, the line that would have made it
# resolve against the tree being checked is already here rather than
# remembered. tests/test_gates_run_this_checkout.py holds all five to it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CANNOT_TELL = 2


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _own_branch(repo: Path) -> Optional[str]:
    """The branch HEAD is on, or None when HEAD is detached."""
    proc = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _containing_branches(repo: Path, tip: str) -> List[str]:
    """Every local branch whose history includes ``tip``."""
    proc = _git(
        repo, "branch", "--contains", tip, "--format=%(refname:short)"
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git could not list branches containing {tip!r}: "
            f"{proc.stderr.strip() or 'unknown error'}"
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _merged_by(repo: Path, tip: str, branch: str) -> bool:
    """Does ``branch`` carry a merge commit that took ``tip`` in?

    The range is ``tip..branch``, so this reads only what ``branch`` added
    beyond this tip -- and a merge that took us in is in there by definition.
    A child branch stacked on ``tip`` adds single-parent commits, which
    ``--merges`` drops, which is the whole point of asking it this way.
    """
    proc = _git(
        repo, "rev-list", "--parents", "--merges", f"{tip}..{branch}"
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git could not walk {tip}..{branch}: "
            f"{proc.stderr.strip() or 'unknown error'}"
        )
    for line in proc.stdout.splitlines():
        # "<commit> <parent1> <parent2> ..." -- we are a parent, not the commit
        if tip in line.split()[1:]:
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", default=".", help="repository to ask about (default: cwd)"
    )
    parser.add_argument(
        "--target",
        default=None,
        help=(
            "the integration target this branch was requested into "
            "(a nested worker's is its parent's branch, not master). "
            "Omitted: any branch but this one counts."
        ),
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()

    tip_proc = _git(repo, "rev-parse", "HEAD")
    if tip_proc.returncode != 0:
        print(
            f"could not read HEAD in {repo}: "
            f"{tip_proc.stderr.strip() or 'not a repository?'}",
            file=sys.stderr,
        )
        return CANNOT_TELL
    tip = tip_proc.stdout.strip()
    mine = _own_branch(repo)

    if args.target:
        probe = _git(
            repo, "merge-base", "--is-ancestor", tip, args.target
        )
        if probe.returncode == 0:
            print(f"landed: {args.target} contains {tip[:12]}")
            return 0
        # An unreadable target is not the answer "not yet".
        if _git(repo, "rev-parse", "--verify", args.target).returncode != 0:
            print(
                f"no such branch {args.target!r} in {repo} — cannot tell "
                "whether it landed",
                file=sys.stderr,
            )
            return CANNOT_TELL
        print(f"not yet: {args.target} does not contain {tip[:12]}")
        return 1

    try:
        candidates = [b for b in _containing_branches(repo, tip) if b != mine]
        holders = [b for b in candidates if _merged_by(repo, tip, b)]
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return CANNOT_TELL

    if holders:
        # Every branch rebased onto the landing carries that merge too, so the
        # list can run long; the fact is the first one, not the census.
        shown = ", ".join(holders[:3])
        rest = f" (+{len(holders) - 3} more)" if len(holders) > 3 else ""
        print(f"landed: {tip[:12]} was merged into {shown}{rest}")
        return 0
    where = f"branch {mine}" if mine else "detached HEAD"
    detail = ""
    if candidates:
        # Worth naming: these are the stacked children that made the naive
        # containment test answer "landed" when nothing had.
        detail = (
            f" ({len(candidates)} branch(es) descend from it without merging "
            f"it: {', '.join(candidates)})"
        )
    print(f"not yet: no merge on any branch but {where} took {tip[:12]} in{detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
