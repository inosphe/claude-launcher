"""The git and board reads behind a run's landing queue (``landing_queue:``).

A queue entry is a child's landing request: the issue it is for (the key),
the branch and the frozen tip the child asked to land, who asked and when,
and where the request stands. The entries live in the parent's run state
(see :mod:`cflow.engine`); this module is the part that looks outside the
run — at git, for whether a tip landed, and at the issue board, for whether
the board still agrees with the queue.

Everything here is a read, and every read answers ``None`` when it could not
be made instead of guessing: a queue that marks an entry landed because git
could not be asked has lost exactly the request it exists to keep.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import model

#: How long one git or board read may take. These run under the run's lock
#: (a round's end resets the queue inside the move), so a hung read must not
#: hold the run for longer than an agent would wait on a tool.
READ_TIMEOUT = 10.0

#: Board statuses a queued request is still consistent with.
_BOARD_ACTIVE = ("in_review", "in_progress")


def _git(repo: str, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=READ_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def head(repo: str) -> Optional[str]:
    """The commit ``repo`` has checked out, or None."""
    proc = _git(repo, "rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def branch(repo: str) -> Optional[str]:
    """The branch ``repo`` has checked out, or None (detached, unreadable)."""
    proc = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def landed(repo: str, tip: str, target: str) -> Optional[bool]:
    """Whether ``tip`` is an ancestor of ``target`` in ``repo``.

    ``None`` when git could not answer (no such commit here, no such target,
    no git) -- which is not "no": an entry stays where it was.
    """
    if not tip or not target:
        return None
    proc = _git(repo, "merge-base", "--is-ancestor", tip, target)
    if proc is None:
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _board_rows(repo: str, args: Sequence[str]) -> Optional[List[dict]]:
    """Run one ``br`` read against the board of ``repo``; None if it failed."""
    from .. import cli_beads

    if shutil.which(cli_beads.BINARY) is None:
        return None
    # The same resolution `claunch beads` uses: a registered workspace keeps
    # its board wherever the operator set it, not necessarily under the root.
    try:
        ref = cli_beads.resolve(repo)
    except Exception:
        return None
    if ref is None or not ref.exists():
        return None
    # br 0.7 refuses a filter on a status the board's policy does not declare
    # while no issue is in it -- in_review_of's own `--status in_review` on a
    # board with nothing in review -- which would read here as "the board
    # could not answer" rather than "nothing". See cli_beads.CUSTOM_STATUSES.
    try:
        cli_beads.ensure_policy(Path(ref.root) / cli_beads.BEADS_DIR)
    except OSError:
        pass
    try:
        proc = subprocess.run(
            [cli_beads.BINARY, "--db", str(ref.db), *args, "--json"],
            cwd=repo, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=READ_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "null")
    except ValueError:
        return None
    rows = data.get("issues") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict)]


def in_review_of(repo: str, session: str) -> Optional[List[str]]:
    """The issues ``session`` holds in review: what its landing request is for.

    Read from the board and not from the worker's words -- the request's
    marker comment goes on exactly these (a batch round moves every carried
    issue to ``in_review``), so this is the list the parent judges.
    """
    rows = _board_rows(
        repo, ["list", "--assignee", session, "--status", "in_review", "--limit", "0"]
    )
    if rows is None:
        return None
    return sorted(str(r["id"]) for r in rows if r.get("id"))


def board_statuses(repo: str, issues: Iterable[str]) -> Optional[Dict[str, str]]:
    """The board status of each of ``issues`` (absent ones left out), or None."""
    wanted = {str(i) for i in issues if i}
    if not wanted:
        return {}
    rows = _board_rows(repo, ["list", "--all", "--limit", "0"])
    if rows is None:
        return None
    return {
        str(r["id"]): str(r.get("status") or "")
        for r in rows
        if str(r.get("id")) in wanted
    }


def board_warnings(entries: Sequence[dict], statuses: Dict[str, str]) -> List[str]:
    """Where the board and the queue disagree, one line per entry.

    The queue holds the landing procedure's state and the board holds the
    work's, so neither overrides the other; a disagreement is said, not
    repaired. Three shapes are worth a line:

    - an entry still waiting to land whose issue the board has closed (or
      sent back to open): landed or dropped somewhere the queue did not see;
    - an entry marked landed whose issue is still in review: the child has
      not closed it yet, which its wrapup should;
    - an entry the board does not have at all.
    """
    out: List[str] = []
    for entry in entries:
        issue = str(entry.get("issue") or "")
        status = entry.get("status")
        board = statuses.get(issue)
        if board is None:
            out.append(f"{issue}: queued ({status}) but not on the board")
            continue
        if status in model.LANDING_SETTLED:
            if status == model.LANDING_LANDED and board == "in_review":
                out.append(
                    f"{issue}: landed, but the board still has it in_review "
                    f"(its assignee closes it)"
                )
            continue
        if board not in _BOARD_ACTIVE:
            out.append(
                f"{issue}: queued ({status}) but the board has it {board}"
            )
    return out
