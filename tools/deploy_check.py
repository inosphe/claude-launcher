"""Did the live daemon actually restart onto the merge it is supposed to serve?

``improv-leader``'s ``reflect`` step ends a round by restarting the daemon so
the merge it just made is what users get. Until this check existed the step
was prose only: nothing stopped the leader from filing "restarted, serving"
while the daemon kept running the pre-merge code, and nothing told the leader
a restart had happened either — the run just repeated its 300-second reminder
until somebody thought to poke ``claunch daemon status`` by hand.

The fact is already on disk. ``daemon/runtime_state.py`` writes ``daemon.json``
on every boot with ``started_at`` (UTC, second precision), and git knows when
the branch tip was committed. If the daemon has been up since *before* that
commit, it cannot be serving it. That is the whole test:

    started_at > committer time of <branch> tip

Strictly greater, and a tie fails: at second precision the two orders are
indistinguishable inside one second, and a gate that guesses in the direction
of "probably fine" is not a gate. The escape from a tie is another restart,
which is exactly the thing the step is asking for anyway.

The check does not prove the *new code* is serving — a restart that picked up
a stale install would pass here. That half stays with the step's ``done_when``
(confirm one behaviour of the merge in the running server). What this closes
is the cheaper failure: no restart at all.

Not merged into ``claunch daemon status``: that command reports on whatever
daemon is running, while this asks a question about one repository's history.
Exit code 0 = restarted after the tip, 1 = not, 2 = could not tell.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

CANNOT_TELL = 2


def _git_committed_at(repo: Path, ref: str) -> datetime:
    """When ``ref``'s tip was committed, as an aware datetime."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%cI", ref],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise LookupError(
            f"git could not read {ref!r} in {repo}: "
            f"{proc.stderr.strip() or 'no such ref'}"
        )
    stamp = proc.stdout.strip()
    if not stamp:
        raise LookupError(f"{ref!r} has no commits in {repo}")
    return datetime.fromisoformat(stamp)


def _daemon_doc(explicit: Optional[Path]) -> tuple[Path, dict]:
    if explicit is not None:
        path = explicit
    else:
        from claude_launcher.daemon import paths

        path = paths.daemon_json()
    if not path.is_file():
        raise LookupError(
            f"no daemon.json at {path} -- nothing is serving, so nothing "
            f"restarted onto this merge"
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LookupError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or "started_at" not in doc:
        raise LookupError(f"{path} carries no started_at")
    return path, doc


def _alive(pid: object) -> Optional[bool]:
    """Whether the pid in daemon.json is a live process (None = cannot tell)."""
    if not isinstance(pid, int):
        return None
    try:
        from claude_launcher import daemon_client
    except ImportError:  # pragma: no cover - only outside the venv
        return None
    return daemon_client.process_alive(pid)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="deploy_check",
        description="fail unless the live daemon restarted after <branch>'s tip",
    )
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--branch", default="master")
    ap.add_argument(
        "--daemon-json",
        type=Path,
        default=None,
        help="override the daemon.json location (tests, second instances)",
    )
    args = ap.parse_args(argv)

    try:
        committed = _git_committed_at(args.repo, args.branch)
        path, doc = _daemon_doc(args.daemon_json)
        started = datetime.fromisoformat(str(doc["started_at"]))
    except (LookupError, ValueError) as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return CANNOT_TELL

    if _alive(doc.get("pid")) is False:
        print(
            f"not restarted: {path} names pid {doc.get('pid')}, which is gone -- "
            f"the file is a leftover of a daemon that is no longer serving",
            file=sys.stderr,
        )
        return 1

    if started > committed:
        print(
            f"restarted {started.isoformat()} > {args.branch} tip "
            f"{committed.isoformat()} -- the running daemon booted after the merge"
        )
        return 0

    print(
        f"not restarted: daemon up since {started.isoformat()}, which is not "
        f"after {args.branch}'s tip {committed.isoformat()} -- the live server "
        f"is still serving pre-merge code. Restart it and run this again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
