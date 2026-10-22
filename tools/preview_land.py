"""Leave the preview merge of a landing candidate where ``merge_ready`` finds it.

``tools/merge_ready.py`` documents the recipe a preview is made by: merge the
candidate onto the target in a scratch worktree, run the changed tests on
*that* tree, and keep the merge commit as ``refs/claunch/preview/<branch>``.
The leader repeated those five commands by hand, from a copy of a script kept
in its scratch directory, once per candidate. This is that recipe as one
command, so the leader can start it while it would otherwise wait (the restart
of the live daemon, the batch before the next) and reuse the ref afterwards.

    uv run --no-sync python tools/preview_land.py --branch <branch> [--base master]

``--base`` is the commit the candidate is merged onto. Default: the current
tip of ``master``. Giving the target as it stands *before* another candidate
lands is what makes a second preview reusable afterwards: when the target has
moved by a change that touches other paths, ``merge_ready.py
--allow-target-advance`` accepts the older preview without a re-measurement.

The ref is written only when the measurement passed, so a ref that exists is
a green preview of exactly two parents, the candidate's tip and ``--base``.
The scratch worktree is removed on every path. Exit codes: ``0`` preview left,
``1`` the measurement failed (no ref), ``2`` could not run, ``3`` the merge
conflicts (no ref).

The measurement is ``uv sync`` plus ``tools/changed_tests.py`` run on the
scratch worktree, both halves with ``--repo``/``--directory`` for the reason
``merge_ready.py`` gives (claunch-873ys). ``--measure-cmd`` replaces it with one
shell command run in the worktree.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

PREVIEW_NS = "refs/claunch/preview"

OK, MEASURE_FAILED, CANNOT_RUN, CONFLICT = 0, 1, 2, 3


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _rev(repo: str, ref: str) -> Optional[str]:
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return done.stdout.strip() if done.returncode == 0 else None


def _measure(wt: str, base_tip: str, custom: Optional[str]) -> int:
    if custom:
        return subprocess.run(custom, shell=True, cwd=wt).returncode
    steps: List[List[str]] = [
        ["uv", "sync", "--extra", "test", "--directory", wt],
        ["uv", "run", "--directory", wt, "--no-sync", "python", "tools/changed_tests.py",
         "--repo", wt, "--base", base_tip],
    ]
    for step in steps:
        code = subprocess.run(step).returncode
        if code != 0:
            return code
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="a checkout of the repository (default: cwd)")
    ap.add_argument("--branch", required=True, help="the candidate, as merge_ready is given it")
    ap.add_argument("--base", default="master", help="what to merge onto (default: master)")
    ap.add_argument("--preview-ref", default=None,
                    help=f"where to leave the merge (default: {PREVIEW_NS}/<branch>)")
    ap.add_argument("--scratch", default=None,
                    help="directory for the worktree (default: $CLAUNCH_SCRATCH)")
    ap.add_argument("--measure-cmd", default=None,
                    help="shell command run in the worktree instead of the changed-tests measurement")
    args = ap.parse_args(argv)

    top = _git(args.repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        print(f"cannot run: {args.repo} is not a git checkout", file=sys.stderr)
        return CANNOT_RUN
    repo = top.stdout.strip()
    scratch = args.scratch or os.environ.get("CLAUNCH_SCRATCH")
    if not scratch:
        print("cannot run: no --scratch and CLAUNCH_SCRATCH is not set", file=sys.stderr)
        return CANNOT_RUN
    tip, base_tip = _rev(repo, args.branch), _rev(repo, args.base)
    if not tip or not base_tip:
        print(f"cannot run: cannot resolve {args.branch if not tip else args.base}", file=sys.stderr)
        return CANNOT_RUN
    ref = args.preview_ref or f"{PREVIEW_NS}/{args.branch}"

    Path(scratch).mkdir(parents=True, exist_ok=True)
    wt = tempfile.mkdtemp(prefix="preview-", dir=scratch)
    try:
        added = _git(repo, "worktree", "add", "--detach", wt, base_tip)
        if added.returncode != 0:
            print(f"cannot run: git worktree add: {added.stderr.strip()}", file=sys.stderr)
            return CANNOT_RUN
        merged = _git(wt, "-c", "user.name=preview-land", "-c", "user.email=preview@claunch.invalid",
                      "merge", "--no-ff", "-m", f"preview of {args.branch}", tip)
        if merged.returncode != 0:
            _git(wt, "merge", "--abort")
            print(f"conflict: {args.branch} does not merge onto {args.base} ({base_tip[:12]})")
            return CONFLICT
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        code = _measure(wt, base_tip, args.measure_cmd)
        if code != 0:
            print(f"measure failed: exit {code} on the merge of {args.branch} onto "
                  f"{args.base} ({base_tip[:12]}); no preview ref left")
            return MEASURE_FAILED
        updated = _git(repo, "update-ref", ref, head)
        if updated.returncode != 0:
            print(f"cannot run: update-ref {ref}: {updated.stderr.strip()}", file=sys.stderr)
            return CANNOT_RUN
        print(f"preview: {ref} -> {head[:12]} (tip {tip[:12]} + {args.base} {base_tip[:12]})")
        return OK
    finally:
        _git(repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        _git(repo, "worktree", "prune")


if __name__ == "__main__":
    sys.exit(main())
