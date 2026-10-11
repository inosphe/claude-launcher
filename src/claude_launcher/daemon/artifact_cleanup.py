"""Bounded, two-phase cleanup of a managed session's test artifacts.

Plans are server-owned and short lived. Clients supply no paths or commands.
Missing completion evidence is a deferred item, never permission to guess.
The caller must hold the exclusive measurement window while applying a plan.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import time

from . import basetemp_reaper as basetemp, paths


class CleanupError(ValueError):
    pass


def _plain(path: Path) -> None:
    """Reject links/reparse points in the entire absolute ancestor chain."""
    for part in (path, *path.parents):
        st = part.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise CleanupError(f"link or reparse point: {part}")


def _snapshot(path: Path) -> dict:
    _plain(path)
    digest = hashlib.sha256()
    size = files = 0
    for root, dirs, names in os.walk(path, followlinks=False, onerror=_walk_error):
        dirs.sort()
        for name in sorted(dirs + names):
            item = Path(root) / name
            st = item.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
                raise CleanupError(f"link or reparse point: {item}")
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise CleanupError(f"special file: {item}")
            if name == ".git" and item.is_file() and item.parent != path:
                raise CleanupError(f"nested linked worktree: {item.parent}")
            digest.update(repr((str(item.relative_to(path)), st.st_mode, st.st_ino,
                                st.st_size, st.st_mtime_ns)).encode())
            if item.is_file():
                files += 1
                size += st.st_size
    st = path.stat()
    digest.update(repr((st.st_dev, st.st_ino, st.st_mtime_ns)).encode())
    return {"fingerprint": digest.hexdigest(), "bytes": size, "files": files}


def _walk_error(error):
    raise error


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode:
        raise CleanupError(result.stderr.strip() or f"git exited {result.returncode}")
    return result.stdout


def _foreign_names() -> set:
    """Conservatively reserve all other instances' names; unreadable = stop."""
    own = paths.daemon_dir().resolve()
    base = paths.base_home()
    instances = [base / "daemon"]
    if (base / "daemons").exists():
        instances.extend((base / "daemons").iterdir())
    names = set()
    for directory in instances:
        if not directory.is_dir() or directory.resolve() == own:
            continue
        db = directory / "sessions.db"
        if db.exists():
            with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
                records = [json.loads(row[0]) for row in conn.execute("SELECT data FROM sessions")]
        elif (directory / "sessions.json").exists():
            records = json.loads((directory / "sessions.json").read_text(encoding="utf-8"))
        else:
            continue
        if not isinstance(records, list):
            raise CleanupError(f"unreadable registry: {directory}")
        for record in records:
            name = record["def"]["name"]
            if not isinstance(name, str) or not name:
                raise CleanupError(f"invalid session name in {directory}")
            names.add(name)
    return names


class Cleanup:
    def __init__(self, manager):
        self.manager = manager
        self.plans = {}

    def _context(self, session):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            raise CleanupError("a managed session is required")
        current = next((s for s in self.manager.list() if s.sdef.name == session), None)
        if current is None:
            raise CleanupError("unknown session")
        if current.exited or getattr(current, "held_at", None):
            raise CleanupError("cleanup is only available to a live session")
        return Path(current.sdef.cwd), paths.session_scratch_dir(session), basetemp.default_root()

    def _candidates(self, session, scratch, root):
        sessions = self.manager.list()
        names = [s.sdef.name for s in sessions]
        foreign = _foreign_names()
        result = []
        for directory, area in ((root, "basetemp"), (scratch, "scratch")):
            if not directory.exists():
                continue
            _plain(directory)
            for path in sorted(directory.iterdir()):
                if area == "basetemp":
                    if basetemp.owner(path.name, names) != session:
                        continue
                    reserved = any(basetemp.matches(path.name, name) for name in foreign)
                else:
                    if not (path.name.startswith("pytest-") or path.name == "browser"
                            or path.name.startswith(session + "-") and "preview" in path.name):
                        continue
                    reserved = False
                reason = "other daemon owns matching name" if reserved else ""
                for live in sessions:
                    if live.exited and not getattr(live, "held_at", None):
                        continue
                    cwd = Path(live.sdef.cwd).resolve()
                    target = path.resolve()
                    if cwd == target or target in cwd.parents:
                        reason = "a live or held session uses this directory"
                result.append((path, reason))
        return result

    def _item(self, path, repo, reserved=""):
        row = {"path": str(path), "status": "cleanup_pending"}
        try:
            if reserved:
                raise CleanupError(reserved)
            row.update(_snapshot(path))
            if not path.is_dir():
                raise CleanupError("not a directory")
            if (path / ".git").is_file():
                # Only detached, clean, registered preview worktrees. Never force,
                # prune other registrations, or remove an integration proof ref.
                listing = _git(repo, "worktree", "list", "--porcelain", "-z")
                registered = {Path(x[9:]).resolve() for x in listing.split("\0")
                              if x.startswith("worktree ")}
                if path.resolve() not in registered:
                    raise CleanupError("worktree is not registered in this repository")
                if _git(path, "status", "--porcelain", "--untracked-files=all", "--ignored"):
                    raise CleanupError("worktree has changed, untracked or ignored files")
                result = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"],
                                        capture_output=True, timeout=30)
                if result.returncode != 1:
                    raise CleanupError("preview must have a detached HEAD")
                row.update(kind="worktree", head=_git(path, "rev-parse", "HEAD").strip(),
                           retained_refs="all preview refs are preserved")
            else:
                summary = json.loads((path / basetemp.RUN_SUMMARY).read_text(encoding="utf-8"))
                if not isinstance(summary, dict) or not summary.get("finished_at") or type(summary.get("exit_code")) is not int:
                    raise CleanupError("no completed test run receipt")
                start = datetime.fromisoformat(str(summary.get("started_at", "")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(summary["finished_at"]).replace("Z", "+00:00"))
                if start.tzinfo is None or end.tzinfo is None or end < start:
                    raise CleanupError("invalid test run times")
                row.update(kind="test", run=summary, seconds=(end - start).total_seconds())
            row["status"] = "ready"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            row["reason"] = str(exc)
        return row

    def plan(self, session):
        repo, scratch, root = self._context(session)
        now = time.time()
        self.plans = {k: v for k, v in self.plans.items() if now - v["created_at"] < 600}
        # One outstanding plan per session, bounded by the session registry.
        self.plans = {k: v for k, v in self.plans.items() if v["session"] != session}
        token = secrets.token_hex(16)
        plan = {"plan_id": token, "session": session, "created_at": now,
                "items": [self._item(p, repo, why) for p, why in self._candidates(session, scratch, root)]}
        self.plans[token] = plan
        return plan

    def _save(self, document):
        folder = paths.session_dir(document["session"]) / "cleanup"
        folder.mkdir(exist_ok=True)
        _plain(folder)
        target = folder / (document["plan_id"] + ".json")
        temporary = folder / (document["plan_id"] + ".tmp")
        # A unique plan id; no symlink or existing temporary may be followed.
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return str(target)

    def apply(self, session, token):
        repo, scratch, root = self._context(session)
        plan = self.plans.get(token)
        if not plan or plan["session"] != session or time.time() - plan["created_at"] >= 600:
            raise CleanupError("unknown, expired or foreign plan; request a new plan")
        self.plans.pop(token)
        # Recompute ownership and candidates before any deletion.
        allowed = {str(p): why for p, why in self._candidates(session, scratch, root)}
        document = {**plan, "items": [dict(row) for row in plan["items"]]}
        document["receipt"] = self._save(document)  # failure prevents every deletion
        for row in document["items"]:
            if row["status"] != "ready":
                continue
            path = Path(row["path"])
            try:
                if str(path) not in allowed:
                    raise CleanupError("ownership or candidate changed")
                current = self._item(path, repo, allowed[str(path)])
                if (current["status"] != "ready" or current.get("fingerprint") != row["fingerprint"]
                        or current.get("head") != row.get("head")):
                    raise CleanupError(current.get("reason", "artifact changed since plan"))
                row["status"] = "deleting"
                self._save(document)  # intent and evidence survive a crash
                if row["kind"] == "worktree":
                    _git(repo, "worktree", "remove", str(path))
                else:
                    import shutil
                    # Read-only git fixture objects are a recorded failure, not
                    # a reason to change permissions or retry through another tool.
                    shutil.rmtree(path)
                row["status"] = "removed"
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row.update(status="cleanup_pending", reason=str(exc))
            self._save(document)  # stop on recording failure before another item
        document["removed"] = sum(r["status"] == "removed" for r in document["items"])
        document["cleanup_pending"] = sum(r["status"] != "removed" for r in document["items"])
        self._save(document)
        return document
