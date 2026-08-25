"""``claunch beads ...`` — the repository's issue board, reached from any worktree.

The board is `beads <https://github.com/steveyegge/beads>`_ driven through its
``br`` binary; this module adds nothing to it but *where it is*. ``br`` finds
its database by looking for ``.beads/*.db`` in the current directory, which
is right for a checkout and wrong for a fleet: a session working in a git
worktree has the tracked ``.beads/issues.jsonl`` there but no database, so
``br`` would quietly initialise a second, empty board in the worktree and
the fleet would end up with one board per checkout. One repository has one
board. It lives at the repository's main checkout, and every call — from
whichever worktree the caller stands in — names it with ``--db``.

That is the whole job here: resolve the repository root through git's
common dir (a worktree's common dir *is* the main checkout's ``.git``),
point ``--db`` at ``<root>/.beads/beads.db``, stamp writes with the calling
session's name as ``--actor`` so the audit trail says which agent did what,
and hand the rest of the argument list to ``br`` untouched. When the
database is missing but the tracked JSONL is there — a fresh clone, or the
main checkout right after the board's first merge — the database is rebuilt
from the JSONL first, so the caller never has to know that step exists.

What this deliberately is not: a wrapper that re-spells ``br``'s commands.
The improv workflows teach ``claunch beads <br arguments>`` and nothing
else, so ``br``'s own ``--help`` stays the reference.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import workspaces

#: The board's directory and files, as ``br init`` lays them out.
BEADS_DIR = ".beads"
DB_NAME = "beads.db"
JSONL_NAME = "issues.jsonl"
CONFIG_NAME = "config.yaml"

#: The session name every claunch-managed session carries; it becomes the
#: ``--actor`` on every ``br`` write so the audit trail names the agent.
SESSION_ENV = "CLAUNCH_SESSION"

#: The binary. ``bd`` is the older Go implementation of the same tracker
#: and reads the same files differently; it is never used here.
BINARY = "br"


class BeadsError(Exception):
    """Raised when the board cannot be reached — no root, no board, no ``br``."""


def repo_root(cwd: Optional[str] = None) -> Optional[Path]:
    """The directory that owns the board for ``cwd``, or ``None``.

    Git first: ``--git-common-dir`` is the one answer that is the same from
    the main checkout and from every worktree cut from it, which is exactly
    the property a single board needs. The registered workspaces are the
    fallback for a directory git does not claim — ``workspaces.owning`` was
    written for the same containment question.
    """
    here = cwd or os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here, capture_output=True, text=True, check=False,
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).parent
    owner = workspaces.owning(here)
    if owner is not None:
        return Path(owner.path)
    return None


def issue_prefix(beads_dir: Path, root: Path) -> str:
    """The prefix issue ids carry, from ``config.yaml`` or the root's name.

    ``br init`` writes the prefix into the database and leaves it as a
    *comment* in ``config.yaml``; this repository uncomments it so a rebuild
    from JSONL can hand the same prefix back to ``br init``. Without it the
    rebuilt board would mint ids under a different prefix than the ones
    already in the JSONL.
    """
    config = beads_dir / CONFIG_NAME
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*issue_prefix\s*:\s*(\S+)\s*$", line)
            if m:
                return m.group(1).strip("'\"")
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    return slug or "issue"


def plan(
    args: List[str],
    root: Path,
    actor: Optional[str],
    db_exists: bool,
    jsonl_exists: bool,
) -> List[List[str]]:
    """The ``br`` invocations to run, in order — pure, so a test can read it.

    Every command names the board with ``--db``. A write gets ``--actor``
    from the session unless the caller set one. ``init`` passes straight
    through (it is how a board is first made). Anything else against a
    missing database rebuilds it from the tracked JSONL when that exists,
    and refuses when nothing is there to rebuild from — a typo'd directory
    must not grow a board of its own.
    """
    beads_dir = root / BEADS_DIR
    db = str(beads_dir / DB_NAME)
    base = [BINARY, "--db", db]
    if actor and "--actor" not in args:
        base += ["--actor", actor]
    if args and args[0] == "init":
        return [base + args]
    if db_exists:
        return [base + args]
    if jsonl_exists:
        prefix = issue_prefix(beads_dir, root)
        return [
            [BINARY, "--db", db, "init", "--prefix", prefix],
            [BINARY, "--db", db, "sync", "--import-only"],
            base + args,
        ]
    raise BeadsError(
        f"no board at {beads_dir} — start one at the repository root with "
        f"'claunch beads init --prefix <name>'"
    )


def run(args: List[str], cwd: Optional[str] = None) -> int:
    """Resolve the board for ``cwd`` and run ``br`` with ``args`` against it."""
    if shutil.which(BINARY) is None:
        raise BeadsError(
            f"'{BINARY}' is not installed — the board needs the beads CLI "
            f"(cargo install beads-rust, or see https://github.com/steveyegge/beads)"
        )
    root = repo_root(cwd)
    if root is None:
        raise BeadsError(
            "not inside a git repository or a registered workspace — "
            "the board belongs to a repository"
        )
    beads_dir = root / BEADS_DIR
    commands = plan(
        list(args), root, os.environ.get(SESSION_ENV) or None,
        db_exists=(beads_dir / DB_NAME).is_file(),
        jsonl_exists=(beads_dir / JSONL_NAME).is_file(),
    )
    code = 0
    for i, cmd in enumerate(commands):
        # Bootstrap steps run quietly; only the caller's own command keeps
        # its output. A failed bootstrap step stops the chain — running the
        # caller's command against a half-built board is worse than no
        # answer.
        last = i == len(commands) - 1
        proc = subprocess.run(
            cmd, cwd=cwd or os.getcwd(), check=False,
            capture_output=not last, text=True,
        )
        code = proc.returncode
        if code != 0:
            if not last:
                detail = (proc.stderr or proc.stdout or "").strip()
                print(
                    f"error: rebuilding the board from {JSONL_NAME} failed "
                    f"at {' '.join(cmd[3:])}: {detail}",
                    file=sys.stderr,
                )
            break
    return code


def _cmd(args: argparse.Namespace) -> int:
    try:
        return run(list(args.args))
    except BeadsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def register(sub) -> None:
    p = sub.add_parser(
        "beads",
        help="the repository's issue board (br), from any worktree",
        description=(
            "Run 'br' against this repository's one board: the database at "
            "<repo root>/.beads/beads.db, found through git's common dir so a "
            "worktree uses the same board as the main checkout. Writes are "
            "stamped --actor $CLAUNCH_SESSION. Every argument after 'beads' "
            "goes to br as-is; 'br --help' lists them."
        ),
    )
    p.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="br subcommand and its arguments (e.g. 'ready --json')",
    )
    p.set_defaults(func=_cmd)
