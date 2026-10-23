"""What artifact cleanup sees, per session, and how much disk each part holds.

The web UI's Artifacts page draws this read-only listing. It answers the
question a person asks before running a cleanup: which directories belong to
which session, which of them the cleanup would consider, and how much they
cost on disk. Nothing here deletes anything -- :mod:`artifact_cleanup` keeps
that, and the two agree because the listing reuses its candidate rule
(``artifact_cleanup.scratch_candidate``, the one ``Cleanup._candidates`` also
calls) and the basetemp ownership rules
(``basetemp_reaper.owner``/``matches``/``sweep_generation``) rather than
restating them.

Sizes are never walked inline. A listing is one ``listdir`` and one ``stat``
per entry, but walking a few hundred thousand files takes seconds to tens of
seconds (the same measurement that made the Workspaces page cache its sizes),
so :func:`measure` walks in a thread when asked and keeps each answer in
``artifact-usage.json`` in the daemon directory. A row carries the time it was
measured and reads ``stale`` once the directory's mtime has moved since, which
is the cheap signal that the number no longer describes what is there.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .. import atomic, pytest_basetemp, workspace_usage
from . import artifact_cleanup as cleanup
from . import basetemp_reaper as basetemp
from . import paths
from .session import session_category

USAGE_FILE = "artifact-usage.json"

#: Paths being walked right now, so a second request joins the page's poll
#: instead of starting a second walk over the same tree.
_running: set = set()
_running_lock = threading.Lock()
_save_lock = threading.Lock()

#: The last walk a measure request started: how many paths it set out to walk
#: and how many have answered. The page draws its progress bar from this
#: instead of guessing from cached sizes, which a forced walk leaves in place
#: until each path is re-walked. ``started``/``finished`` are ISO stamps.
_job: Dict[str, object] = {"total": 0, "done": 0, "started": None, "finished": None}
_job_lock = threading.Lock()


def usage_path() -> Path:
    return paths.daemon_dir() / USAGE_FILE


def load() -> Dict[str, dict]:
    """The cache: absolute path -> the last measurement of it."""
    try:
        doc = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(doc: Dict[str, dict]) -> None:
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(path) as tmp:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        atomic.replace(tmp, path)


def save_row(path: str, row: Optional[dict]) -> None:
    """Keep ``row`` as ``path``'s measurement (``None`` forgets it)."""
    with _save_lock:
        doc = load()
        if row is None:
            doc.pop(path, None)
        else:
            doc[path] = row
        _write(doc)


def retain(keep: Iterable[str]) -> None:
    """Drop cached rows for paths the current listing no longer has.

    The cache is keyed by path and a page lists every artifact of every
    session, so without this it would only grow as sessions and basetemps
    came and went.
    """
    kept = {str(p) for p in keep}
    with _save_lock:
        doc = load()
        pruned = {key: value for key, value in doc.items() if key in kept}
        if len(pruned) != len(doc):
            _write(pruned)


def running() -> List[str]:
    with _running_lock:
        return sorted(_running)


def job() -> dict:
    """The walk in progress (or the last one): ``total``, ``done``, stamps."""
    with _job_lock:
        return dict(_job)


def _job_set(**fields) -> None:
    with _job_lock:
        _job.update(fields)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(seconds) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(seconds), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _measure_one(path: str) -> dict:
    """Walk one path and describe what it holds; errors are the row's answer."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return {"error": str(exc), "at": _stamp()}
    if stat.S_ISLNK(st.st_mode):
        # Never followed: the link is not what cleanup would remove.
        return {"error": "link", "at": _stamp()}
    if not stat.S_ISDIR(st.st_mode):
        return {"error": "not a directory", "at": _stamp()}
    started = time.monotonic()
    size, files = workspace_usage.walk_size(path)
    return {"bytes": size, "files": files, "mtime_ns": st.st_mtime_ns,
            "seconds": round(time.monotonic() - started, 2), "at": _stamp()}


def measure(targets: Iterable[str], force: bool = False) -> int:
    """Walk ``targets`` one at a time, keeping each answer as it lands.

    Sequential on purpose: two walks over one disk would make both slower and
    the page already polls for progress. A row that already has a size is
    skipped unless ``force``, so a poll that asks again costs nothing.
    """
    seen = set()
    cached = load()
    todo: List[str] = []
    for raw in targets:
        path = str(raw or "")
        if not path or path in seen:
            continue
        seen.add(path)
        if not force and "bytes" in (cached.get(path) or {}):
            continue
        todo.append(path)
    # The bar counts the paths this call will walk, decided before the first
    # one starts: a skipped cache hit is not progress the reader can watch.
    _job_set(total=len(todo), done=0, started=_stamp(), finished=None)
    done = 0
    for path in todo:
        with _running_lock:
            _running.add(path)
        try:
            row = _measure_one(path)
        finally:
            with _running_lock:
                _running.discard(path)
        save_row(path, row)
        done += 1
        _job_set(done=done)
    _job_set(finished=_stamp())
    return done


# ---- the listing ---------------------------------------------------------- #

def _entries(directory: Path) -> List[Path]:
    """The directory's entries by name; nothing when it cannot be read."""
    try:
        return sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return []


def _regular_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not cleanup._is_link(st)


def _measurement(path: Path, cached: Dict[str, dict], active: set) -> dict:
    """The size fields a row carries, from the cache (never a walk)."""
    saved = cached.get(str(path)) or {}
    out = {"running": str(path) in active}
    if not saved:
        return out
    for key in ("bytes", "files", "at", "seconds", "error"):
        if key in saved:
            out[key] = saved[key]
    try:
        current = path.lstat().st_mtime_ns
    except OSError:
        current = None
    saved_mtime = saved.get("mtime_ns")
    # A size taken before the last write to the tree no longer describes it.
    out["stale"] = bool(saved_mtime and current and saved_mtime != current)
    return out


def _area_row(path: Path, cached: Dict[str, dict], active: set,
              entries: Optional[int] = None) -> dict:
    row = {"path": str(path), "exists": path.is_dir()}
    if entries is not None:
        row["entries"] = entries
    row.update(_measurement(path, cached, active))
    return row


def _kind_area(area: str, name: str, session: str) -> str:
    if area == "basetemp":
        if name == session:
            return "test-dir"
        if basetemp.sweep_generation(name, session):
            return "sweep"
        if basetemp.stamped_run(name, session):
            return "run"
        return "manual"
    if name.startswith("pytest-"):
        return "pytest"
    if name == "browser":
        return "browser"
    if "preview" in name:
        return "preview"
    return "other"


def _live_use(path: Path, live_roots: List[str]) -> str:
    """The same verdict ``artifact_cleanup._candidates`` gives an item.

    ``live_roots`` are the live or held sessions' directories, resolved once
    per listing and kept as normalised strings: the check runs for every item,
    and comparing ``Path`` ancestors against every session built a Path per
    component per comparison -- thousands of items times hundreds of sessions.
    """
    try:
        target = os.path.normcase(str(path.resolve()))
    except OSError:
        return ""
    prefix = target.rstrip(os.sep) + os.sep
    for here in live_roots:
        if here == target or here.startswith(prefix):
            return "a live or held session uses this directory"
    return ""


def _item_row(path: Path, area: str, kind: str, cached: Dict[str, dict],
              active: set, live_roots: List[str], reserved: str = "") -> dict:
    row = {"path": str(path), "name": path.name, "area": area, "kind": kind}
    try:
        row["modified"] = _iso(path.lstat().st_mtime)
    except OSError:
        pass
    row.update(_measurement(path, cached, active))
    reason = reserved or _live_use(path, live_roots)
    if reason:
        row["reason"] = reason
    return row


def _checkout_root(cwd: str) -> Optional[str]:
    """The main checkout of the repository ``cwd`` stands in, or None.

    Only used to share one ``git worktree list`` between the sessions that
    stand in different worktrees of the same repository: a linked worktree
    carries ``.git`` as a file naming ``<main>/.git/worktrees/<name>``, and
    running that listing once per worktree cost a subprocess per session with
    a scratch directory (417 of them on this host, 6.6s of one page load).
    """
    try:
        path = Path(cwd).resolve()
    except OSError:
        return None
    for here in (path, *path.parents):
        git = here / ".git"
        try:
            if git.is_dir():
                return str(here)
            if not git.is_file():
                continue
            text = git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text.startswith("gitdir: "):
            return None
        target = Path(text[len("gitdir: "):].strip())
        if not target.is_absolute():
            target = here / target
        git_dir = Path(os.path.normpath(target))
        # <main>/.git/worktrees/<name>: the main checkout is three levels up.
        # Any other layout (a separate git dir) is left to this checkout --
        # git run from here lists the same worktrees.
        if (git_dir.parent.name.lower() == "worktrees"
                and git_dir.parent.parent.name == ".git"):
            return str(git_dir.parent.parent.parent)
        return str(here)
    return None


def _foreign() -> set:
    try:
        return cleanup._foreign_names()
    except (ValueError, OSError, sqlite3.Error):
        # The listing is a reading, not a gate: an unreadable other instance
        # leaves the reservation column empty instead of failing the page.
        return set()


def _cleanup_state(session: str) -> dict:
    """The schedule waiting for this session and its recent cleanup receipts."""
    folder = paths.session_dir(session) / "cleanup"
    out = {"folder": str(folder), "scheduled": None, "receipts": []}
    receipts = []
    for entry in _entries(folder):
        if not _regular_file(entry):
            continue
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        if entry.name == cleanup.SCHEDULE_NAME:
            out["scheduled"] = {
                "created_at": _iso(doc.get("created_at")),
                "expires_at": _iso(doc.get("expires_at")),
                "attempts": doc.get("attempts"),
                "last_reason": doc.get("last_reason") or "",
            }
            continue
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        rows = [item for item in items if isinstance(item, dict)]
        removed = [item for item in rows if item.get("status") == "removed"]
        receipts.append({
            "plan_id": doc.get("plan_id") or entry.stem,
            "created_at": _iso(doc.get("created_at")),
            "items": len(rows),
            "removed": doc.get("removed", len(removed)),
            "bytes": sum(int(item.get("bytes") or 0) for item in removed),
            "cleanup_pending": doc.get(
                "cleanup_pending", sum(1 for item in rows if item.get("status") != "removed")),
        })
    receipts.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    out["receipts"] = receipts[:5]
    return out


def _totals(parts: List[dict]) -> dict:
    total = {"bytes": 0, "files": 0, "measured": 0, "unmeasured": 0}
    for row in parts:
        if isinstance(row.get("bytes"), int):
            total["measured"] += 1
            total["bytes"] += row["bytes"]
            total["files"] += int(row.get("files") or 0)
        elif not row.get("exists", True):
            continue  # a directory that is not there holds nothing
        else:
            total["unmeasured"] += 1
    return total


def inventory(manager) -> dict:
    """Every session's artifact directories, the orphans, and the cached sizes.

    Read-only and cheap: one listing per session scratch, one per basetemp
    root, and one read of the registry. Sizes come from ``artifact-usage.json``;
    :func:`measure` fills them.
    """
    cached = load()
    active = set(running())
    sessions = list(manager.list())
    names = [row.sdef.name for row in sessions]
    root = basetemp.default_root()
    reserved_names = _foreign()
    registered: Dict[str, set] = {}

    def registered_for(cwd: str) -> set:
        # One git reading per repository, shared by every session standing in
        # any of its worktrees.
        root = _checkout_root(cwd) if cwd else None
        key = os.path.normcase(root or cwd or "")
        if key not in registered:
            try:
                registered[key] = (cleanup._registered_worktrees(Path(root))
                                   if root else set())
            except (OSError, ValueError):
                registered[key] = set()
        return registered[key]

    basetemps: Dict[str, List[Path]] = {}
    root_entries = _entries(root)
    for entry in root_entries:
        owner = basetemp.owner(entry.name, names)
        if owner:
            basetemps.setdefault(owner, []).append(entry)

    # Resolved once: every item is judged against these, and a session's
    # directory does not change while one listing is taken.
    live_roots: List[str] = []
    for session in sessions:
        if session.exited and not getattr(session, "held_at", None):
            continue
        cwd = str(getattr(session.sdef, "cwd", "") or "")
        if not cwd:
            continue
        try:
            live_roots.append(os.path.normcase(str(Path(cwd).resolve())))
        except OSError:
            continue

    doc_sessions = []
    for session in sessions:
        name = session.sdef.name
        cwd = str(getattr(session.sdef, "cwd", "") or "")
        scratch = paths.session_scratch_dir(name)
        scratch_files = _entries(scratch)
        items = []
        for entry in scratch_files:
            if cleanup.scratch_candidate(entry, name, registered_for(cwd)):
                items.append(_item_row(entry, "scratch",
                                       _kind_area("scratch", entry.name, name),
                                       cached, active, live_roots))
        for entry in basetemps.get(name, []):
            reserved = ("other daemon owns matching name"
                        if any(basetemp.matches(entry.name, other)
                               for other in reserved_names) else "")
            items.append(_item_row(entry, "basetemp",
                                   _kind_area("basetemp", entry.name, name),
                                   cached, active, live_roots, reserved))
        scratch_row = _area_row(scratch, cached, active, entries=len(scratch_files))
        # The scratch rows above are entries inside that one directory, so the
        # total counts the directory once and the basetemp entries separately.
        counted = [scratch_row] + [row for row in items if row["area"] == "basetemp"]
        doc_sessions.append({
            "name": name,
            "cwd": cwd,
            "category": session_category(session),
            "live": not session.exited and not getattr(session, "held_at", None),
            "scratch": scratch_row,
            "items": items,
            "cleanup": _cleanup_state(name),
            "totals": _totals(counted),
        })

    orphans = [
        _item_row(entry, "basetemp", "orphan", cached, active, live_roots,
                  "no registry session owns this name")
        for entry in root_entries if basetemp.owner(entry.name, names) is None
    ]
    return {
        "root": {"path": str(root), "exists": root.is_dir()},
        "sessions": doc_sessions,
        "orphans": orphans,
        "totals": _totals([row for each in doc_sessions for row in [each["scratch"]]]
                          + [row for each in doc_sessions
                             for row in each["items"] if row["area"] == "basetemp"]
                          + orphans),
        "running": sorted(active),
        "job": job(),
        "measurable": measurable_paths(doc_sessions, orphans),
        "usage_file": str(usage_path()),
    }


def measurable_paths(doc_sessions: List[dict], orphans: List[dict],
                     session: str = "") -> List[str]:
    """The paths :func:`measure` is asked to walk for this listing.

    Scratch *entries* are left out: they live inside the scratch directory,
    which is measured whole, and walking the same bytes twice would only make
    the row look measured twice. With ``session`` only that session's paths
    are returned.
    """
    out: List[str] = []
    for each in doc_sessions:
        if session and each["name"] != session:
            continue
        if each["scratch"].get("exists"):
            out.append(each["scratch"]["path"])
        out.extend(row["path"] for row in each["items"] if row["area"] == "basetemp")
    if not session:
        out.extend(row["path"] for row in orphans)
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]
