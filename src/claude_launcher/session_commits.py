"""What a session actually committed, read back out of the repository.

A round leaves three things behind. The cflow journal says what each step
reported, :mod:`claude_launcher.reports` keeps the one HTML page the round
was written up on, and the third — the commits — had nowhere to be read.
``git log`` holds them, but every agent in a fleet commits under the one
human's name, so the log alone cannot answer "what did *that* session do".

It can, though, because ``commit-stamp`` already put the answer in the
message: :mod:`claude_launcher.commit_stamp` makes every agent end its
commits with ``Claunch-Session:`` and (in a linked worktree)
``Claunch-Worktree:`` trailers, and names the exact query this module runs.
So this is a *reader*, not a registry:

* **Nothing is written.** The commit is the record. There is no file to keep
  in sync, nothing to miss when a session is killed mid-round, and no way for
  the list to disagree with the history it describes — the same reason
  :mod:`claude_launcher.reports` makes a directory listing be its own index.
* **The trailer is checked, not just grepped.** ``--grep`` narrows the walk,
  but a session named ``s19`` must not collect ``s191``'s work, so the
  trailer read off each commit is compared exactly before it counts.
* **Every branch, not just the checked-out one.** A worker's commits live on
  its feature branch, which is usually not what the daemon's read of the
  repository has checked out, so the walk starts from all refs.

The daemon serves the result inside ``/api/sessions/<name>/meta`` and
``claunch commits`` prints the same thing; both call :func:`for_session`, so
the dashboard and the terminal cannot drift apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, List, Optional

#: Trailer keys, spelled as ``commit-stamp`` teaches them. Changing either
#: spelling here without changing it there silently empties every list.
SESSION_TRAILER = "Claunch-Session"
WORKTREE_TRAILER = "Claunch-Worktree"

#: How many commits a caller gets when it does not say. A round is a handful
#: of commits; this is high enough that a long one is still whole and low
#: enough that a session detail panel stays a panel.
DEFAULT_LIMIT = 50

#: Seconds before the walk is abandoned and an empty list returned. The
#: daemon calls this on a request path, so a pathological repository must
#: cost a slow panel, never a hung one.
TIMEOUT = 10

#: Record and field separators. ASCII 0x1e/0x1f cannot appear in a subject or
#: a trailer value, so no quoting question arises.
_RS = "\x1e"
_FS = "\x1f"

_FORMAT = _FS.join(
    [
        "%H",
        "%h",
        "%cI",
        "%s",
        f"%(trailers:key={SESSION_TRAILER},valueonly)",
        f"%(trailers:key={WORKTREE_TRAILER},valueonly)",
    ]
) + _RS


def _first_line(value: str) -> str:
    """The first non-empty line of a trailer atom's (newline-joined) value."""
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _parse(out: str, session: str, limit: int) -> List[Dict[str, str]]:
    commits: List[Dict[str, str]] = []
    for record in out.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_FS)
        if len(fields) < 6:
            continue
        sha, short, when, subject, stamped, worktree = fields[:6]
        # The exact check the module docstring promises: ``--grep`` is a
        # filter on the walk, this is the one that decides membership.
        if _first_line(stamped) != session:
            continue
        entry = {
            "sha": sha,
            "short": short,
            "committed_at": when,
            "subject": subject,
            "session": session,
        }
        wt = _first_line(worktree)
        if wt:
            entry["worktree"] = wt
        commits.append(entry)
        if len(commits) >= limit:
            break
    return commits


def for_session(
    cwd: Optional[str | Path],
    session: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, str]]:
    """The commits ``session`` made in the repository at ``cwd``, newest first.

    Returns ``[]`` — never raises — when there is no directory, no git, no
    repository, or nothing stamped: every caller here is describing a session
    rather than gating on it, and a detail panel that 500s because a session's
    directory was pruned is worse than one that shows no commits.
    """
    session = (session or "").strip()
    if not session or not cwd:
        return []
    path = Path(cwd)
    if not path.is_dir():
        return []
    # ``-n`` is applied by git to the *grepped* set, and the exact-trailer
    # check below can still drop rows from it, so ask for more than we mean
    # to return rather than handing back a short list built from a full one.
    scan = max(limit * 2, limit + 20)
    cmd = [
        "git",
        "-C",
        str(path),
        "log",
        "--all",
        "--no-notes",
        f"--max-count={scan}",
        "--extended-regexp",
        f"--grep=^{SESSION_TRAILER}:[ \t]*{session}[ \t]*$",
        f"--format={_FORMAT}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return _parse(proc.stdout, session, limit)


def summary(commits: List[Dict[str, str]]) -> Dict[str, object]:
    """The shape ``/meta`` carries: the list plus the two facts a rail shows.

    ``latest`` is the newest commit's short hash and ``worktrees`` the distinct
    checkouts the session committed from — one for the usual worker, more when
    a session moved, and none when it only ever worked in the main checkout.
    """
    worktrees: List[str] = []
    for c in commits:
        wt = c.get("worktree")
        if wt and wt not in worktrees:
            worktrees.append(wt)
    return {
        "commits": commits,
        "count": len(commits),
        "latest": commits[0]["short"] if commits else None,
        "worktrees": worktrees,
    }
