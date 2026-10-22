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

#: Runner-reported retries a job gets before it is parked as ``stuck``. Each
#: one waits twice as long as the last (60 s, 120 s, ... up to RETRY_MAX_DELAY).
#: Without a limit a deterministic failure -- a basetemp the sweep cannot clear,
#: a command the checkout cannot run -- retried every minute for days and, being
#: the oldest rows, was claimed ahead of every queued job (claunch-ersj2).
RETRY_LIMIT = 6
RETRY_BASE_DELAY = 60.0
RETRY_MAX_DELAY = 3600.0
#: Same-SHA remeasures a failed pause job gets: a pass clears the pause as an
#: intermittent failure, a second failure is a real regression for the repair path.
REMEASURE_LIMIT = 1
#: A runner that sees its sweep fail measures the same SHA once more before the
#: failure pauses the repository (``finish(auto_remeasure=True)``). Over the
#: queue's history most failures named a test that failed again on other SHAs, so
#: a pause was taken for an intermittent test; a pass the second time is recorded
#: as intermittent and pauses nothing.
AUTO_REMEASURE_LIMIT = 1
#: Merges one sweep covers, and how long the oldest queued merge waits for company
#: before it is swept alone. Checkpoints are cumulative (A, A+B, A+B+C), so the
#: newest one in a batch is the tree that contains all of them: one sweep answers
#: for the batch, and a failure is narrowed afterwards with ``unbatch``.
BATCH_SIZE = 5
BATCH_SETTLE = 600.0
#: Results the originating session is told about.
NOTIFIED = ("passed", "failed", "stuck")


def retry_delay(failures):
    return min(RETRY_BASE_DELAY * 2 ** max(0, failures - 1), RETRY_MAX_DELAY)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], encoding="utf-8", errors="replace"
    ).strip()


def default_database():
    from .daemon import paths
    return paths.base_home() / "daemon" / "delayed-sweeps.sqlite3"


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
                    origin TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT -1,
                    created REAL NOT NULL, available REAL NOT NULL DEFAULT 0,
                    pid INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    receipt TEXT, detail TEXT NOT NULL DEFAULT '',
                    notified INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    remeasures INTEGER NOT NULL DEFAULT 0, history TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(common, sha)
                );
                CREATE TABLE IF NOT EXISTS pauses (
                    common TEXT PRIMARY KEY, job TEXT NOT NULL,
                    repair_issue TEXT, cleared_sha TEXT
                );
            """)
            # Preserve pre-release queue files without inventing their origin.
            db.execute("BEGIN IMMEDIATE")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
            if "origin" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")
            if "failures" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")
            if "remeasures" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN remeasures INTEGER NOT NULL DEFAULT 0")
            if "history" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
            # Batching: ``covered_by`` names the tip job whose sweep answers for this
            # merge; ``formed`` marks a job that left the batching pool (a batch tip,
            # or a job swept alone); ``child`` is the session watching the tip;
            # ``bisect_of`` is the failed tip a member is swept alone to narrow.
            for name, ddl in (("covered_by", "TEXT"), ("formed", "INTEGER NOT NULL DEFAULT 0"),
                              ("child", "TEXT NOT NULL DEFAULT ''"), ("bisect_of", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

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
        from . import config
        from .daemon import paths
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
        origin = json.dumps({"instance": paths.instance(),
                             "home": str(config.launcher_home().resolve()),
                             "config": str(config.sync_file().resolve()),
                             "base": str(paths.base_home().resolve()),
                             "scratch": str(paths.session_scratch_dir(owner).resolve())})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Every ordering here is by ``created``. The wall clock can step back
            # (VM time sync), which would put a later enqueue ahead of earlier ones
            # and change which merge a batch ends on; never stamp below the newest.
            newest = db.execute("SELECT MAX(created) FROM jobs").fetchone()[0] or 0
            db.execute("INSERT OR IGNORE INTO jobs "
                       "(id,repo,common,sha,ref,issue,owner,tool,python,command,origin,status,created) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                       (ident, repo, common, sha, ref, issue, owner,
                        str(Path(tool).resolve()), sys.executable, command, origin,
                        max(time.time(), newest)))
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
            jobs = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created,rowid")
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

    def dispatch(self, size=BATCH_SIZE, settle=BATCH_SETTLE, now=None):
        """Group queued background merges of one repository into batches.

        A group is ripe when it holds ``size`` merges or its oldest has waited
        ``settle`` seconds. The newest merge of a group becomes the batch tip
        (``formed``); the older merges it contains become ``covered`` by it and
        are not swept unless ``unbatch`` releases them. A merge the tip does not
        contain stays in the pool for a later batch. Repairs (priority >= 0)
        and retries are never batched. Returns the tips formed by this call."""
        now = time.time() if now is None else now
        tips = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM jobs WHERE status='queued' AND formed=0 AND priority<0 "
                              "AND covered_by IS NULL ORDER BY created,rowid").fetchall()
            groups = {}
            for row in rows:
                groups.setdefault(row["common"], []).append(row)
            for common, group in groups.items():
                while group:
                    chunk = group[:size]
                    if len(chunk) < size and now - chunk[0]["created"] < settle:
                        break
                    tip = chunk[-1]
                    members = [r for r in chunk[:-1] if self.contains(tip["repo"], r["sha"], tip["sha"])]
                    for member in members:
                        db.execute("UPDATE jobs SET status='covered',covered_by=?,formed=1 WHERE id=?",
                                   (tip["id"], member["id"]))
                    db.execute("UPDATE jobs SET formed=1 WHERE id=?", (tip["id"],))
                    tips.append(tip["id"])
                    taken = {r["id"] for r in members} | {tip["id"]}
                    group = [r for r in group if r["id"] not in taken]
        return [self.get(t) for t in tips]

    def members(self, ident):
        """Jobs a batch tip answers for."""
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM jobs WHERE covered_by=? ORDER BY created,rowid", (ident,))]

    def unattended(self):
        """Batch tips that still wait for a verdict and have no watching session."""
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM jobs WHERE formed=1 AND priority<0 AND child='' AND covered_by IS NULL "
                "AND status IN ('queued','retry','running') ORDER BY created,rowid")]

    def attach_child(self, ident, child):
        with self.connect() as db:
            db.execute("UPDATE jobs SET child=? WHERE id=?", (child, ident))

    def unbatch(self, job):
        """Release the merges a failed, stuck or cancelled tip covered.

        Each becomes a queued job swept alone (``bisect_of`` lets it run while
        the repository is paused by the tip), so the first one that fails is the
        merge that broke the batch."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            if row["status"] not in ("failed", "stuck", "cancelled"):
                raise ValueError(f"only a failed, stuck or cancelled tip is unbatched, not {row['status']}")
            freed = db.execute("UPDATE jobs SET status='queued',covered_by=NULL,bisect_of=?,available=0 "
                               "WHERE covered_by=? AND status='covered'", (job, job)).rowcount
        return {"job": job, "released": freed}

    def claim(self, *, background=True, batch=None):
        """Take the next job to run. ``batch`` is ``(size, settle)`` or None: with it,
        background jobs are grouped first and only formed batch tips run."""
        from .daemon.window import pid_alive
        if batch:
            self.dispatch(*batch)
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
                AND (j.formed=1 OR j.priority>=0 OR ?)
                AND (p.job IS NULL OR p.job='' OR p.repair_issue=j.issue OR p.job=j.id
                     OR p.job=j.bisect_of)
                ORDER BY j.priority DESC,j.created,j.rowid LIMIT 1""",
                (time.time(), background and not background_running, not batch)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',pid=0,available=?,attempts=attempts+1 WHERE id=?",
                       (time.time() + 60, row["id"]))
            return dict(row)

    def spawned(self, ident, pid):
        with self.connect() as db:
            db.execute("UPDATE jobs SET pid=? WHERE id=? AND status='running'", (pid, ident))

    def finish(self, ident, status, receipt=None, detail="", auto_remeasure=False):
        if status not in ("passed", "failed", "retry"):
            raise ValueError("invalid verification result")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            if row["status"] == "cancelled":
                return  # a runner finishing after an operator cancel keeps the cancel
            failures = row["failures"] + (status == "retry")
            available = 0
            if status == "retry" and failures >= RETRY_LIMIT:
                status = "stuck"
                detail = f"no verdict after {failures} attempts; last: {detail}"
            elif status == "retry":
                available = time.time() + retry_delay(failures)
            db.execute("UPDATE jobs SET status=?,receipt=?,detail=?,pid=0,available=?,notified=0,"
                       "failures=? WHERE id=?",
                       (status, receipt, detail, available, failures, ident))
            if status == "passed":
                # The tip contains every merge it covers: their verdict is its verdict.
                db.execute("UPDATE jobs SET status='passed',receipt=?,detail=?,notified=1 "
                           "WHERE covered_by=? AND status='covered'",
                           (receipt, f"covered by {ident}", ident))
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (row["common"],)).fetchone()
            if status == "failed":
                # A late verdict about pre-repair history must not pause it again.
                covered = pause and pause["cleared_sha"] and self.contains(row["repo"], row["sha"], pause["cleared_sha"])
                # A SHA master cannot reach (merge discarded after enqueue) has no
                # repair that could contain it, so its failure would pause forever.
                covered = covered or self.orphaned(row["repo"], row["sha"])
                if (auto_remeasure and not covered and (not pause or not pause["job"])
                        and row["remeasures"] < AUTO_REMEASURE_LIMIT):
                    # Measure the same SHA again first. The first failure stays in
                    # history, nothing is notified and nothing is paused: merging
                    # goes on, and only a second failure takes the pause below.
                    history = json.loads(row["history"]) + [{
                        "status": "failed", "receipt": receipt, "detail": detail, "at": time.time()}]
                    db.execute("UPDATE jobs SET status='queued',failures=0,available=0,pid=0,notified=1,"
                               "remeasures=remeasures+1,history=?,detail=? WHERE id=?",
                               (json.dumps(history), "auto remeasure after a failure: " + str(detail)[:300],
                                ident))
                elif not covered and (not pause or not pause["job"]):
                    db.execute("INSERT INTO pauses(common,job) VALUES (?,?) ON CONFLICT(common) "
                               "DO UPDATE SET job=excluded.job,repair_issue=NULL", (row["common"], ident))
                elif not covered and pause["repair_issue"] and pause["repair_issue"] == row["issue"]:
                    # The repair's own merge sweep failed: the pause follows the new
                    # failure and the slot empties so the next repair can be assigned.
                    db.execute("UPDATE pauses SET job=?,repair_issue=NULL WHERE common=?",
                               (ident, row["common"]))
            elif (status == "passed" and pause and pause["job"] == ident and row["remeasures"]
                  and not pause["repair_issue"]):
                # The pause job passed on its same-SHA remeasure: an intermittent failure.
                db.execute("UPDATE pauses SET job='',repair_issue=NULL,cleared_sha=? WHERE common=?",
                           (row["sha"], row["common"]))
                db.execute("UPDATE jobs SET detail=? WHERE id=?",
                           ("remeasure passed; intermittent \u2014 open a follow-up issue", ident))
            elif status == "passed" and row["remeasures"] and not (pause and pause["job"]):
                # The failure did not repeat on the same SHA and no pause was taken.
                db.execute("UPDATE jobs SET detail=? WHERE id=?",
                           ("remeasure passed; intermittent \u2014 the first failure is in history", ident))
            elif status == "passed" and pause and pause["job"] and pause["repair_issue"] == row["issue"]:
                failed = self.get(pause["job"])
                if self.contains(row["repo"], failed["sha"], row["sha"]):
                    db.execute("UPDATE pauses SET job='',repair_issue=NULL,cleared_sha=? WHERE common=?",
                               (row["sha"], row["common"]))

    def cancel(self, job, reason):
        """Operator escape hatch: drop a job that will never reach a verdict.

        A running runner is left to finish (it holds the test window and
        cleans up its checkout); its result no longer changes the row."""
        if not reason or not reason.strip():
            raise ValueError("cancel requires a reason")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            if row["status"] in ("passed", "failed"):
                raise ValueError(f"job already has a verdict: {row['status']}")
            db.execute("UPDATE jobs SET status='cancelled',detail=?,available=0 WHERE id=?",
                       (f"cancelled: {reason.strip()}", job))
            # The merges it covered still need a verdict: back to the batching pool.
            db.execute("UPDATE jobs SET status='queued',covered_by=NULL,formed=0 "
                       "WHERE covered_by=? AND status='covered'", (job,))

    def requeue(self, job):
        """Give a stuck or cancelled job a fresh set of attempts."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise ValueError("unknown verification job")
            if row["status"] not in ("stuck", "cancelled"):
                raise ValueError(f"only a stuck or cancelled job is requeued, not {row['status']}")
            db.execute("UPDATE jobs SET status='queued',failures=0,available=0,notified=0,detail='' "
                       "WHERE id=?", (job,))

    def remeasure(self, repo, job, reason):
        """Leader action: measure the active pause job's SHA again, at most REMEASURE_LIMIT times.

        The pause stays active meanwhile. A pass clears it (intermittent failure);
        a failure keeps it and the job goes the repair path."""
        if not reason or not reason.strip():
            raise ValueError("remeasure requires a reason")
        common = self.common(repo)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (common,)).fetchone()
            if not pause or pause["job"] != job:
                raise ValueError("remeasure must name the active failed job")
            if pause["repair_issue"]:
                raise ValueError(f"repair issue {pause['repair_issue']} is assigned; only a repair releases the pause")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if row["status"] != "failed":
                raise ValueError(f"only a failed job is remeasured, not {row['status']}")
            if row["remeasures"] >= REMEASURE_LIMIT:
                raise ValueError("remeasure limit reached; a second failure is a regression, assign a repair")
            history = json.loads(row["history"]) + [{"status": row["status"], "receipt": row["receipt"],
                                                     "detail": row["detail"], "at": time.time()}]
            db.execute("UPDATE jobs SET status='queued',priority=0,failures=0,available=0,pid=0,notified=0,"
                       "remeasures=remeasures+1,history=?,detail=? WHERE id=?",
                       (json.dumps(history), f"remeasure {row['remeasures'] + 1}: {reason.strip()}", job))

    def release(self, repo, job, reason):
        """Leader escape hatch: clear a pause whose failed SHA master cannot reach."""
        if not reason or not reason.strip():
            raise ValueError("release requires a reason")
        common = self.common(repo)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pause = db.execute("SELECT * FROM pauses WHERE common=?", (common,)).fetchone()
            if not pause or pause["job"] != job:
                raise ValueError("release must name the active failed job")
            failed = self.get(job)
            if not self.orphaned(failed["repo"], failed["sha"]):
                raise ValueError("failed commit is reachable from master; only a green repair releases it")
            db.execute("UPDATE pauses SET job='',repair_issue=NULL WHERE common=?", (common,))
            db.execute("UPDATE jobs SET detail=? WHERE id=?",
                       (f"pause released, sha unreachable from master: {reason.strip()}", job))

    @classmethod
    def orphaned(cls, repo, sha):
        """True when refs/heads/master exists and does not contain sha."""
        ref = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/heads/master"],
                             capture_output=True)
        return ref.returncode == 0 and not cls.contains(repo, sha, "refs/heads/master")

    @staticmethod
    def contains(repo, ancestor, head):
        return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, head],
                              capture_output=True).returncode == 0

    def notified(self, ident):
        with self.connect() as db:
            db.execute("UPDATE jobs SET notified=1 WHERE id=?", (ident,))

    def suspects(self, job, limit=20):
        """Merges that can have broken ``job``'s sweep, and the merges stacked on top.

        ``suspects``: the first-parent merges after the newest passed sweep that is
        an ancestor of the job's SHA, up to and including that SHA -- the tree the
        sweep measured. Without such a sweep, the last ``limit`` merges before it.
        Those whose changed files meet a failing test (the file itself, or the module
        the test is named after) come first; the order inside each group is newest
        first. ``after``: merges that landed past the failed SHA. They were not
        measured, so they are not suspects, and a revert of a suspect may have to
        carry them."""
        row = self.get(job)
        repo, sha = row["repo"], row["sha"]
        passed = [j for j in self.status()["jobs"]
                  if j["common"] == row["common"] and j["status"] == "passed" and j["sha"] != sha
                  and self.contains(repo, j["sha"], sha)]
        base = max(passed, key=lambda j: j["created"])["sha"] if passed else None
        span = f"{base}..{sha}" if base else sha

        def merges(spec, *extra):
            try:
                return git(repo, "rev-list", "--first-parent", "--merges", *extra, spec).split()
            except subprocess.CalledProcessError:
                return []

        failing = set()
        try:
            for line in json.loads(row["detail"] or "[]"):
                head = str(line).split(" ", 1)[-1].split("::", 1)[0].split(" - ", 1)[0]
                if head.endswith(".py"):
                    failing.add(head)
        except (TypeError, ValueError):
            pass  # a detail that is not the runner's failure list names no tests
        stems = {Path(f).stem.removeprefix("test_") for f in failing}
        by_sha = {j["sha"]: j["issue"] for j in self.status()["jobs"] if j["common"] == row["common"]}

        def describe(merge):
            files = git(repo, "diff", "--name-only", merge + "^1", merge).split()
            meets = sorted(f for f in files if f in failing or Path(f).stem in stems)
            return {"sha": merge, "issue": by_sha.get(merge, ""),
                    "subject": git(repo, "log", "-1", "--format=%s", merge),
                    "meets_failing_tests": meets, "files": len(files)}

        found = [describe(m) for m in merges(span, *([] if base else ["-n", str(limit)]))]
        found.sort(key=lambda d: not d["meets_failing_tests"])  # stable: newest first inside a group
        after = []
        if self.contains(repo, sha, "refs/heads/master"):
            after = [{"sha": m, "issue": by_sha.get(m, ""), "subject": git(repo, "log", "-1", "--format=%s", m)}
                     for m in merges(f"{sha}..refs/heads/master")]
        return {"job": job, "sha": sha, "base": base, "failing_tests": sorted(failing),
                "suspects": found, "after": after}


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
            queue.finish(ident, "failed", str(receipt), json.dumps(data.get("failures") or []),
                         auto_remeasure=True)
        else:
            queue.finish(ident, "retry", str(receipt) if receipt.exists() else None,
                         f"sweep execution exit {code}; no regression verdict")
    except Exception as exc:
        queue.finish(ident, "retry", detail=str(exc))
    finally:
        # Remove only this runner's checkout; logs and receipts stay available.
        if checkout.exists():
            subprocess.run(["git", "-C", row["repo"], "worktree", "remove", str(checkout)], capture_output=True)
