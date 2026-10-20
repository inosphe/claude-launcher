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

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

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
    repository, when the walk timed out, or when nothing was stamped: every
    caller here is describing a session rather than gating on it, and a detail
    panel that 500s because a session's directory was pruned is worse than one
    that shows no commits.

    **The consequence, which every caller has to carry:** those are five
    different facts arriving as one value, and only the last of them is "this
    session made no commits". A pruned worktree answers empty for a session
    that committed twenty times. So nothing built on this may say the SESSION
    committed nothing — only that no stamped commit was *found*, which is a
    statement about the reading. Both callers here are worded that way on
    purpose, and :func:`claude_launcher.daemon.api.h_session_meta` reports
    ``None`` rather than an empty summary for a session with no directory, so
    that one of the five stops making any claim at all. Making the rest of the
    distinction available instead of merely avoided is ``claunch-j5kp``.
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


# ---- the rail card's commit chips -------------------------------------------
#
# The session list polls every few seconds and carries every row, so the card
# cannot afford :func:`for_session`'s one walk per session per poll. Instead
# one walk per *repository* serves every session that works in it: a single
# ``git log --all`` over every stamped commit, grouped by trailer, plus two
# ``rev-list`` calls that say which of those commits a remote has and which
# ``master`` has. The result is kept until the repository's refs change (a
# commit, push, fetch or merge all move a ref), so a quiet repository costs
# one ``for-each-ref`` per poll.

#: Commits a card shows; the rest are counted, and drawn only on the selected
#: card (:func:`rail` with ``everything``).
RAIL_LIMIT = 6

#: Seconds a snapshot is trusted even when the refs did not move (a commit on
#: a detached HEAD moves no ref). Short enough to self-heal, long enough that
#: a fleet of rows in one repository shares one walk.
RAIL_TTL = 60

#: The integration branch's names, in the order they are looked for.
_BASE_NAMES = ("master", "main")

_RAIL_FORMAT = _FS.join(
    ["%H", "%h", "%s", f"%(trailers:key={SESSION_TRAILER},valueonly)"]
) + _RS

_rail_lock = threading.RLock()
#: cwd -> (expires, common dir or None)
_rail_dirs: Dict[str, Tuple[float, Optional[str]]] = {}
#: common dir -> snapshot dict
_rail_snaps: Dict[str, Dict[str, object]] = {}


def _git(cwd: str, *args: str, stdin: Optional[str] = None) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            input=stdin,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _common_dir(cwd: str) -> Optional[str]:
    now = time.monotonic()
    with _rail_lock:
        hit = _rail_dirs.get(cwd)
        if hit and hit[0] > now:
            return hit[1]
    found: Optional[str] = None
    if Path(cwd).is_dir():
        out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        found = out.strip() or None if out else None
    with _rail_lock:
        if len(_rail_dirs) >= 4096:
            _rail_dirs.clear()
        _rail_dirs[cwd] = (now + RAIL_TTL, found)
    return found


def _refs(cwd: str) -> Optional[Dict[str, str]]:
    out = _git(cwd, "for-each-ref", "--format=%(objectname) %(refname)")
    if out is None:
        return None
    refs: Dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition(" ")
        if name:
            refs[name] = sha
    return refs


def _base_refs(refs: Dict[str, str]) -> List[str]:
    for base in _BASE_NAMES:
        found = [
            r for r in refs
            if r == f"refs/heads/{base}"
            or (r.startswith("refs/remotes/") and r.endswith(f"/{base}")
                and r.count("/") == 3)
        ]
        if found:
            return sorted(found)
    return []


def _branch_refs(refs: Dict[str, str], branch: str) -> List[str]:
    """The local and remote-tracking refs a PR's head branch goes by."""
    if not branch:
        return []
    return sorted(
        r for r in refs
        if r == f"refs/heads/{branch}"
        or (r.startswith("refs/remotes/") and r.split("/", 3)[-1] == branch
            and r.count("/") >= 3)
    )


def _not_reachable(cwd: str, shas: List[str], refs: List[str]) -> Optional[set]:
    """Which of ``shas`` no ref in ``refs`` reaches (all of them when no refs)."""
    if not shas:
        return set()
    if not refs:
        return set(shas)
    # Every revision goes through stdin, negations included, so a fleet's
    # worth of commits never meets the argv length limit.
    feed = "\n".join(shas + [f"^{r}" for r in refs]) + "\n"
    out = _git(cwd, "rev-list", "--stdin", stdin=feed)
    if out is None:
        return None
    return set(shas) & set(out.split())


def _pr_commits(cwd: str, refs: Dict[str, str], head: str, base: str) -> frozenset:
    """The commits a PR from ``head`` into ``base`` carries.

    Open: what the head branch has that the base does not. Merged with a
    merge commit: the base no longer lacks anything, so the PR's commits are
    the ones that merge brought in — reachable from the head, not from the
    merge's first parent. A fast-forward or squash merge leaves nothing to
    tell the PR's commits from the base's, and an unknown or deleted head
    branch nothing to start from: both answer empty, and the card draws the
    PR's frame without commits in it rather than guess.
    """
    heads = _branch_refs(refs, head)
    bases = _branch_refs(refs, base) if base else []
    bases = bases or _base_refs(refs)
    if not heads or not bases:
        return frozenset()
    out = _git(cwd, "rev-list", *heads, "--not", *bases)
    if out is None:
        return frozenset()
    own = set(out.split())
    if own:
        return frozenset(own)
    # Every head commit is on the base. Walk the base back to the merge that
    # brought the (remote, when there is one) head in.
    remote = [r for r in heads if r.startswith("refs/remotes/")]
    tip = refs[(remote or heads)[0]]
    base_ref = ([r for r in bases if r.startswith("refs/remotes/")] or bases)[0]
    merges = _git(cwd, "rev-list", "--ancestry-path", "--merges", "--reverse",
                  f"{tip}..{base_ref}")
    first = (merges or "").split()
    if not first:
        return frozenset()
    out = _git(cwd, "rev-list", tip, f"^{first[0]}^1")
    return frozenset((out or "").split())


def _mark_states(cwd: str, refs: Dict[str, str], rows: List[Dict[str, str]]) -> bool:
    """Set ``state`` on each of ``rows`` that has none; False when git failed."""
    rows = [c for c in rows if "state" not in c]
    if not rows:
        return True
    shas = [c["sha"] for c in rows]
    unpushed = _not_reachable(
        cwd, shas, sorted(r for r in refs if r.startswith("refs/remotes/")))
    unmerged = _not_reachable(cwd, shas, _base_refs(refs))
    if unpushed is None or unmerged is None:
        return False
    with _rail_lock:
        for c in rows:
            # master first: a local landing is merged before anyone pushes.
            c["state"] = ("merged" if c["sha"] not in unmerged
                          else "local" if c["sha"] in unpushed else "pushed")
    return True


def _owners(
    cwd: str,
    refs: Dict[str, str],
    prs: Optional[List[Dict[str, object]]],
    shas: set,
    members: Optional[Dict[Tuple[str, str], frozenset]] = None,
) -> Dict[str, Tuple[int, int]]:
    """sha -> (size, number) of the recorded PR that carries it.

    A commit two stacked PRs both carry belongs to the one whose branch
    carries fewer commits — the lower layer of the stack. ``members`` caches
    each PR's commits for as long as the refs it was read against hold.
    """
    owner: Dict[str, Tuple[int, int]] = {}
    for pr in prs or []:
        if not isinstance(pr, dict) or not pr.get("number"):
            continue
        key = (str(pr.get("branch") or ""), str(pr.get("base") or ""))
        with _rail_lock:
            inside = members.get(key) if members is not None else None
        if inside is None:
            inside = _pr_commits(cwd, refs, *key)
            if members is not None:
                with _rail_lock:
                    members[key] = inside
        for sha in inside & shas:
            if sha not in owner or len(inside) < owner[sha][0]:
                owner[sha] = (len(inside), int(pr["number"]))
    return owner


def _snapshot(cwd: str, common: str) -> Optional[Dict[str, object]]:
    refs = _refs(cwd)
    if refs is None:
        return None
    stamp = hash(tuple(sorted(refs.items())))
    now = time.monotonic()
    with _rail_lock:
        snap = _rail_snaps.get(common)
        if snap and snap["stamp"] == stamp and snap["expires"] > now:
            return snap
    out = _git(
        # --topo-order: a child before its parent even when both were made
        # in the same second on different branches, where date order ties.
        cwd, "log", "--all", "--topo-order", "--no-notes", "--extended-regexp",
        f"--grep=^{SESSION_TRAILER}:", f"--format={_RAIL_FORMAT}",
    )
    if out is None:
        return None
    by_session: Dict[str, List[Dict[str, str]]] = {}
    counts: Dict[str, int] = {}
    for record in out.split(_RS):
        fields = record.strip("\n").split(_FS)
        if len(fields) < 4:
            continue
        sha, short, subject, stamped = fields[:4]
        who = _first_line(stamped)
        if not who:
            continue
        counts[who] = counts.get(who, 0) + 1
        # Every row is kept (a selected card shows them all, claunch-7myrv);
        # only the ones a card shows by default get their state now.
        by_session.setdefault(who, []).append(
            {"sha": sha, "short": short, "subject": subject})
    if not _mark_states(cwd, refs, [c for rows in by_session.values()
                                    for c in rows[:RAIL_LIMIT]]):
        return None
    snap = {"stamp": stamp, "expires": now + RAIL_TTL, "refs": refs,
            "sessions": by_session, "counts": counts, "members": {}}
    with _rail_lock:
        if len(_rail_snaps) >= 256:
            _rail_snaps.clear()
        _rail_snaps[common] = snap
    return snap


def rail(
    cwd: Optional[str],
    session: str,
    prs: Optional[List[Dict[str, object]]] = None,
    everything: bool = False,
) -> Optional[Dict[str, object]]:
    """The session's newest commits for its rail card, or ``None``.

    The newest ``RAIL_LIMIT`` rows, as the list poll wants them; with
    ``everything`` all of them, for the selected card that unfolds its ``+N``
    (claunch-7myrv).

    Each row is ``{short, subject, state, pr?}``: ``state`` is ``merged``
    (``master`` or a remote's ``master`` reaches it), ``pushed`` (some
    remote-tracking ref reaches it) or ``local``; ``pr`` is the number of the
    recorded PR (``prs``, the rows :mod:`claude_launcher.daemon.session_prs`
    serves) whose head branch carries the commit. A commit two stacked PRs both
    carry belongs to the one whose branch carries fewer of this session's
    commits — the lower layer of the stack. ``more`` counts the commits past
    the shown ones.

    ``None`` — never an empty list — when nothing was found, for the reason
    :func:`for_session` gives: an empty reading is not evidence that the
    session committed nothing, so the card draws no line rather than a claim.
    """
    session = (session or "").strip()
    if not session or not cwd:
        return None
    common = _common_dir(str(cwd))
    if not common:
        return None
    snap = _snapshot(str(cwd), common)
    if not snap:
        return None
    rows = snap["sessions"].get(session) or []
    if not rows:
        return None
    rows = rows if everything else rows[:RAIL_LIMIT]
    if not _mark_states(str(cwd), snap["refs"], rows):
        return None
    owner = _owners(str(cwd), snap["refs"], prs, {c["sha"] for c in rows},
                    snap["members"])
    out = []
    for c in rows:
        # ``sha`` is what the chip's link to the commit page names: a short
        # hash unique today can stop being unique as the repository grows.
        row = {"sha": c["sha"], "short": c["short"], "subject": c["subject"],
               "state": c["state"]}
        if c["sha"] in owner:
            row["pr"] = owner[c["sha"]][1]
        out.append(row)
    return {"commits": out, "more": max(0, snap["counts"].get(session, 0) - len(out))}


# ---- an exited session's chips, kept (claunch-ork3b) ------------------------
#
# An exited session commits nothing more, so the commits one reading found are
# its commits for good: the caller keeps the record :func:`kept_rail` returns
# (the daemon writes it beside the session's meta.json) and this never walks
# for them again. What can still move is where each commit has got to and
# which PR carries it -- and only for a commit that is not merged yet, since
# master having it is final. So a record whose commits are all merged costs
# no git at all; one with a local or pushed commit costs one ``for-each-ref``
# per repository per ``RAIL_TTL`` (shared by every record in it) and a re-mark
# only when those refs moved. The git runs in the repository's common
# directory, which outlives the session's worktree.

#: The record's shape; a record of another version is read again.
KEPT_VERSION = 1

#: common dir -> (expires, refs or None)
_kept_refs: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}


def _refs_of(common: str) -> Optional[Dict[str, str]]:
    now = time.monotonic()
    with _rail_lock:
        hit = _kept_refs.get(common)
        if hit and hit[0] > now:
            return hit[1]
    refs = _refs(common) if Path(common).is_dir() else None
    with _rail_lock:
        if len(_kept_refs) >= 256:
            _kept_refs.clear()
        _kept_refs[common] = (now + RAIL_TTL, refs)
    return refs


def _refs_stamp(refs: Dict[str, str]) -> str:
    # Written to disk and compared after a restart, so not ``hash()``: that
    # is salted per process.
    text = "\n".join(f"{sha} {name}" for name, sha in sorted(refs.items()))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _prs_key(prs: Optional[List[Dict[str, object]]]) -> str:
    keys = sorted(
        [int(p["number"]), str(p.get("branch") or ""), str(p.get("base") or "")]
        for p in prs or [] if isinstance(p, dict) and p.get("number")
    )
    return json.dumps(keys)


def kept_current(record: Optional[dict], session: str, created_at: Optional[str],
                 exited_at: Optional[str]) -> bool:
    """Whether ``record`` is this session's for this exit, so that settling
    it reads no commits (it may still re-mark states)."""
    return (
        isinstance(record, dict)
        and record.get("v") == KEPT_VERSION
        and record.get("session") == session
        and record.get("created_at") == created_at
        and record.get("exited_at") == exited_at
    )


def kept_view(record: Optional[dict]) -> Optional[Dict[str, object]]:
    """The chips a kept record shows, in :func:`rail`'s shape; ``None`` when
    it holds no commits."""
    rows = (record or {}).get("rows") or []
    if not rows:
        return None
    shown = [dict(r) for r in rows[:RAIL_LIMIT]]
    return {"commits": shown, "more": max(0, len(rows) - len(shown))}


def _read_kept(session: str, cwds: List[Optional[str]], prs, created_at,
               exited_at, prior: Optional[dict],
               allow: Callable[[], bool]) -> Optional[dict]:
    for cwd in cwds:
        if not cwd:
            continue
        common = _common_dir(str(cwd))
        if not common:
            continue
        # The budget is spent on a repository found, never on a directory
        # that is gone: a row with nothing to read would otherwise take a
        # slot on every poll and starve the readable rows behind it.
        if not allow():
            return None
        found = rail(str(cwd), session, prs, everything=True)
        with _rail_lock:
            snap = _rail_snaps.get(common)
        if snap is None:
            continue
        if found is None:
            if session in snap["sessions"]:
                continue  # found, but git failed to mark them: not a reading
            rows: List[Dict[str, object]] = []
        else:
            rows = found["commits"]
        if prior:
            # Only added to: what an earlier exit kept stays, and a commit
            # it saw merged stays merged.
            old = {r["sha"]: r for r in prior.get("rows") or []}
            for r in rows:
                was = old.get(r["sha"])
                if was and was.get("state") == "merged":
                    r["state"] = "merged"
                if was and "pr" in was and "pr" not in r:
                    r["pr"] = was["pr"]
            seen = {r["sha"] for r in rows}
            rows = rows + [dict(r) for r in prior.get("rows") or [] if r["sha"] not in seen]
        return {
            "v": KEPT_VERSION, "session": session, "created_at": created_at,
            "exited_at": exited_at, "common": common,
            "stamp": _refs_stamp(snap["refs"]), "prs": _prs_key(prs), "rows": rows,
        }
    return None


def _settle(record: dict, prs) -> Tuple[dict, bool]:
    rows = record.get("rows") or []
    key = _prs_key(prs)
    prs_moved = record.get("prs") != key
    if not prs_moved and all(r.get("state") == "merged" for r in rows):
        return record, False
    common = record.get("common")
    refs = _refs_of(common) if common else None
    if refs is None:
        return record, False
    stamp = _refs_stamp(refs)
    if stamp == record.get("stamp") and not prs_moved:
        return record, False
    new = [dict(r) for r in rows]
    redo = [r for r in new if r.get("state") != "merged"]
    probe: List[Dict[str, str]] = [{"sha": r["sha"]} for r in redo]
    if not _mark_states(common, refs, probe):
        return record, False
    for r, p in zip(redo, probe):
        r["state"] = p["state"]
    ask = {r["sha"] for r in new
           if prs_moved or r.get("state") != "merged" or "pr" not in r}
    ask |= {r["sha"] for r in redo}
    owner = _owners(common, refs, prs, ask)
    for r in new:
        # A PR once found stays: a deleted head branch must not empty a frame.
        if r["sha"] in owner:
            r["pr"] = owner[r["sha"]][1]
    return dict(record, rows=new, stamp=stamp, prs=key), True


def kept_rail(
    record: Optional[dict],
    session: str,
    *,
    created_at: Optional[str],
    exited_at: Optional[str],
    cwds: List[Optional[str]],
    prs: Optional[List[Dict[str, object]]] = None,
    read: Union[bool, Callable[[], bool]] = True,
) -> Tuple[Optional[dict], bool]:
    """An exited session's kept chips: ``(record, changed)``.

    ``record`` is what an earlier call returned (or ``None``). It is used as
    it is while it belongs to this session, this creation (``created_at`` --
    a reused name is another session) and this exit; then only the states of
    its unmerged commits and its PR numbers are brought up to date, as the
    module comment above says. Otherwise -- no record, or the session ran
    again since (``exited_at`` moved) -- the commits are read from the first
    of ``cwds`` that answers, and an earlier record's commits are kept beside
    them. ``read=False`` forbids that reading (the caller's budget); the old
    record, or ``None``, comes back unchanged; a callable is asked only once
    a repository is found to read. A reading that failed gives
    no record: an unreadable repository is not "no commits" (claunch-j5kp).
    ``changed`` says the caller should keep the new record.
    """
    session = (session or "").strip()
    if not session:
        return None, False
    if not (isinstance(record, dict) and record.get("v") == KEPT_VERSION
            and record.get("session") == session
            and record.get("created_at") == created_at):
        record = None
    if kept_current(record, session, created_at, exited_at):
        return _settle(record, prs)
    if read is False:
        return record, False
    allow = read if callable(read) else (lambda: True)
    fresh = _read_kept(session, cwds, prs, created_at, exited_at, record, allow)
    if fresh is None:
        return record, False
    return fresh, True


# ---- a rebased-away commit, marked superseded (claunch-rqtpe) ---------------
#
# A rebase leaves the old hash on the pushed branch, so the card showed it as
# pushed-not-merged although master has the same change under the new hash
# (s623's b48ce17, landed as 7ab01a5). Such a commit is marked ``superseded``
# by the merged commit that carries its change. The candidates are only the
# same session's merged commits with the same subject -- a rebase and a
# cherry-pick keep both -- so no walk of master is needed. "The same change"
# is a diff with its context left out: ``git patch-id`` hashes the hunk
# headers' context too, so a function renamed on master changes it although
# the commit did not change (claunch-patchid-context-drift-68mu).
#
# Both facts are fixed once found: a commit's diff never changes, and a
# commit superseded by a merged one stays so. The caller keeps the record
# :func:`supersede` returns (the daemon writes it beside the session's
# meta.json), so each commit's diff is read at most once.

#: The record's shape; a record of another version is started again.
SUPERSEDED_VERSION = 1


def _fingerprint(cwd: str, sha: str) -> Optional[str]:
    """A hash of what ``sha`` changes, its context left out; ``""`` when it
    changes nothing a diff shows (a merge commit, an empty commit) and
    ``None`` when git failed."""
    out = _git(cwd, "diff-tree", "-p", "--unified=0", "--no-color",
               "--no-ext-diff", "--no-renames", "--root", "--format=", sha)
    if out is None:
        return None
    lines = [line for line in out.splitlines()
             if not line.startswith(("@@", "index "))]
    if not lines:
        return ""
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def supersede(
    cwd: Optional[str],
    rows: List[Dict[str, object]],
    pool: List[Dict[str, object]],
    record: Optional[dict],
    refs: Optional[Dict[str, str]] = None,
) -> Tuple[dict, bool]:
    """Mark each unmerged row of ``rows`` that a merged row of ``pool`` (the
    same session's commits) supersedes: ``(record, changed)``.

    A superseded row gets ``superseded: {sha, short}``, the merged commit
    that carries its change. ``record`` is what an earlier call returned
    (or ``None``); ``changed`` says the caller should keep the new one.
    ``refs`` lets a pool row with no ``state`` yet be marked; without it
    such a row is not a candidate.
    """
    if not (isinstance(record, dict) and record.get("v") == SUPERSEDED_VERSION):
        record = {"v": SUPERSEDED_VERSION, "fp": {}, "by": {}}
    fp: Dict[str, str] = dict(record.get("fp") or {})
    by: Dict[str, Dict[str, str]] = dict(record.get("by") or {})
    changed = False
    for row in rows:
        sha = str(row.get("sha") or "")
        if not sha or row.get("state") == "merged":
            continue
        if sha not in by and cwd:
            cands = [p for p in pool
                     if p.get("subject") == row.get("subject") and p.get("sha") != sha]
            unstated = [p for p in cands if "state" not in p]
            if unstated and refs is not None:
                _mark_states(cwd, refs, unstated)
            cands = [p for p in cands if p.get("state") == "merged"]
            # No merged commit of the same subject: nothing to read.
            for one in ([sha] + [str(p["sha"]) for p in cands]) if cands else []:
                if one not in fp:
                    got = _fingerprint(cwd, one)
                    if got is not None:
                        fp[one] = got
                        changed = True
            mine = fp.get(sha)
            for p in cands:
                if mine and fp.get(str(p["sha"])) == mine:
                    by[sha] = {"sha": str(p["sha"]), "short": str(p.get("short") or "")}
                    changed = True
                    break
        if sha in by:
            row["superseded"] = dict(by[sha])
    if changed:
        record = {"v": SUPERSEDED_VERSION, "fp": fp, "by": by}
    return record, changed


def rail_superseded(
    cwd: Optional[str], session: str, chips: Optional[Dict[str, object]],
    record: Optional[dict],
) -> Tuple[Optional[dict], bool]:
    """:func:`supersede` for the chips :func:`rail` just returned, its pool
    the session's commits off the same walk."""
    if not chips or not cwd:
        return record, False
    common = _common_dir(str(cwd))
    with _rail_lock:
        snap = _rail_snaps.get(common) if common else None
    if not snap:
        return record, False
    pool = snap["sessions"].get((session or "").strip()) or []
    return supersede(str(cwd), chips["commits"], pool, record, snap["refs"])
