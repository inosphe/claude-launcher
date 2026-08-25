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

**A dirty tree cannot produce a receipt.** A sweep in a checkout holding
somebody else's uncommitted work is not a verdict about the commit -- it is a
verdict about that commit plus whatever was lying around. This is not
hypothetical: a leader's sweep in the shared checkout collected four foreign
tests and reported 1562/1563 where clean master had 1559, and six issues were
closed citing the contaminated number before another session caught it.
``run`` refuses to write a receipt when ``git status --porcelain`` is
non-empty, and records the fact either way.

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
        dirty = _git(repo, "status", "--porcelain")
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
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
        path = receipt_path(repo, commit, args.receipts)
    except LookupError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    if not path.is_file():
        print(
            f"no sweep receipt for {args.branch} tip {commit[:12]} at {path} -- "
            f"spawn a subagent to run 'python tools/sweep.py run --branch "
            f"{args.branch}' in a clean tree at that commit, then leave this step "
            f"again. Do not run the suite in this turn.",
            file=sys.stderr,
        )
        return 1

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot tell: {path} is unreadable: {exc}", file=sys.stderr)
        return CANNOT_TELL

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
