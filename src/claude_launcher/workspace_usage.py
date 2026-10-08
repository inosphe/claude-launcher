"""Disk usage of a workspace's repository and its worktrees (claunch-2oait).

Walking a large checkout takes seconds to tens of seconds on Windows, so the
Workspaces page never waits on it: the daemon measures in a thread when asked
and keeps the last answer, with the time it was taken, in
``workspace-usage.json`` in the daemon directory -- a restart shows the old
number with its age instead of nothing.

What is measured is every checkout of the repository: the workspace's own
directory and each linked worktree (``git worktree list``), the main checkout
among them when the workspace is a linked worktree. A worktree under another
checkout's directory (``.claude/worktrees/...`` under the main checkout) is
left out of that checkout's walk and counted on its own line, so nothing is
counted twice and the lines add up to the total. Links are not followed:
a symlink or a Windows junction is the size of the link, never the size of
what it points at, which may be another checkout or the whole drive.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import atomic, reposync_flow
from .daemon.paths import daemon_dir

USAGE_FILE = "workspace-usage.json"
#: two measurements finishing together each read, change and write the file
_save_lock = threading.Lock()


def usage_path() -> Path:
    return daemon_dir() / USAGE_FILE


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    return bool(is_junction and is_junction())


def walk_size(root: str, skip: Iterable[str] = ()) -> Tuple[int, int]:
    """``(bytes, files)`` under ``root``, not following links and not
    entering any directory in ``skip``. Entries that cannot be read (removed
    mid-walk, no permission) are passed over."""
    skipped = {_key(p) for p in skip}
    total = files = 0
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            it = os.scandir(here)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if _is_link(entry):
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                    elif entry.is_dir(follow_symlinks=False):
                        if _key(entry.path) not in skipped:
                            stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
    return total, files


def _inside(child: str, parent: str) -> bool:
    c, p = _key(child), _key(parent)
    return c != p and c.startswith(p.rstrip(os.sep) + os.sep)


def measure(path: str) -> dict:
    """The workspace at ``path`` and its worktrees, one line each, and the
    total. ``main`` marks the line that is the workspace itself."""
    trees = [path] + [t["path"] for t in reposync_flow.linked_worktrees(path)
                      if not t.get("prunable") and os.path.isdir(t["path"])]
    seen: Dict[str, str] = {}
    for t in trees:
        seen.setdefault(_key(t), t)
    trees = list(seen.values())
    started = time.monotonic()
    parts: List[dict] = []
    for i, t in enumerate(trees):
        nested = [o for o in trees if _inside(o, t)]
        size, files = walk_size(t, nested)
        parts.append({"path": t, "bytes": size, "files": files, "workspace": i == 0})
    return {"bytes": sum(p["bytes"] for p in parts), "files": sum(p["files"] for p in parts),
            "parts": parts, "seconds": round(time.monotonic() - started, 2)}


def load() -> Dict[str, dict]:
    """The cache: workspace name -> the last measurement, with ``at``."""
    try:
        doc = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save(name: str, row: Optional[dict]) -> None:
    """Keep ``row`` as ``name``'s measurement (``None`` forgets it). Only the
    daemon writes this file, one measurement at a time per workspace."""
    with _save_lock:
        doc = load()
        if row is None:
            doc.pop(name, None)
        else:
            doc[name] = row
        path = usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            atomic.replace(tmp, path)
