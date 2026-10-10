"""Where a session is actually working, when that is not where it was started.

The daemon records a session's directory once, at creation (``sdef.cwd``), and
only an operator ``migrate`` changes it. A session started at a repository's
main checkout that then moves into a worktree -- Claude Code's own worktree
tool, or a ``git worktree add`` the workflow told it to make -- keeps that
record pointing at the main checkout for the rest of its life. Everything that
asks the daemon "where does this session stand" then measures the main
checkout, which is on the integration branch:

* the ``enqueue-landing`` push recorded ``branch=master`` and master's tip as
  the session's landing request, and the leader's queue marked it landed 13
  seconds later because that tip was already in master (claunch-4zc51.1.2);
* ``tools/landed_check.py`` and ``tools/merge_ready.py`` resolve the session's
  checkout through :func:`claude_launcher.cflow.checkout.own_checkout` and
  answered "cannot tell: this checkout is on master, the integration target
  itself" on every visit (claunch-mesh-drop-batch-to-8lqg1.1).

The fact that is missing is not a guess: Claude writes the working directory
into every transcript entry (``"cwd"``), and that value follows the session
into a worktree. Measured on the session that hit the defect: its first entry
carries the main checkout, every entry after the move carries the worktree.
So the newest entry that names a directory is where the session works now.

The answer is taken only when it is a checkout of the *same repository* as the
recorded directory (equal ``git rev-parse --git-common-dir``). A session that
``cd``-s into another repository to read something has not moved its work
there, and reading it as if it had would point every gate at a repository its
round never touched. Anything that cannot be measured -- not a Claude session,
no transcript yet, a directory that no longer exists, git not answering --
answers ``None``, which leaves the caller on the recorded directory exactly as
before this module existed.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..cflow import landing as cflow_landing
from . import ctxsize
from .harness import CLAUDE_HARNESS

#: The field the session list carries the answer in. Absent when the session
#: works where it was started (or nothing could be measured), so a reader that
#: falls back to ``cwd`` needs no special case.
FIELD = "work_cwd"

#: path -> (mtime, size, newest cwd). Same identity rule as ``ctxsize._reads``:
#: an unchanged transcript costs one stat.
_reads: Dict[str, Tuple[float, int, Optional[str]]] = {}

#: normalized directory -> (common dir or None, when it was asked). A
#: directory's repository does not change, but a failed answer (git busy, a
#: worktree mid-creation) must expire.
_repos: Dict[str, Tuple[Optional[str], float]] = {}
REPO_MISS_TTL = 30.0


def _cwd_of(entry: dict) -> Optional[str]:
    """The working directory one Claude transcript entry records, or None.
    Sidechain entries are a subagent's and say nothing about the session."""
    if not isinstance(entry, dict) or entry.get("isSidechain"):
        return None
    value = entry.get("cwd")
    return value if isinstance(value, str) and value.strip() else None


def last_cwd(path: Optional[Path]) -> Optional[str]:
    """The newest ``cwd`` recorded in the transcript at ``path``, or None."""
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    hit = _reads.get(key)
    if hit is not None and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return hit[2]
    where = ctxsize.read_tail(path, _cwd_of)
    _reads[key] = (stat.st_mtime, stat.st_size, where)
    return where


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _repo(path: str) -> Optional[str]:
    key = _norm(path)
    hit = _repos.get(key)
    now = time.monotonic()
    if hit is not None and (hit[0] is not None or now - hit[1] < REPO_MISS_TTL):
        return hit[0]
    found = cflow_landing.common_dir(path)
    _repos[key] = (found, now)
    return found


def resolve(home: str, where: Optional[str]) -> Optional[str]:
    """``where`` when it is a different checkout of ``home``'s repository,
    else None. ``home`` is the recorded directory, ``where`` the measured one."""
    if not home or not where:
        return None
    if _norm(home) == _norm(where):
        return None
    if not os.path.isdir(where):
        return None
    mine = _repo(home)
    if mine is None or _repo(where) != mine:
        return None
    return os.path.abspath(where)


def of(sdef: Any) -> Optional[str]:
    """Where the session with definition ``sdef`` works, when that differs
    from ``sdef.cwd``; None otherwise (see the module docstring)."""
    if sdef is None:
        return None
    if str(getattr(sdef, "harness", None) or CLAUDE_HARNESS) != CLAUDE_HARNESS:
        return None
    home = str(getattr(sdef, "cwd", "") or "")
    try:
        where = last_cwd(ctxsize.transcript_of(sdef))
    except Exception:  # noqa: BLE001 - a lookup never fails its caller
        return None
    return resolve(home, where)


def of_session(manager: Any, name: Optional[str]) -> Optional[str]:
    """:func:`of` for the session called ``name``, alive or not; None when the
    manager does not know it."""
    if not name:
        return None
    try:
        session = manager.get(name)
    except Exception:  # noqa: BLE001
        return None
    return of(getattr(session, "sdef", None))


def attach(info: dict, sdef: Any) -> dict:
    """Add :data:`FIELD` to a session row when the session works elsewhere."""
    where = of(sdef)
    if where:
        info[FIELD] = where
    return info
