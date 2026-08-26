"""Run this repository's full sweep somewhere else, and gate on the receipt it leaves.

``improv-leader``'s ``sweep`` step judges the batch's merge result with the
whole suite. That run used to *be* the step's ``verify``, which meant the
cflow engine ran it (``cflow/engine.py`` ``_run_verify``) synchronously while
the leader's ``next`` call blocked on it. Three things were wrong with that:

* The leader's own workflow says it does not run sweeps or merges in its
  turn -- and then a single ``verify:`` line did exactly that, invisibly.
  ``improv-worker``'s review step already warned about this shape: "the step
  payload's ``verify`` field means leaving the step *starts a sweep* ... the
  least visible sweep is the least recorded sweep."
* Six sessions each running ``-n 8`` on leaving a step is 48 processes that
  contaminate each other's timings, and nothing in the journal says a sweep
  was even running.
* A verify's result lives only in the run. When the daemon restarts
  mid-sweep -- four spontaneous restarts were observed in one shift
  (``claunch-3k9``) -- the sweep is gone and so is the verdict.

So the run and the verdict are split, and a file on disk joins them:

    subagent:  python tools/sweep.py run   --branch master   # minutes
    the gate:  python tools/sweep.py check --branch master   # milliseconds

``run`` executes the suite and writes a **receipt** naming the commit it
judged, the command in full, and the counts. ``check`` is what the step arms
as its ``verify``: it looks up the receipt for the branch's *current* tip and
passes only if that sweep was green.

Both halves live in one file on purpose. The receipt is a format shared
between a writer and a reader, and a format split across two files drifts.

**Keyed by commit, not by clock.** The receipt's name is the sha it judged,
so there is no "is this recent enough" guess: a receipt either belongs to the
tip being gated or it does not. This is also what survives the restarts
above. ``claunch-bg-sweep-lost-on-restart-hq7`` separates two failure axes,
and the receipt covers both -- if the sweep subagent dies, no receipt is
written and the gate is red (producer); if the *leader* dies, the receipt is
still on disk when it comes back (consumer).

**Keyed by repository, not by working directory.** The identity is the git
*common* dir, which every worktree of one repository shares. That is
deliberate: the leader sweeps in a throwaway worktree (a short path, and one
nobody else has uncommitted files in) and the receipt still answers for
master in the main checkout.

**The tree must actually BE the commit the receipt names.** Two ways it can
fail to be, and both were seen for real:

* *Dirty.* A sweep in a checkout holding somebody else's uncommitted work is
  a verdict about that commit plus whatever was lying around. A leader's
  sweep in the shared checkout collected four foreign tests and reported
  1562/1563 where clean master had 1559; six issues were closed citing the
  contaminated number before another session caught it.
* *The wrong commit entirely.* The suite runs in the working tree while the
  receipt is filed under ``--branch``'s sha, so running it from a feature
  branch files a receipt naming a commit nothing tested. That happened on
  the round that wrote this file: a red receipt against ``master``, from a
  tree that was not master, while master was fine. A clean ``git status``
  does not catch it -- the tree was a perfectly clean checkout of something
  else.

So ``run`` refuses both, and checks HEAD first, because standing on the
wrong commit is the worse of the two.

**A commit is the key; a tree is the verdict.** The sweep judges the working
tree, not the history above it, so a green receipt for tree *T* is a verdict
about every commit whose tree is *T*. ``check`` uses that: if the tip has no
receipt of its own it will accept a green one recorded for a different commit
with the same tree, and says so, naming both shas.

That is not a convenience -- it is what makes ``improv-leader``'s five-minute
integration window cost one sweep instead of two. The leader sweeps the
integration *preview* (the batch's candidates merged onto master in a scratch
branch) before committing to the merge, so a broken combination is caught
while master is still clean. Merging those same candidates in the same order
with ``--no-ff`` then produces a different commit with **byte-identical
content**, and without this the batch would pay for the whole suite twice
over one tree.

Safe here because nothing in this suite reads the repository's own history:
every test that touches git (``tests/test_mergecheck.py``,
``tests/test_worktree.py``, ``tests/test_sweep.py``, ``tests/test_spawn_api.py``,
``tests/test_cflow_layers.py``) builds a throwaway repository under
``tmp_path``. A suite that asserted on ``git log`` would need this turned off.
Only *green* receipts carry over; a red one is a verdict the gate refuses to
launder, and the tip is simply left without a receipt, which is red anyway.

Exit codes match ``tools/deploy_check.py``: 0 = confirmed, 1 = no, 2 = could
not tell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# A gate runs the tree it is checking. This checkout's ``src`` goes in front of
# every installed copy, so the ``claude_launcher`` imports below resolve HERE --
# whatever the worktree's .venv holds (``uv run --no-sync`` promises never to
# populate it) and whatever else on the path answers to the same name.
# Pinned by tests/test_gates_run_this_checkout.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CANNOT_TELL = 2

#: This repository's sweep: the whole suite, no marker filter. The worker's
#: gate is deliberately *not* this (see ``tools/changed_tests.py``) -- only
#: the leader's batch sweep runs everything.
#:
#: ``--no-sync``: uv would otherwise re-resolve the venv before running, and
#: the daemon holds ``.venv/Scripts/claunch.exe`` open, so the replace fails
#: with os error 5. Prepare the tree once with ``uv sync --extra test``; that
#: one sync also installs the xdist ``-n`` needs.
#:
#: ``-n 8`` is measured on this machine, not chosen: serial 532s, n=4 230s,
#: n=8 178s, n=auto (32 here) 184s -- and 32 workers starved the PTY timing
#: tests into intermittent failures. The suite is parallel safe (the heavy
#: e2e tests take OS-assigned ports; conftest gives each test its own tmp
#: home and env).
#:
#: The basetemp is short and per-session because both are hard limits, not
#: taste: xdist inserts ``popen-gwN/`` under it and the transcript tests fold
#: an absolute cwd back into a filename, so the path is 2*basetemp+162 and
#: 260 chars (MAX_PATH) is a FileNotFoundError. Measured: 48 chars passes at
#: 258, 49 fails at 260. And pytest empties its basetemp at startup, so two
#: concurrent runs sharing one path delete each other's temp trees mid-run.
DEFAULT_COMMAND = 'uv run --no-sync pytest tests -q -n 8 --basetemp="C:/t/{session}w"'

#: ``123 passed``, ``2 failed``, ``1 skipped`` ... from pytest's summary line.
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|skipped|error|errors|xfailed|xpassed)")


def _git(repo: Path, *args: str) -> str:
    """``git -C repo args...``, stripped. Raises LookupError on failure."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.strip() or 'no output'}"
        )
    return proc.stdout.strip()


def repo_key(repo: Path) -> str:
    """Stable id for the *repository*, shared by all of its worktrees.

    ``--git-common-dir`` is the one path every worktree of a repository
    agrees on, which is what lets a sweep run in a scratch worktree answer
    for the branch in the main checkout.
    """
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    resolved = str(Path(common).resolve()).lower().replace("\\", "/")
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]


def receipts_dir(repo: Path, override: Optional[Path] = None) -> Path:
    """Where this repository's receipts live -- outside the repository.

    Receipts are evidence about a tree, not part of it: keeping them in the
    worktree would put them in front of every ``git status`` the workflow
    reads, and the dirty-tree rule above would then trip over its own output.
    """
    if override is not None:
        return override / repo_key(repo)
    from claude_launcher import config

    return config.launcher_home() / "sweeps" / repo_key(repo)


def receipt_path(repo: Path, commit: str, override: Optional[Path] = None) -> Path:
    return receipts_dir(repo, override) / f"{commit}.json"


def is_green(receipt: dict) -> bool:
    """A receipt that judged a clean tree and found nothing wrong."""
    counts = receipt.get("counts") or {}
    return (
        not receipt.get("dirty")
        and receipt.get("exit_code") == 0
        and not counts.get("failed")
        and not counts.get("error")
    )


def find_receipt_by_tree(
    repo: Path, tree: str, override: Optional[Path] = None
) -> Optional[tuple]:
    """The newest green receipt recorded for `tree`, whatever commit it named.

    The sweep runs against a working tree, so the sha in the receipt's name is
    only an address -- the thing judged is the content. This is what lets one
    sweep of an integration preview answer for the merge commit that lands the
    same candidates: same tree, different sha.

    Returns ``(path, receipt)`` or ``None``. Unreadable files are skipped
    rather than fatal: this is a fallback, and the caller is already on its
    way to a red gate without it.
    """
    directory = receipts_dir(repo, override)
    if not directory.is_dir():
        return None
    best = None
    for path in sorted(directory.glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if receipt.get("tree") != tree or not is_green(receipt):
            continue
        if best is None or (receipt.get("finished_at") or "") > (
            best[1].get("finished_at") or ""
        ):
            best = (path, receipt)
    return best


def parse_counts(output: str) -> dict:
    """Counts from pytest's summary line -- the last one, when xdist repeats it."""
    counts: dict = {}
    for line in output.splitlines():
        found = _COUNT_RE.findall(line)
        if found and ("passed" in line or "failed" in line or "error" in line):
            counts = {kind.rstrip("s"): int(n) for n, kind in found}
    return counts


def _failure_lines(output: str, limit: int = 40) -> list:
    """The ``FAILED``/``ERROR`` short-summary lines, so a red gate can name names."""
    lines = [
        ln.strip()
        for ln in output.splitlines()
        if ln.startswith("FAILED ") or ln.startswith("ERROR ")
    ]
    return lines[:limit]


def cmd_run(args) -> int:
    repo = args.repo.resolve()
    try:
        commit = _git(repo, "rev-parse", args.branch)
        tree = _git(repo, "rev-parse", args.branch + "^{tree}")
        head = _git(repo, "rev-parse", "HEAD")
        dirty = _git(repo, "status", "--porcelain")
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    # The suite runs in this working tree, but the receipt is filed under
    # --branch's sha. If those are different commits the receipt is a lie in
    # the most useful-looking form: it names a commit nobody tested. This
    # happened on the very round that wrote this file -- a run in a feature
    # worktree filed a red receipt against master, and master was fine.
    #
    # A clean tree is not enough to catch it: `git status` was empty, because
    # the tree was a perfectly clean checkout of a *different* commit. So the
    # identity has to be checked directly, and it is checked before the
    # dirty test because standing on the wrong commit is the worse error.
    if head != commit:
        print(
            f"refusing to sweep: {repo} is at {head[:12]}, but this run would "
            f"file its receipt against {args.branch} = {commit[:12]}. The "
            f"suite runs in the working tree, so the receipt would name a "
            f"commit nothing tested. Check out {args.branch} (a scratch "
            f"worktree detached at it is the cheap way) and run this there.",
            file=sys.stderr,
        )
        return CANNOT_TELL

    if dirty and not args.allow_dirty:
        print(
            f"refusing to sweep: {repo} has {len(dirty.splitlines())} uncommitted "
            f"path(s), so a run here judges {args.branch} plus whatever is lying "
            f"around, not {commit[:12]}. Sweep a clean tree -- a scratch worktree "
            f"at the tip is the cheap way -- or pass --allow-dirty to record an "
            f"explicitly untrusted receipt.\n{dirty}",
            file=sys.stderr,
        )
        return CANNOT_TELL

    session = os.environ.get("CLAUNCH_SESSION", "sweep")
    command = args.command or DEFAULT_COMMAND.format(session=session)

    started = datetime.now(timezone.utc)
    print(f"sweep: {command}\n  repo={repo}\n  {args.branch}={commit}", flush=True)
    proc = subprocess.run(
        command, shell=True, cwd=str(repo), capture_output=True, text=True
    )
    finished = datetime.now(timezone.utc)
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output)

    counts = parse_counts(output)
    receipt = {
        "commit": commit,
        "tree": tree,
        "branch": args.branch,
        "repo": str(repo),
        "command": command,
        "exit_code": proc.returncode,
        "counts": counts,
        "failures": _failure_lines(output),
        "dirty": bool(dirty),
        "session": session,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "seconds": round((finished - started).total_seconds(), 1),
    }
    dest = receipt_path(repo, commit, args.receipts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts"
    print(
        f"receipt: {dest}\n  {summary} in {receipt['seconds']}s, "
        f"exit {proc.returncode}"
    )
    return 0 if proc.returncode == 0 else 1


def cmd_check(args) -> int:
    repo = args.repo.resolve()
    try:
        commit = _git(repo, "rev-parse", args.branch)
        tree = _git(repo, "rev-parse", args.branch + "^{tree}")
        path = receipt_path(repo, commit, args.receipts)
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    stood_in_for = None
    if path.is_file():
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot tell: {path} is unreadable: {exc}", file=sys.stderr)
            return CANNOT_TELL
    else:
        # No receipt under this sha. The sweep judges content, not history, so
        # a green receipt for the same tree is a verdict about this commit --
        # the integration preview's sweep answering for the merge that lands
        # the same candidates is exactly that case, and it is what keeps a
        # batch to one sweep. Nothing is inferred: the sha it was recorded
        # under is printed alongside.
        found = find_receipt_by_tree(repo, tree, args.receipts)
        if found is None:
            print(
                f"no sweep receipt for {args.branch} tip {commit[:12]} at {path}, "
                f"and none green for its tree {tree[:12]} -- spawn a subagent to "
                f"run 'python tools/sweep.py run --branch {args.branch}' in a "
                f"clean tree at that commit, then leave this step again. Do not "
                f"run the suite in this turn.",
                file=sys.stderr,
            )
            return 1
        path, receipt = found
        stood_in_for = receipt.get("commit") or "?"

    if receipt.get("dirty"):
        print(
            f"receipt {path} was recorded with --allow-dirty: it judged "
            f"{commit[:12]} plus uncommitted files, which is not a verdict about "
            f"the commit. Re-sweep a clean tree.",
            file=sys.stderr,
        )
        return 1

    counts = receipt.get("counts") or {}
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts"
    via = (
        ""
        if stood_in_for is None
        else (
            f"\nvia the receipt for {stood_in_for[:12]}: a different "
            f"commit, the same tree {tree[:12]}"
        )
    )
    if receipt.get("exit_code") != 0 or counts.get("failed") or counts.get("error"):
        failures = "\n  ".join(receipt.get("failures") or []) or "(none recorded)"
        print(
            f"sweep of {commit[:12]} was red: {summary}, exit "
            f"{receipt.get('exit_code')}\n  {failures}\n"
            f"command: {receipt.get('command')}",
            file=sys.stderr,
        )
        return 1

    print(
        f"sweep of {args.branch} tip {commit[:12]} green: {summary} "
        f"in {receipt.get('seconds')}s\ncommand: {receipt.get('command')}\n"
        f"swept by {receipt.get('session')} at {receipt.get('finished_at')}"
        f"{via}"
    )
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="sweep", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    for name, help_text in (
        ("run", "run the full suite and write a receipt (for a subagent)"),
        (
            "check",
            "pass only if a green receipt exists for the branch tip (for a gate)",
        ),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", type=Path, default=Path("."))
        p.add_argument("--branch", default="master")
        p.add_argument(
            "--receipts",
            type=Path,
            default=None,
            help="override the receipt root (tests)",
        )
        if name == "run":
            p.add_argument(
                "--command", default=None, help="sweep command (default: this repo's)"
            )
            p.add_argument(
                "--allow-dirty",
                action="store_true",
                help="sweep anyway, and mark the receipt untrusted",
            )

    args = ap.parse_args(argv)
    return cmd_run(args) if args.mode == "run" else cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
