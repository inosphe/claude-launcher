"""Durable, per-merge verification jobs, independent of a leader's round.

The test-window queue is transient resource arbitration. This SQLite queue
keeps the work, immutable refs, attempts and repository pause across restarts.
Only a green repair containing the failed merge resumes normal admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], encoding="utf-8", errors="replace"
    ).strip()


def default_database():
    from .daemon import paths
    return paths.daemon_dir() / "delayed-sweeps.sqlite3"


class Queue:
    def __init__(self, database=None):
        self.database = Path(database or default_database())
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, repo TEXT NOT NULL, common TEXT NOT NULL,
                    sha TEXT NOT NULL, ref TEXT NOT NULL, issue TEXT NOT NULL,
                    owner TEXT NOT NULL, tool TEXT NOT NULL, python TEXT NOT NULL, command TEXT,
                    status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT -1,
                    created REAL NOT NULL, available REAL NOT NULL DEFAULT 0,
                    pid INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    receipt TEXT, detail TEXT NOT NULL DEFAULT '',
                    notified INTEGER NOT NULL DEFAULT 0, UNIQUE(common, sha)
                );
                CREATE TABLE IF NOT EXISTS pauses (
                    common TEXT PRIMARY KEY, job TEXT NOT NULL,
                    repair_issue TEXT, cleared_sha TEXT
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.database), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def common(repo):
        return os.path.normcase(str(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()))

    def enqueue(self, repo, sha, issue, owner, tool, command=None):
        repo = str(Path(repo).resolve())
        common = self.common(repo)
        commit = git(repo, "rev-parse", f"{sha}^{{commit}}")
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or commit != sha:
            raise ValueError("enqueue requires a full merge SHA")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
            raise ValueError("invalid owner session")
        if len(git(repo, "show", "-s", "--format=%P", sha).split()) != 2:
            raise ValueError("verification checkpoint must be a two-parent merge")
        ident = hashlib.sha256((common + ":" + sha).encode()).hexdigest()[:24]
        ref = f"refs/heads/delayed-sweep/{owner}/{sha}"
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs "
                       "(id,repo,common,sha,ref,issue,owner,tool,python,command,status,created) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
                       (ident, repo, common, sha, ref, issue, owner,
                        str(Path(tool).resolve()), sys.executable, command, time.time()))
            row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())
            if row["issue"] != issue:
                raise ValueError("the merge already belongs to a different verification issue")
            repair = db.execute("SELECT repair_issue FROM pauses WHERE common=?", (common,)).fetchone()
            if repair and repair[0] == issue:
                db.execute("UPDATE jobs SET priority=0 WHERE id=?", (ident,))
        self.prepare(ident)
        return self.get(ident)

    def prepare(self, ident):
        row = self.get(ident)
        # CAS: repeated enqueue may repair a crash between the DB and the ref.
        proc = subprocess.run(["git", "-C", row["repo"], "update-ref", row["ref"],
                               row["sha"], "0" * 40], capture_output=True)
        if proc.returncode and git(row["repo"], "rev-parse", row["ref"]) != row["sha"]:
            raise ValueError("verification checkpoint ref moved")
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE id=? AND status='pending'", (ident,))

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            return dict(row)

    def status(self, repo=None):
        common = self.common(repo) if repo else None
        with self.connect() as db:
            jobs = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created,id")
                    if common is None or r["common"] == common]
            pauses = [dict(r) for r in db.execute("SELECT * FROM pauses")
                      if common is None or r["common"] == common]
        return {"jobs": jobs, "pauses": pauses}

    def admit(self, repo, issue):
        common = self.common(repo)
        with self.connect() as db:
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (common,)).fetchone()
        if pause and pause["job"] and pause["repair_issue"] != issue:
            raise ValueError(f"repository paused by {pause['job']}; only repair issue "
                             f"{pause['repair_issue'] or '(not delegated yet)'} may merge")

    def repair(self, repo, job, issue):
        common = self.common(repo)
        with self.connect() as db:
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (common,)).fetchone()
            if not pause or pause["job"] != job:
                raise ValueError("repair must name the active failed job")
            if pause["repair_issue"] and pause["repair_issue"] != issue:
                raise ValueError("a repair issue is already assigned")
            db.execute("UPDATE pauses SET repair_issue=? WHERE common=?", (issue, common))

    def claim(self, *, background=True):
        from .daemon.window import pid_alive
        for row in self.status()["jobs"]:
            if row["status"] == "pending":
                try:
                    self.prepare(row["id"])
                except (ValueError, OSError, subprocess.CalledProcessError) as exc:
                    self.finish(row["id"], "retry", detail=str(exc))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            background_running = False
            for row in db.execute("SELECT * FROM jobs WHERE status='running'").fetchall():
                live = row["pid"] and pid_alive(row["pid"])
                spawning = not row["pid"] and row["available"] > time.time()
                if live or spawning:
                    if row["priority"] >= 0:
                        return None
                    # A repair may join the window ahead of a waiting background
                    # runner. The window still prevents concurrent sweeps.
                    background_running = True
                    continue
                db.execute("UPDATE jobs SET status='retry',pid=0,detail='runner interrupted' WHERE id=?", (row["id"],))
            row = db.execute("""SELECT j.* FROM jobs j LEFT JOIN pauses p ON p.common=j.common
                WHERE j.status IN ('queued','retry') AND j.available<=?
                AND (j.priority>=0 OR ?)
                AND (p.job IS NULL OR p.job='' OR p.repair_issue=j.issue)
                ORDER BY j.priority DESC,j.created,j.id LIMIT 1""",
                (time.time(), background and not background_running)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',pid=0,available=?,attempts=attempts+1 WHERE id=?",
                       (time.time() + 60, row["id"]))
            return dict(row)

    def spawned(self, ident, pid):
        with self.connect() as db:
            db.execute("UPDATE jobs SET pid=? WHERE id=? AND status='running'", (pid, ident))

    def finish(self, ident, status, receipt=None, detail=""):
        if status not in ("passed", "failed", "retry"):
            raise ValueError("invalid verification result")
        row = self.get(ident)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status=?,receipt=?,detail=?,pid=0,available=?,notified=0 WHERE id=?",
                       (status, receipt, detail, time.time() + 60 if status == "retry" else 0, ident))
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (row["common"],)).fetchone()
            if status == "failed":
                # A late verdict about pre-repair history must not pause it again.
                covered = pause and pause["cleared_sha"] and self.contains(row["repo"], row["sha"], pause["cleared_sha"])
                if not covered and (not pause or not pause["job"]):
                    db.execute("INSERT INTO pauses(common,job) VALUES (?,?) ON CONFLICT(common) "
                               "DO UPDATE SET job=excluded.job,repair_issue=NULL", (row["common"], ident))
            elif status == "passed" and pause and pause["job"] and pause["repair_issue"] == row["issue"]:
                failed = self.get(pause["job"])
                if self.contains(row["repo"], failed["sha"], row["sha"]):
                    db.execute("UPDATE pauses SET job='',repair_issue=NULL,cleared_sha=? WHERE common=?",
                               (row["sha"], row["common"]))

    @staticmethod
    def contains(repo, ancestor, head):
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, head],
                              capture_output=True).returncode == 0

    def notified(self, ident):
        with self.connect() as db:
            db.execute("UPDATE jobs SET notified=1 WHERE id=?", (ident,))


def run_one(queue, ident, scratch):
    """Run against a detached, clean checkout; infrastructural failures retry."""
    import importlib.util
    import shlex
    from types import SimpleNamespace
    row = queue.get(ident)
    if row["status"] != "running":
        raise ValueError("job has not been claimed")
    root = Path(scratch) / ("delayed-sweep-" + ident) / str(row["attempts"])
    checkout = root / "checkout"
    root.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("queued_sweep", row["tool"])
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    try:
        git(row["repo"], "worktree", "add", "--detach", str(checkout), row["sha"])
        if git(checkout, "rev-parse", row["ref"]) != row["sha"]:
            raise ValueError("immutable checkpoint ref changed")
        # Install the snapshot's dependencies and plugins in its own venv;
        # pytest's repository pythonpath imports the checked-out sources.
        command = row["command"]
        if not command:
            env = dict(os.environ)
            env.pop("UV_PROJECT_ENVIRONMENT", None)
            env.pop("VIRTUAL_ENV", None)
            subprocess.run(["uv", "sync", "--extra", "test"], cwd=checkout,
                           env=env, check=True)
            python = checkout / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not python.exists():
                raise ValueError("snapshot test environment has no Python interpreter")
            interpreter = (subprocess.list2cmdline([str(python), "-m", "pytest"])
                           if os.name == "nt" else shlex.join([str(python), "-m", "pytest"]))
            command = sweep.default_command(row["owner"] + "-" + ident).replace(
                "uv run --no-sync pytest", interpreter, 1)
        args = SimpleNamespace(repo=checkout, branch=row["ref"], command=command,
                               receipts=None, allow_dirty=False, priority=row["priority"])
        from datetime import datetime, timezone
        started = datetime.now(timezone.utc).isoformat()
        code = sweep.cmd_run(args)
        receipt = sweep.receipt_path(checkout, row["sha"])
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
        counts = data.get("counts") or {}
        fresh = data.get("started_at", "") >= started and data.get("commit") == row["sha"]
        if fresh and counts and code == 0 and sweep.is_green(data):
            queue.finish(ident, "passed", str(receipt))
        elif fresh and code == 1 and (counts.get("failed") or counts.get("error")):
            queue.finish(ident, "failed", str(receipt), json.dumps(data.get("failures") or []))
        else:
            queue.finish(ident, "retry", str(receipt) if receipt.exists() else None,
                         f"sweep execution exit {code}; no regression verdict")
    except Exception as exc:
        queue.finish(ident, "retry", detail=str(exc))
    finally:
        # Remove only this runner's checkout; logs and receipts stay available.
        if checkout.exists():
            subprocess.run(["git", "-C", row["repo"], "worktree", "remove", str(checkout)], capture_output=True)
