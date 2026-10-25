"""Bounded, two-phase cleanup of a managed session's test artifacts.

Plans are server-owned and short lived. Clients supply no paths or commands.
Missing completion evidence is a deferred item, never permission to guess.
The caller must hold the exclusive measurement window while applying a plan.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time

from . import basetemp_reaper as basetemp, open_files, paths

log = logging.getLogger(__name__)


class CleanupError(ValueError):
    pass


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _plain(path: Path) -> None:
    """Reject links/reparse points in the entire absolute ancestor chain."""
    for part in (path, *path.parents):
        if _is_link(part.lstat()):
            raise CleanupError(f"link or reparse point: {part}")


def _regular(path: Path) -> bool:
    """Whether ``path`` is a regular file itself, never through a link."""
    st = path.lstat()
    return stat.S_ISREG(st.st_mode) and not _is_link(st)


def _gitdir_verdict(gitfile: Path, data: bytes, tree: Path) -> str:
    """Where the gitdir named by a nested ``.git`` file lies: "inside" or "missing".

    A gitdir inside ``tree`` goes with it, and a gitdir that no longer exists
    belongs to nothing. Anything else -- an existing gitdir outside the tree,
    reached directly or through a link, or a file that names no gitdir -- may
    be a real linked worktree of another repository and is preserved.
    """
    text = data.decode("utf-8")
    target = text[len("gitdir: "):].strip() if text.startswith("gitdir: ") else ""
    if not target or "\n" in target or "\r" in target:
        raise CleanupError(f"nested linked worktree: {gitfile.parent} (no gitdir line)")
    # Git resolves a relative gitdir against the directory holding the file.
    real = os.path.normcase(os.path.realpath(os.path.join(gitfile.parent, target)))
    root = os.path.normcase(os.path.realpath(tree))
    if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
        return "inside"
    try:
        os.lstat(real)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError as exc:
        # A gitdir that cannot be looked at is not a gitdir that is gone.
        raise CleanupError(f"nested linked worktree: {gitfile.parent} (gitdir {real}: {exc.strerror})")
    raise CleanupError(f"nested linked worktree: {gitfile.parent} (gitdir {real})")


#: How long one plan may spend fingerprinting before the rest of its items are
#: reported unmeasured. The MCP client gives up after 120s and the daemon's
#: work does not stop with it, so a plan must answer well inside that.
PLAN_BUDGET = 60.0

#: A refused apply is not the caller's to retry: the caller's turn ends and
#: nobody asks again -- which is exactly the state auto-release runs in
#: (claunch-q4cqy.2). The daemon keeps the request in the session's cleanup
#: folder and replans it when the measurement window frees up. The bounds keep
#: a schedule from becoming a silent claim on disk that never runs and never
#: gives up.
SCHEDULE_TTL = 24 * 3600.0
SCHEDULE_MAX_ATTEMPTS = 20
SCHEDULE_NAME = "scheduled.json"

#: ``_context()`` refusals that mean the session itself is gone. A scheduled run
#: that hits one of these gives up instead of retrying, because no later attempt
#: can bring the session back. Matched exactly: a refusal that merely mentions a
#: session ("invalid session name in ...") says nothing about the session being
#: over, and abandoning on it would drop a cleanup that could still run
#: (claunch-q4cqy.2 review, finding 5).
SESSION_GONE = frozenset({
    "a managed session is required",
    "unknown session",
    "cleanup is only available to a live session",
})


def _snapshot(path: Path, deadline: float | None = None) -> dict:
    _plain(path)
    # A preview worktree (a top-level .git file) keeps the strict rule: its
    # removal goes through git, and nested worktrees inside it are not ours.
    top = path / ".git"
    worktree = os.path.lexists(top) and _regular(top)
    digest = hashlib.sha256()
    size = files = 0
    base = os.fspath(path)
    for root, dirs, names in os.walk(base, followlinks=False, onerror=_walk_error):
        if deadline is not None and time.monotonic() > deadline:
            raise CleanupError("not measured within the plan's time budget; request a new plan")
        # Relative names by slicing: Path.relative_to per entry was most of
        # the plan's CPU time on large basetemps (claunch-30rkn).
        prefix = "" if root == base else root[len(base) + 1:] + os.sep
        dirs.sort()
        for name in sorted(dirs + names):
            item = os.path.join(root, name)
            relative = prefix + name
            st = os.lstat(item)
            if _is_link(st):
                # A link inside the tree is unlinked, never followed: only the
                # link and its target string are evidence, so a retargeted link
                # changes the fingerprint. os.walk enters a Windows junction
                # unless it is pruned here.
                if name in dirs:
                    dirs.remove(name)
                digest.update(repr((relative, "link", st.st_ino,
                                    st.st_mtime_ns, os.readlink(item))).encode())
                continue
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise CleanupError(f"special file: {item}")
            if name == ".git" and stat.S_ISREG(st.st_mode) and root != base:
                if worktree:
                    raise CleanupError(f"nested linked worktree: {root}")
                # The verdict and the file's bytes are evidence: a gitdir
                # rewritten after the plan changes the fingerprint even when
                # size and mtime are restored, and apply re-judges it.
                gitfile = Path(item)
                data = gitfile.read_bytes()
                digest.update(repr((relative, "gitdir",
                                    _gitdir_verdict(gitfile, data, path), data)).encode())
            digest.update(repr((relative, st.st_mode, st.st_ino,
                                st.st_size, st.st_mtime_ns)).encode())
            if stat.S_ISREG(st.st_mode):
                files += 1
                size += st.st_size
    st = path.stat()
    digest.update(repr((st.st_dev, st.st_ino, st.st_mtime_ns)).encode())
    return {"fingerprint": digest.hexdigest(), "bytes": size, "files": files}


def _walk_error(error):
    raise error


def _unlink_links(path: Path) -> None:
    """Remove every link under ``path`` -- the link itself, never its target.

    Explicit, so deletion does not rest on rmtree declining to follow a link.
    A Windows directory link or junction is removed with rmdir, which takes
    the reparse point and leaves the target's contents.
    """
    for root, dirs, names in os.walk(path, followlinks=False, onerror=_walk_error):
        for name in [*dirs, *names]:
            item = os.path.join(root, name)
            st = os.lstat(item)
            if not _is_link(st):
                continue
            if name in dirs:
                dirs.remove(name)
            if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY:
                os.rmdir(item)
            else:
                os.unlink(item)


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


def _registered_worktrees(repo: Path) -> set:
    """Resolved paths of ``repo``'s linked worktrees; empty if git cannot say.

    Only widens what a plan looks at: every listed path is still judged by
    ``Cleanup._item`` before anything is removed.
    """
    try:
        listing = _git(repo, "worktree", "list", "--porcelain", "-z")
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    return {Path(x[9:]).resolve() for x in listing.split("\0") if x.startswith("worktree ")}


def _scratch_artifact(path: Path, registered: set) -> bool:
    """Whether a scratch entry is a test artifact whatever its name.

    The name rule alone missed nearly every preview worktree sessions made
    (`preview-r2`, `bt-preview`; claunch-30rkn): a registered worktree, a
    completed-run receipt, or a pytest ``-n`` basetemp (``popen-gw*``) is
    listed so the plan shows it, as ready or with the reason it stays.
    """
    try:
        if _is_link(path.lstat()) or not path.is_dir():
            return False
        if path.resolve() in registered or os.path.lexists(path / basetemp.RUN_SUMMARY):
            return True
        return any(entry.name.startswith("popen-gw") for entry in os.scandir(path))
    except OSError:
        return False


def scratch_candidate(path: Path, session: str, registered: set) -> bool:
    """Whether a scratch entry is one cleanup would consider.

    The single copy of the rule: the Artifacts page lists what a plan would
    take, so it asks this function rather than restating the names
    (claunch-bgs76.1). Two copies had already been written once, and a rule
    that only one of them follows makes the page and the plan disagree about
    the same directory.
    """
    if (path.name.startswith("pytest-") or path.name == "browser"
            or (path.name.startswith(session + "-") and "preview" in path.name)):
        return True
    return _scratch_artifact(path, registered)


class Cleanup:
    def __init__(self, manager):
        self.manager = manager
        self.plans = {}
        # Plans of different sessions may now run at once (claunch-30rkn).
        self._plans_lock = threading.Lock()

    def _context(self, session):
        if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
            raise CleanupError("a managed session is required")
        current = next((s for s in self.manager.list() if s.sdef.name == session), None)
        if current is None:
            raise CleanupError("unknown session")
        if current.exited or getattr(current, "held_at", None):
            raise CleanupError("cleanup is only available to a live session")
        return Path(current.sdef.cwd), paths.session_scratch_dir(session), basetemp.default_root()

    def _candidates(self, session, scratch, root, repo):
        sessions = self.manager.list()
        names = [s.sdef.name for s in sessions]
        foreign = _foreign_names()
        registered = _registered_worktrees(repo) if scratch.exists() else set()
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
                    if not scratch_candidate(path, session, registered):
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

    def _item(self, path, repo, reserved="", deadline=None):
        row = {"path": str(path), "status": "cleanup_pending"}
        try:
            if reserved:
                raise CleanupError(reserved)
            row.update(_snapshot(path, deadline))
            if not path.is_dir():
                raise CleanupError("not a directory")
            git = path / ".git"
            if git.exists() and _regular(git):
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
                head = _git(path, "rev-parse", "HEAD").strip()
                preserving_refs = _git(repo, "for-each-ref", "--format=%(refname)",
                                       "--contains", head, "refs/").splitlines()
                if not preserving_refs:
                    raise CleanupError("detached HEAD has no preserving persistent ref")
                row.update(kind="worktree", head=head, preserving_refs=preserving_refs,
                           retained_refs="all preview refs are preserved")
            else:
                receipt = path / basetemp.RUN_SUMMARY
                if not _regular(receipt):
                    # A linked receipt could vouch for a run this tree never had.
                    raise CleanupError("test run receipt is not a regular file")
                summary = json.loads(receipt.read_text(encoding="utf-8"))
                if not isinstance(summary, dict) or not summary.get("finished_at") or type(summary.get("exit_code")) is not int:
                    raise CleanupError("no completed test run receipt")
                start = datetime.fromisoformat(str(summary.get("started_at", "")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(summary["finished_at"]).replace("Z", "+00:00"))
                if start.tzinfo is None or end.tzinfo is None or end < start:
                    raise CleanupError("invalid test run times")
                row.update(kind="test", run=summary, seconds=(end - start).total_seconds())
            row["status"] = "ready"
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # "Too many open files" on its own leaves the reader guessing
            # whether this daemon was capped or the machine ran out; the
            # process's own limit answers that (claunch-wo1zj.1).
            row["reason"] = str(exc) + open_files.exhaustion_note(exc)
        return row

    def plan(self, session, budget=PLAN_BUDGET):
        repo, scratch, root = self._context(session)
        deadline = time.monotonic() + budget
        token = secrets.token_hex(16)
        items = [self._item(p, repo, why, deadline)
                 for p, why in self._candidates(session, scratch, root, repo)]
        now = time.time()
        plan = {"plan_id": token, "session": session, "created_at": now, "items": items}
        standing = self._schedule_record(session)
        if standing:
            # A scheduler is already waiting for the window; a caller that sees
            # this reports it instead of scheduling its own retry
            # (claunch-q4cqy.2 review, finding 3).
            plan["scheduled"] = {key: standing.get(key) for key in
                                 ("created_at", "expires_at", "attempts", "last_reason")}
        with self._plans_lock:
            self.plans = {k: v for k, v in self.plans.items() if now - v["created_at"] < 600}
            # One outstanding plan per session, bounded by the session registry.
            self.plans = {k: v for k, v in self.plans.items() if v["session"] != session}
            self.plans[token] = plan
        return plan

    # ---- scheduled applies -------------------------------------------------- #

    def _schedule_file(self, session) -> Path:
        return paths.session_dir(session) / "cleanup" / SCHEDULE_NAME

    def _schedule_record(self, session):
        """The schedule waiting for this session, or None.

        A record that cannot be read is treated as absent: the caller still has
        the plan, and a corrupt file must not stop a later apply from being
        scheduled again.
        """
        target = self._schedule_file(session)
        try:
            if not _regular(target):
                return None
            record = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("session") != session:
            return None
        return record

    def scheduled_record(self, session):
        """The cleanup already waiting for this session, or None.

        Callers use this to say "already scheduled" instead of scheduling a
        second time; the record itself is what the clock runs.
        """
        return self._schedule_record(session)

    def _write_schedule(self, record) -> None:
        folder = self._schedule_file(record["session"]).parent
        folder.mkdir(exist_ok=True)
        _plain(folder)
        target = folder / SCHEDULE_NAME
        temporary = folder / (SCHEDULE_NAME + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def schedule(self, session, reason):
        """Remember that this session's cleanup must run when the window frees.

        Scheduling again keeps the first clock and the attempt count; only the
        reason is refreshed. A caller that retries its own apply therefore does
        not reset the bound that eventually gives up.

        A record that has already given up -- expired or out of attempts -- is
        not inherited: handing its clock to a new request would make the answer
        "scheduled" for a request the daemon will never run (claunch-q4cqy.2
        review, finding 1).
        """
        self._context(session)  # a schedule belongs to a live session
        now = time.time()
        previous = self._schedule_record(session)
        spent = (not previous
                 or float(previous.get("expires_at") or 0) <= now
                 or int(previous.get("attempts") or 0) >= SCHEDULE_MAX_ATTEMPTS)
        record = {
            "session": session,
            "created_at": now if spent else previous["created_at"],
            "expires_at": now + SCHEDULE_TTL if spent else previous["expires_at"],
            "attempts": 0 if spent else int(previous.get("attempts") or 0),
            "last_reason": str(reason or ""),
        }
        self._write_schedule(record)
        return record

    def unschedule(self, session, why="") -> None:
        """Drop the schedule; ``why`` stays in the log, not on disk."""
        target = self._schedule_file(session)
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        if why:
            log.info("cleanup: schedule for %s dropped (%s)", session, why)

    def scheduled(self):
        """Live sessions whose cleanup is waiting for the window.

        A record past its TTL is dropped here: nothing else walks this folder,
        and ``schedule()`` treats an expired record as absent, so leaving the
        file would only keep a dead request on disk (claunch-q4cqy.2 review,
        finding 2).
        """
        names = []
        now = time.time()
        for current in self.manager.list():
            if current.exited or getattr(current, "held_at", None):
                continue  # the exit funnel's reaper owns an ended session
            name = current.sdef.name
            record = self._schedule_record(name)
            if not record:
                continue
            if float(record.get("expires_at") or 0) <= now:
                self.unschedule(name, "expired")
                continue
            names.append(name)
        return names

    def run_scheduled(self, session):
        """Plan and apply again, now that the caller holds the window.

        Whatever remains after this run is what a re-plan would produce again --
        a missing receipt, a changed tree -- so the schedule is dropped either
        way. Repeating it would only spend the window.
        """
        record = self._schedule_record(session)
        if not record:
            return {"status": "none", "session": session}
        if (float(record.get("expires_at") or 0) <= time.time()
                or int(record.get("attempts") or 0) >= SCHEDULE_MAX_ATTEMPTS):
            self.unschedule(session, "expired")
            return {"status": "expired", "session": session,
                    "attempts": record.get("attempts")}
        record["attempts"] = int(record.get("attempts") or 0) + 1
        try:
            self._write_schedule(record)
            plan = self.plan(session)
            document = self.apply(session, plan["plan_id"])
        except CleanupError as exc:
            if str(exc) in SESSION_GONE:
                # Ended, unknown or not live: the exit funnel's reaper owns it
                # now, and retrying cannot bring the session back.
                self.unschedule(session, str(exc))
                return {"status": "abandoned", "session": session,
                        "reason": str(exc)}
            record["last_reason"] = str(exc)
            self._write_schedule(record)
            return {"status": "cleanup_pending", "session": session,
                    "reason": str(exc)}
        self.unschedule(session, "ran")
        return {"status": "ran", "session": session,
                "removed": document.get("removed", 0),
                "cleanup_pending": document.get("cleanup_pending", 0),
                "receipt": document.get("receipt")}

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
        with self._plans_lock:
            plan = self.plans.get(token)
            if not plan or plan["session"] != session or time.time() - plan["created_at"] >= 600:
                raise CleanupError("unknown, expired or foreign plan; request a new plan")
            self.plans.pop(token)
        # Recompute ownership and candidates before any deletion.
        allowed = {str(p): why for p, why in self._candidates(session, scratch, root, repo)}
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
                    _unlink_links(path)
                    # Read-only git fixture objects are a recorded failure, not
                    # a reason to change permissions or retry through another tool.
                    shutil.rmtree(path)
                row["status"] = "removed"
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row.update(status="cleanup_pending",
                           reason=str(exc) + open_files.exhaustion_note(exc))
            self._save(document)  # stop on recording failure before another item
        document["removed"] = sum(r["status"] == "removed" for r in document["items"])
        document["cleanup_pending"] = sum(r["status"] != "removed" for r in document["items"])
        self._save(document)
        # A direct apply served whatever the schedule was waiting for; leaving
        # it would have the clock replan work that just ran.
        if self._schedule_record(session):
            self.unschedule(session, "applied directly")
        return document
