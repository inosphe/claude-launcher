"""``claunch commits`` — the commits a session made, as the daemon sees them.

The same read the session detail panel does (``/api/sessions/<name>/meta``
carries it under ``commits``), printed in a terminal. Both call
:func:`claude_launcher.session_commits.for_session`, so what a worker checks
before it reports and what a human reads on the dashboard are the same list.

``--session`` defaults to ``$CLAUNCH_SESSION`` and the directory to the shell's,
so inside a claunch session the whole command is ``claunch commits``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import session_commits


def _cmd(args: argparse.Namespace) -> int:
    name = (args.session or os.environ.get("CLAUNCH_SESSION") or "").strip()
    if not name:
        print(
            "error: no session: run this inside a claunch session "
            "(CLAUNCH_SESSION is set there) or name one with --session <name>",
            file=sys.stderr,
        )
        return 2
    cwd = Path(args.repo or Path.cwd())
    found = session_commits.for_session(cwd, name, limit=args.limit)
    if args.json:
        print(json.dumps(session_commits.summary(found), indent=2, ensure_ascii=False))
        return 0
    if not found:
        # Not an error: a round that has not committed yet is the normal
        # state of this command for most of a round's life.
        print(f"{name}: no stamped commits in {cwd}")
        return 0
    for c in found:
        where = f"  [{c['worktree']}]" if c.get("worktree") else ""
        print(f"{c['short']}  {c['committed_at']}  {c['subject']}{where}")
    return 0


def register(sub) -> None:
    p = sub.add_parser(
        "commits",
        help="the commits a session made here (from its commit-stamp trailers)",
        description=(
            "Reads the Claunch-Session trailers that the commit-stamp skill "
            "writes, so nothing has to be recorded twice and the list cannot "
            "disagree with git. The daemon serves the same read inside "
            "/api/sessions/<name>/meta."
        ),
    )
    p.add_argument("--session", help="session name (default: $CLAUNCH_SESSION)")
    p.add_argument("--repo", help="repository/worktree to read (default: cwd)")
    p.add_argument(
        "--limit",
        type=int,
        default=session_commits.DEFAULT_LIMIT,
        help=f"most recent N commits (default: {session_commits.DEFAULT_LIMIT})",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_cmd)
