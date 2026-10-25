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
from typing import Any, Dict, List, Optional, Tuple

from ..cflow import landing as cflow_landing
from . import ctxsize
from .harness import CLAUDE_HARNESS

#: Branches a round is never worked on: the integration branch itself, and
#: the trunks generally. A checkout on one of them is not a round's work even
#: when its name matches the session's -- ``landing_refusal`` refuses a
#: request measured there anyway, so taking it would only move the failure.
TRUNKS = ("master", "main")

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

#: common dir -> (checkouts, when). A repository's worktree list changes when
#: someone cuts or removes a checkout; a few seconds of staleness costs a
#: branch-section miss, never a wrong checkout, because the caller re-reads
#: the branch's HEAD before using the answer.
_checkout_cache: Dict[str, Tuple[List[Tuple[str, Optional[str]]], float]] = {}
CHECKOUTS_TTL = 5.0


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


def _git(cwd: str, *args: str):
    """One read-only git call, or None when git could not be run."""
    return cflow_landing._git(cwd, *args)


def _checkouts(home: str, *, include_main: bool = False) -> List[Tuple[str, Optional[str]]]:
    """Every linked checkout of ``home``'s repository as ``(path, branch)``.

    The main checkout is left out unless ``include_main``: it is where the
    session was started, and ``resolve`` already refuses it (same directory,
    or the trunk branch). When included it is first, which is the order
    ``git worktree list`` gives.
    ``branch`` is None for a detached checkout, which is never a round's work.

    Cached by repository for :data:`CHECKOUTS_TTL`, and the cache holds the
    repository's WHOLE list rather than this caller's slice: :func:`attach`
    asks once per row of a session list, and several of those sessions can be
    in checkouts of one repository. Caching a slice keyed by the repository
    would hand the second session the first one's exclusions.
    """
    mine = _repo(home)
    if mine is None:
        return []
    now = time.monotonic()
    hit = _checkout_cache.get(mine)
    if hit is None or now - hit[1] >= CHECKOUTS_TTL:
        done = _git(home, "worktree", "list", "--porcelain")
        out: List[Tuple[str, Optional[str]]] = []
        if done is not None and done.returncode == 0:
            path, branch = "", None
            for line in (done.stdout or "").splitlines() + [""]:
                if not line.strip():
                    if path:
                        out.append((path, branch))
                    path, branch = "", None
                    continue
                key, _, value = line.partition(" ")
                if key == "worktree":
                    path = value.strip()
                elif key == "branch":
                    # refs/heads/<name> -> <name>
                    branch = value.strip().removeprefix("refs/heads/") or None
        hit = (out, now)
        _checkout_cache[mine] = hit
    if include_main:
        return list(hit[0])
    return [(p, b) for p, b in hit[0] if _norm(p) != _norm(home)]


def _own_branch(branch: Optional[str], session: str) -> bool:
    """Whether ``branch`` is one this session cut for a round.

    ``branch-setup`` names every round branch ``<session>-<gist>``, so the
    session's own name followed by a separator is the mark. The separator is
    what makes it the session's: without it a session ``s2`` would claim the
    round branch of ``s2-something`` only, but a session ``s2-r`` would take
    ``s2-round`` by plain string prefix, which is a different session's round.
    """
    if not branch or not session:
        return False
    if branch in TRUNKS:
        return False
    return branch == session or branch.startswith(session + "-")


def by_branch(home: str, session: str) -> Optional[str]:
    """The checkout of ``home``'s repository that holds a round branch this
    session cut, or None.

    This is the answer for the move the transcript does not record. A worker
    spawned into its parent's checkout inherits that directory as its
    ``cwd``, and if it reaches its own worktree with ``git -C`` rather than
    by standing there, no transcript entry says so -- :func:`resolve` then
    has nothing to accept and the caller measures the inherited directory,
    which is the parent's round (claunch-1o6cs: the landing request carried
    the parent's branch and tip, and the leader held the merge).

    An inference, and it is only used where the transcript had no answer:
    a recorded ``cd`` is what the session stood in, this is a read of what it
    cut. Several candidates are possible (a session's earlier rounds leave
    their checkouts behind); the one whose HEAD is newest is the round being
    worked, which is the one a landing request is about. Ties keep
    ``worktree list`` order, which is the order the checkouts were made in.
    """
    if not home or not session:
        return None
    mine = _repo(home)
    if mine is None:
        return None
    # The directory the session was started in, when it is already standing on
    # one of its own round branches, IS where it works -- searching further
    # would answer with an older round's checkout left behind by a previous
    # round of this same session.
    if _own_branch(cflow_landing.branch(home), session):
        return None
    # Only a session that inherited ANOTHER checkout is searched for. A session
    # started at the repository's main checkout works there until a recorded
    # move says otherwise, and a round branch of the same name lying in a
    # worktree is then either an earlier round's leftover or another session
    # recycled onto this name -- `worktree.child_name` records that names are
    # reused after `clear-sessions`, so adopting one would wake a new worker on
    # someone else's branch. The inherited case is the one with no answer
    # otherwise: the parent's checkout is not a trunk, so `resolve` refuses it
    # and the caller would measure the parent's round.
    listed = _checkouts(home, include_main=True)
    if not listed:
        return None
    main_checkout = listed[0][0]
    if _norm(home) == _norm(main_checkout):
        return None
    best: Optional[Tuple[float, str]] = None
    for path, branch in _checkouts(home):
        if not _own_branch(branch, session):
            continue
        if _repo(path) != mine:
            continue
        head = cflow_landing.head(path)
        if not head:
            continue
        when = _git(path, "log", "-1", "--format=%ct", head)
        try:
            stamp = float((when.stdout or "").strip()) if when is not None and when.returncode == 0 else 0.0
        except ValueError:
            stamp = 0.0
        if best is None or stamp >= best[0]:
            best = (stamp, path)
    return os.path.abspath(best[1]) if best else None


def of(sdef: Any) -> Optional[str]:
    """Where the session with definition ``sdef`` works, when that differs
    from ``sdef.cwd``; None otherwise (see the module docstring).

    Two reads, in that order: the newest ``cwd`` the transcript records (the
    move that was made by standing somewhere), and -- only when that answers
    nothing -- the checkout holding a round branch named for this session
    (:func:`by_branch`). The second is what a spawned worker needs: it is
    handed its parent's directory and its own work is in a checkout the
    transcript never names.
    """
    if sdef is None:
        return None
    if str(getattr(sdef, "harness", None) or CLAUDE_HARNESS) != CLAUDE_HARNESS:
        return None
    home = str(getattr(sdef, "cwd", "") or "")
    try:
        where = last_cwd(ctxsize.transcript_of(sdef))
    except Exception:  # noqa: BLE001 - a lookup never fails its caller
        return None
    moved = resolve(home, where)
    if moved:
        return moved
    return by_branch(home, str(getattr(sdef, "name", "") or ""))


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
