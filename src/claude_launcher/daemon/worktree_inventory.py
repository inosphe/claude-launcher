"""The launcher worktrees on this machine, who stands in them, and removing them.

Every session created with a worktree leaves a checkout under
``<repo>/.claude/worktrees/<name>`` (see :mod:`claude_launcher.worktree`), and
nothing ever takes one away: the session ends, is archived, and the checkout
stays with its branch and whatever was left in it. This module is the reading
the web UI's Worktrees page draws, and the one verb that clears them.

Which repositories are looked at: the registered workspaces, plus the
repository of every session record's directory. A session record carries its
directory and not the worktree it was cut into, so the two are joined on the
path — a session belongs to a worktree when its directory is that checkout or
inside it.

Three states, from the sessions joined to a checkout:

- ``active``   — at least one session that is not archived (running, paused
  or killed). Such a session can come back into that directory, so the
  checkout is not removed unless those sessions are archived with it.
- ``orphaned`` — sessions were joined, and every one of them is archived.
- ``unlinked`` — no session record names the checkout (records cleared, or a
  worktree made outside a session).

Removal never follows a link. ``git worktree remove`` deletes through a
directory junction or symlink inside the checkout into the tree it points at
(claunch-4m2s9: a borrowed ``node_modules`` junction took 562 tracked files of
another worktree with it), so every link inside the checkout is unlinked
first and only then is the checkout handed to git. The branch is kept: a
branch is history, and whether it can go is a separate decision.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import worktree as worktree_mod

#: Read-only git calls are bounded; see ``worktree._READ_TIMEOUT``.
_READ_TIMEOUT = 20.0

STATE_ACTIVE = "active"
STATE_ORPHANED = "orphaned"
STATE_UNLINKED = "unlinked"

#: Candidate trunk names, in the order they are preferred.
TRUNKS = ("master", "main")

#: ``repo_root`` answers per directory, kept for the daemon's life. A session
#: directory's repository does not change, and the rail holds hundreds of
#: records whose directories would otherwise each cost a git process on every
#: page load.
_ROOT_CACHE: Dict[str, Optional[str]] = {}


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _git(args: List[str], cwd: str, timeout: Optional[float] = _READ_TIMEOUT):
    return worktree_mod._git(args, cwd=cwd, timeout=timeout)


def _root_of(cwd: str) -> Optional[str]:
    """The main checkout of ``cwd``'s repository, cached, without git when the
    directory is itself a launcher worktree (the common case)."""
    if not cwd:
        return None
    marker = os.sep + str(worktree_mod.WORKTREES_SUBDIR) + os.sep
    norm = os.path.abspath(cwd)
    at = norm.lower().find(marker.lower())
    if at > 0:
        return norm[:at]
    key = _key(norm)
    if key not in _ROOT_CACHE:
        if not os.path.isdir(norm):
            return None  # not cached: a directory can appear later
        root = worktree_mod.repo_root(norm)
        _ROOT_CACHE[key] = str(root) if root else None
    return _ROOT_CACHE[key]


def repo_roots(dirs: Iterable[str]) -> List[str]:
    """Distinct repository roots of ``dirs`` that still exist, in first-seen
    order."""
    seen: Dict[str, str] = {}
    for d in dirs:
        root = _root_of(d)
        if root and _key(root) not in seen and os.path.isdir(root):
            seen[_key(root)] = root
    return list(seen.values())


def trunk(root: str) -> str:
    """The repository's trunk branch name, or ``""`` when it has neither."""
    done = _git(
        ["for-each-ref", "--format=%(refname:short)",
         *[f"refs/heads/{t}" for t in TRUNKS]],
        cwd=root,
    )
    have = set((done.stdout or "").split()) if done.returncode == 0 else set()
    return next((t for t in TRUNKS if t in have), "")


def _porcelain(root: str) -> List[dict]:
    """``git worktree list --porcelain`` as one dict per checkout."""
    done = _git(["worktree", "list", "--porcelain"], cwd=root)
    if done.returncode != 0:
        return []
    out: List[dict] = []
    cur: dict = {}
    for line in (done.stdout or "").splitlines() + [""]:
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value.strip() if value else True
    return out


def _ahead_behind(root: str, base: str) -> Dict[str, Tuple[int, int]]:
    """``branch -> (ahead, behind)`` against ``base`` for every local branch,
    in one git call. Empty when git cannot answer (``ahead-behind`` needs git
    2.41); :func:`_merged_set` is the fallback."""
    done = _git(
        ["for-each-ref", f"--format=%(refname:short)\t%(ahead-behind:{base})",
         "refs/heads"],
        cwd=root,
    )
    if done.returncode != 0:
        return {}
    out: Dict[str, Tuple[int, int]] = {}
    for line in (done.stdout or "").splitlines():
        name, _, counts = line.partition("\t")
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            out[name] = (int(parts[0]), int(parts[1]))
    return out


def _merged_set(root: str, base: str) -> Optional[set]:
    done = _git(
        ["for-each-ref", f"--merged={base}", "--format=%(refname:short)",
         "refs/heads"],
        cwd=root,
    )
    if done.returncode != 0:
        return None
    return set((done.stdout or "").split())


def _is_ancestor(root: str, sha: str, base: str) -> Optional[bool]:
    done = _git(["merge-base", "--is-ancestor", sha, base], cwd=root)
    if done.returncode in (0, 1) and not (done.stderr or "").strip():
        return done.returncode == 0
    return None


def created_at(path: str) -> Optional[str]:
    """When the checkout was made, as ISO-8601 UTC.

    Read from the worktree's administrative ``commondir`` file, which
    ``git worktree add`` writes once and nothing rewrites; the checkout
    directory's own times move with every edit. Falls back to the
    directory's birth time where the platform keeps one.
    """
    stamp: Optional[float] = None
    try:
        text = Path(path, ".git").read_text(encoding="utf-8", errors="replace")
        if text.startswith("gitdir:"):
            admin = Path(text[len("gitdir:"):].strip())
            if not admin.is_absolute():
                admin = Path(path) / admin
            stamp = (admin / "commondir").stat().st_mtime
    except OSError:
        stamp = None
    if stamp is None:
        try:
            st = os.stat(path)
            stamp = getattr(st, "st_birthtime", None) or (
                st.st_ctime if sys.platform == "win32" else st.st_mtime
            )
        except OSError:
            return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def _inside(child: str, parent: str) -> bool:
    c, p = _key(child), _key(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _state(sessions: List[dict]) -> str:
    if any(s.get("category") != "archived" for s in sessions):
        return STATE_ACTIVE
    return STATE_ORPHANED if sessions else STATE_UNLINKED


def list_repo(root: str, sessions: List[dict]) -> dict:
    """Every launcher worktree of ``root`` with its branch, merge state and
    the sessions joined to it.

    ``sessions`` are ``{"name", "cwd", "category", "status", "created_at"}``
    dicts — the daemon's records reduced to what the join needs, so this can
    be read without a live manager.
    """
    base_dir = str(worktree_mod.worktrees_dir(Path(root)))
    base = trunk(root)
    counts = _ahead_behind(root, base) if base else {}
    merged = None if counts or not base else _merged_set(root, base)
    items: List[dict] = []
    for entry in _porcelain(root):
        path = os.path.abspath(str(entry.get("worktree", "")))
        if not path or not _inside(path, base_dir) or _key(path) == _key(base_dir):
            continue
        name = os.path.relpath(path, base_dir).replace(os.sep, "/")
        ref = entry.get("branch")
        branch = ref[len("refs/heads/"):] if isinstance(ref, str) and \
            ref.startswith("refs/heads/") else ""
        head = str(entry.get("HEAD", "") or "")
        ahead = behind = None
        is_merged: Optional[bool] = None
        if base and branch and branch in counts:
            ahead, behind = counts[branch]
            is_merged = ahead == 0
        elif base and branch and merged is not None:
            is_merged = branch in merged
        elif base and head and not branch:
            is_merged = _is_ancestor(root, head, base)
        joined = [s for s in sessions if s.get("cwd") and _inside(s["cwd"], path)]
        joined.sort(key=lambda s: s.get("created_at") or "")
        items.append({
            "name": name,
            "path": path,
            "branch": branch,
            "head": head[:12],
            "detached": not branch,
            "trunk": base,
            "merged": is_merged,
            "ahead": ahead,
            "behind": behind,
            "is_trunk": bool(branch) and branch == base,
            "missing": not os.path.isdir(path),
            "locked": "locked" in entry,
            "prunable": "prunable" in entry,
            "created_at": created_at(path),
            "sessions": joined,
            "state": _state(joined),
        })
    items.sort(key=lambda w: w.get("created_at") or "", reverse=True)
    return {"root": root, "trunk": base, "worktrees": items}


def inventory(roots: Iterable[str], sessions: List[dict]) -> dict:
    """:func:`list_repo` for every root; a root git cannot read is reported,
    not raised."""
    repos: List[dict] = []
    errors: List[dict] = []
    for root in roots:
        try:
            repo = list_repo(root, sessions)
        except worktree_mod.WorktreeError as exc:
            errors.append({"root": root, "error": str(exc)})
            continue
        if repo["worktrees"]:
            repos.append(repo)
    return {"repos": repos, "errors": errors}


# --------------------------------------------------------------------------- #
# removing
# --------------------------------------------------------------------------- #
class RemoveError(Exception):
    """A checkout that was not removed, and why."""


def find(roots: Iterable[str], path: str) -> Optional[Tuple[str, dict]]:
    """``(root, porcelain entry)`` of the launcher worktree at ``path``."""
    want = _key(path)
    for root in roots:
        base_dir = str(worktree_mod.worktrees_dir(Path(root)))
        for entry in _porcelain(root):
            p = os.path.abspath(str(entry.get("worktree", "")))
            if _key(p) == want and _inside(p, base_dir) and _key(p) != _key(base_dir):
                return root, entry
    return None


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def unlink_links(path: str) -> List[str]:
    """Remove every symlink and junction under ``path`` — the link itself,
    never what it points at — and answer the paths removed.

    Walks without following a link, so a borrowed tree is never entered.
    """
    removed: List[str] = []
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if _is_link(entry):
                try:
                    # A junction or directory symlink is removed with rmdir,
                    # which deletes the link and not the target's contents.
                    try:
                        os.rmdir(entry.path)
                    except OSError:
                        os.unlink(entry.path)
                    removed.append(entry.path)
                except OSError as exc:
                    raise RemoveError(
                        f"could not unlink {entry.path} before removal: {exc}"
                    ) from exc
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
            except OSError:
                continue
    return removed


def _hosts_this_daemon(path: str) -> bool:
    here = os.path.abspath(__file__)
    return _inside(here, path) or _inside(os.getcwd(), path)


def remove(root: str, path: str, *, force: bool = False) -> dict:
    """Remove the launcher worktree ``path`` of ``root``; the branch stays.

    Without ``force`` git refuses a checkout with modified or untracked
    files, and that refusal is passed on as the answer. With it, those files
    are discarded.
    """
    if _hosts_this_daemon(path):
        raise RemoveError("this daemon is running from that checkout")
    if not os.path.isdir(path):
        # The directory is already gone; only git's record of it is left.
        done = _git(["worktree", "prune"], cwd=root, timeout=None)
        if done.returncode != 0:
            raise RemoveError((done.stderr or "git worktree prune failed").strip())
        return {"path": path, "root": root, "unlinked": [], "pruned": True}
    links = unlink_links(path) if os.path.isdir(path) else []
    args = ["worktree", "remove", *(["--force"] if force else []), path]
    done = _git(args, cwd=root, timeout=None)
    if done.returncode != 0 and os.path.isdir(path):
        # A session that was just archived can still hold a file open for a
        # moment on Windows; one retry covers that without hiding a refusal.
        time.sleep(1.0)
        done = _git(args, cwd=root, timeout=None)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise RemoveError(detail or "git worktree remove failed")
    return {"path": path, "root": root, "unlinked": links}
