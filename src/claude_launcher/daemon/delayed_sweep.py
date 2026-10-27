"""Drain the durable sweep queue without holding a leader's workflow task."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from ..delayed_sweep import BATCH_SETTLE, BATCH_SIZE, NOTIFIED, Queue
from .. import config, store, test_window
from . import instance_manifest, paths, pools, spawn_events
from .session import delivery_wake

log = logging.getLogger("claunch.daemon.delayed_sweep")


def _ensure_git_identity(env):
    """Give the runner a commit identity when git resolves none in `env`.

    The daemon has no GIT_CONFIG_GLOBAL, so tests that commit in scratch
    repositories fail with "Author identity unknown". A configured identity
    is left alone.
    """
    for key, var in (("user.name", "NAME"), ("user.email", "EMAIL")):
        try:
            found = subprocess.run(["git", "config", "--get", key], env=env,
                                   capture_output=True, text=True, timeout=10)
            resolved = found.returncode == 0 and found.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            resolved = False
        if not resolved:
            value = "claunch delayed sweep" if var == "NAME" else "delayed-sweep@claunch.invalid"
            env.setdefault("GIT_AUTHOR_" + var, value)
            env.setdefault("GIT_COMMITTER_" + var, value)


#: Live watching sessions at once, and the pause before a failed spawn is tried again.
CHILDREN_MAX = 3
SPAWN_BACKOFF = 300.0
#: Seconds between nudges the watching session's workflow sends (its wait timeout).
WATCH_WORKFLOW = "sweep-job-v2"
#: The role that run declares for itself (``default_role`` in
#: workflows/v2/sweep-job-v2.yaml). Unlike a cflow2 delegation, the daemon
#: composes this spawn body itself, so it carries the name here; a test keeps
#: the two spellings equal.
WATCH_ROLE = "oneshot"


def _one_line(text: str, limit: int = 300) -> str:
    """A log line and a refusal body from a message that may run to a traceback.

    Collapsing whitespace keeps one failure to one line, which is what the
    log and the notice to the owner are for."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def suspect_lines(queue, job):
    """The notice lines naming the merges that can have broken a failed sweep.

    Empty when the list cannot be computed: a notice never waits on it."""
    try:
        found = queue.suspects(job["id"])
    except Exception:
        log.exception("suspects of %s were not computed", job["id"])
        return ""
    rows = [f"  {m['sha'][:12]} {m['issue'] or '-'} {m['subject'][:60]}"
            + (f" [meets {', '.join(m['meets_failing_tests'][:3])}]" if m["meets_failing_tests"] else "")
            for m in found["suspects"]]
    later = [f"  {m['sha'][:12]} {m['issue'] or '-'} {m['subject'][:60]}" for m in found["after"]]
    text = ("revert candidates (merges after the last green sweep, up to the failed SHA; "
            "those meeting a failing test first):\n" + "\n".join(rows) + "\n")
    if later:
        text += ("merged after the failed SHA (not measured, not suspects; a revert may "
                 "have to carry them):\n" + "\n".join(later) + "\n")
    return text


def batch_policy(cfg=None):
    """(size, settle seconds) for batching background sweeps, or None when off.

    ``delayed_sweep_batch_size`` 0 or false turns batching off: every merge is
    swept on its own, as before claunch-yfkt2."""
    cfg = store.daemon_config() if cfg is None else cfg
    try:
        size = int(cfg.get("delayed_sweep_batch_size", BATCH_SIZE) or 0)
        settle = float(cfg.get("delayed_sweep_batch_settle", BATCH_SETTLE))
    except (TypeError, ValueError):
        return None
    return (size, max(0.0, settle)) if size > 0 else None


class Clock:
    def __init__(self, manager, queue=None, poll=15, window=None, batch=False, spawn=None, policy=None):
        self.manager = manager
        self.queue = queue
        self.poll = poll
        self.task = None
        self.runner = None
        self.window = window
        #: True: group background merges into batches and watch each with a child session.
        self.batch = batch
        self.spawn = spawn
        #: (size, settle) fixed by the caller; None reads the daemon settings each tick.
        self.policy = policy
        self.spawn_tried = {}
        #: Jobs whose watcher the daemon refused in a way a retry cannot
        #: change, and the refusal it gave. A refused spawn never holds the
        #: sweep back, but an answer that is already final must not be asked
        #: for again every SPAWN_BACKOFF: each attempt costs a session number
        #: and a log line, and on 2026-10-27 it cost 34 of each per minute
        #: (claunch-k3acp).
        #: Keyed by ``(job id, generation)``, not by the job id alone: a person
        #: who fixes the cause the refusal named asks again with
        #: ``retry-watcher``, which bumps the generation, so the job gets a
        #: watcher without a daemon restart (claunch-k3acp review of
        #: 4f3f8b85).
        self.spawn_permanent: dict = {}
        #: Jobs whose owner has been told, and with which reason — one report
        #: per job and generation, whatever it says the second time.
        self.spawn_reported: dict = {}
        #: How often each reason has been seen, so repeats are counted
        #: instead of logged.
        self.spawn_failures: dict = {}

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def shutdown(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        # An in-flight test keeps its pid-bound grant across daemon restarts.
        # The next clock observes the persisted runner pid instead of rerunning it.

    async def tick(self):
        queue = self.queue or Queue()
        for job in queue.status()["jobs"]:
            if job["status"] not in NOTIFIED or job["notified"]:
                continue
            origin = json.loads(job["origin"])
            if (origin.get("instance") != paths.instance()
                    or origin.get("base") != str(paths.base_home().resolve())):
                continue
            try:
                session = self.manager.get(job["owner"])
                if session.exited:
                    continue
                notice = ("---\n# claunch delayed-sweep: result -- machine-generated\n"
                          f"job: {job['id']}\nrepo: {job['repo']}\n"
                          f"sha: {job['sha']}\nstatus: {job['status']}\n"
                          f"receipt: {job['receipt']}\n"
                          + (f"detail: {job['detail']}\n" if job["status"] == "stuck" else "")
                          + (await asyncio.to_thread(suspect_lines, queue, job)
                             if job["status"] == "failed" else "")
                          + "protocol: call flow_status, read tools/delayed_sweep.py status "
                          "for this repository. A failed active pause requires the "
                          "leader's handle-regression route and a repair child. "
                          + ("A stuck job ran out of retries without a verdict: fix what "
                             "the detail names, then 'tools/delayed_sweep.py requeue "
                             "--job <id>', or 'cancel --job <id> --reason ...'. "
                             if job["status"] == "stuck" else "")
                          + "Live deployment is separate.\n---")
                with delivery_wake("notice"):
                    delivered = await session.deliver(notice)
                if delivered:
                    queue.notified(job["id"])
            except KeyError:
                pass
        policy = (self.policy or batch_policy()) if self.batch else None
        if policy:
            await self.attend(queue, policy)
        # Queue.claim persists runner leases and permits one repair to wait
        # ahead of the background runner. Resource exclusivity belongs to Window.
        window = self.window.status() if self.window is not None else {}
        idle = not window.get("holders") and not window.get("queue")
        job = queue.claim(background=idle, batch=policy)
        if not job:
            return
        origin = json.loads(job["origin"])
        if not {"instance", "home", "base", "scratch"} <= origin.keys():
            queue.finish(job["id"], "retry", detail="job has no recorded originating instance")
            return
        scratch = Path(origin["scratch"])
        scratch.mkdir(parents=True, exist_ok=True)
        logfile = scratch / ("delayed-sweep-" + job["id"] + ".log")
        script = Path(job["tool"]).with_name("delayed_sweep.py")
        env = dict(os.environ)
        # A daemon-owned test must outlive its originating leader session.
        env.pop("CLAUNCH_SESSION", None)
        # ...but the Window page still names who it runs for (claunch-ersj2).
        env[test_window.WINDOW_REQUESTED_BY_ENV] = job["owner"]
        env.pop(test_window.WINDOW_MODE_ENV, None)
        for name in (test_window.WINDOW_GRANT_ENV, test_window.WINDOW_CLASS_ENV,
                     test_window.WINDOW_WORKERS_ENV, test_window.WINDOW_REPORT_ENV):
            env.pop(name, None)
        env["CLAUNCH_SCRATCH"] = str(scratch)
        # The runner is started by absolute interpreter path, so the suite's
        # `python` check commands find no interpreter unless its directory
        # is on PATH (what activating the venv would do).
        env["PATH"] = str(Path(job["python"]).parent) + os.pathsep + env.get("PATH", "")
        # The config file is <home>/.claunch.yaml, so it goes with the home:
        # another instance's daemon running this job cannot hand the suite its
        # own config file under the origin's home (claunch-edy0w). A ``config``
        # recorded by older code is not read.
        env[config.LAUNCHER_HOME_ENV] = origin["home"]
        env[paths.INSTANCE_BASE_ENV] = origin["base"]
        # The daemon of a named instance carries its manifest's bookkeeping
        # (applied name, previous values, port). The suite's first cli.main()
        # would see an "applied" instance that differs from the default one
        # and undo it, restoring CLAUDE_LAUNCHER_HOME to the pre-manifest
        # value -- the test's throwaway home becomes the real one. The runner
        # gets its home and instance explicitly, so none of it is passed on.
        for name in (instance_manifest.APPLIED_ENV, instance_manifest.PREV_ENV,
                     instance_manifest.PORT_ENV):
            env.pop(name, None)
        # The default instance is the absence of the variable: an empty value
        # is still "set" and the suite's leaked-instance-state guard rejects it.
        if origin["instance"]:
            env[paths.INSTANCE_ENV] = origin["instance"]
        else:
            env.pop(paths.INSTANCE_ENV, None)
        # git config lookups can take seconds; keep them off the event loop.
        await asyncio.to_thread(_ensure_git_identity, env)
        try:
            with logfile.open("ab") as output:
                self.runner = await asyncio.create_subprocess_exec(
                    job["python"], str(script), "--database", str(queue.database),
                    "run-one", "--job", job["id"], "--scratch", str(scratch),
                    stdout=output, stderr=output, env=env)
            queue.spawned(job["id"], self.runner.pid)
        except Exception as exc:
            queue.finish(job["id"], "retry", detail=str(exc))

    def watch_body(self, job):
        """Spawn request for the session that watches a batch tip."""
        queue = self.queue or Queue()
        members = len(queue.members(job["id"]))
        tool = Path(job["tool"]).with_name("delayed_sweep.py")
        task = (f"스윕 작업 {job['id']}({job['sha'][:12]}, 머지 {1 + members}건)를 지켜보는 일회성 세션이다.\n"
                "스윕은 데몬이 테스트 창에서 단위 테스트보다 낮은 우선순위로 돌린다. 직접 테스트를 "
                "돌리지 않는다. 워크플로(sweep-job-v2)가 대기와 주기 알림을 몬다 — flow_status로 "
                "시작한다.\n"
                f"큐 도구: {job['python']} {tool} --database {queue.database}")
        return {"task": task, "workflow": WATCH_WORKFLOW, "workflow_version": 2,
                # The role the run declares (``default_role`` in
                # workflows/v2/sweep-job-v2.yaml). The daemon builds this body
                # itself, so the declaration is not read off the chart the way
                # ``cflow2_exec.session_body`` reads a delegation's — the two
                # are kept in step by a test, and the watcher joins its
                # parent's main mesh as ``oneshot`` rather than falling to the
                # handle's default.
                "role": WATCH_ROLE,
                "cflow_inputs": {"job": job["id"], "python": job["python"], "tool": str(tool),
                                 "database": str(queue.database)},
                "beads": False}

    async def attend(self, queue, policy):
        """Form batches, then give each unwatched batch tip a child session.

        A tip is swept whether or not it has a watcher; a spawn that fails is
        tried again after SPAWN_BACKOFF and never holds the sweep back.

        A failure the daemon answered finally (a 4xx about the request) is not
        tried again: the job is marked in ``spawn_permanent`` and its owner is
        told once. Every attempt costs a session number and a log line, and
        on 2026-10-27 an unretryable refusal was asked 34 times a minute
        (claunch-k3acp). Anything else — no answer, a timeout, a 5xx — keeps
        its retry."""
        await pools.background(queue.dispatch, *policy)
        jobs = queue.status()["jobs"]
        live = 0
        for job in jobs:
            if not job["child"] or job["covered_by"] or job["status"] in ("passed", "failed", "stuck", "cancelled"):
                continue
            try:
                gone = self.manager.get(job["child"]).exited
            except KeyError:
                gone = True
            except AttributeError:
                gone = False
            if gone:
                queue.attach_child(job["id"], "")
            else:
                live += 1
        spawn = self.spawn
        if spawn is None:
            from .cflow2_exec import default_spawn as spawn
        for job in queue.unattended():
            if live >= CHILDREN_MAX:
                break
            if (job["id"], job["generation"]) in self.spawn_permanent:
                continue
            origin = json.loads(job["origin"])
            if (origin.get("instance") != paths.instance()
                    or origin.get("base") != str(paths.base_home().resolve())):
                continue
            if time.monotonic() - self.spawn_tried.get((job["id"], job["generation"]),
                                                       -SPAWN_BACKOFF) < SPAWN_BACKOFF:
                continue
            try:
                if self.manager.get(job["owner"]).exited:
                    continue
            except KeyError:
                continue
            self.spawn_tried[(job["id"], job["generation"])] = time.monotonic()
            try:
                name = await asyncio.to_thread(spawn, job["owner"], self.watch_body(job))
            except Exception as exc:  # noqa: BLE001 - refused or unreachable alike
                await self._spawn_failed(job, exc)
                # ``_spawn_failed`` owns everything this failure decides --
                # the log, the retry, and (for a permanent refusal) the typed
                # block plus the durable warning event, which it emits from
                # inside :meth:`_report_spawn_failure` so both ride the same
                # ``(job id, generation)`` condition (claunch-is4o4,
                # claunch-k3acp). Nothing is reported here: a second gate at
                # this call site would drift from that one the first time a
                # person bumps the generation with ``retry-watcher``.
                continue
            queue.attach_child(job["id"], name)
            live += 1

    async def _spawn_failed(self, job, exc) -> None:
        """Record one failed watcher spawn; report it if it is final.

        The log carries one line per reason, not one per attempt: a transient
        failure repeats every SPAWN_BACKOFF and the count is the only thing
        that changes.

        A *transient* failure still reaches the person as a warning event in
        the session history (claunch-is4o4) -- that retry loop never becomes
        final, so nothing else would tell them it is happening (34 failures a
        minute, one session number burned each time). The event folds repeats
        of one (owner, reason) into one report, so it is not one row per
        attempt."""
        reason = _one_line(str(exc) or exc.__class__.__name__)
        if getattr(exc, "permanent", False):
            self.spawn_permanent[(job["id"], job["generation"])] = reason
            self.spawn_failures[reason] = self.spawn_failures.get(reason, 0) + 1
            log.warning("sweep watcher for %s will not be created: %s",
                        job["id"], reason)
            await self._report_spawn_failure(job, reason)
            return
        count = self.spawn_failures[reason] = self.spawn_failures.get(reason, 0) + 1
        if count == 1:
            log.warning("sweep watcher for %s could not be created: %s "
                        "(retrying every %ds; later repeats are counted, not logged)",
                        job["id"], reason, int(SPAWN_BACKOFF))
        else:
            log.debug("sweep watcher for %s still failing (%d times): %s",
                      job["id"], count, reason)
        spawn_events.report(
            job["owner"], kind="fail", reason=reason,
            site="delayed_sweep.attend", job=job["id"], sha=job.get("sha", ""),
            generation=job.get("generation"), permanent=False,
        )

    async def _report_spawn_failure(self, job, reason: str) -> None:
        """Tell the job's owner once, with the refusal and what it costs them.

        One report per job generation: the reason is kept, so a job the daemon
        refuses for a second reason still reports that second one, while a
        repeat of the first is not sent again. A generation bump
        (``retry-watcher``) is a new attempt by a person, and its report is
        new too.

        The durable event is emitted here, inside the same guard, rather than
        at the call site: the block below is typed text that scrolls away, and
        the event is what a person can still find afterwards. Keeping both on
        one ``(job id, generation)`` condition means a ``retry-watcher`` bump
        cannot deliver a block with no matching row, or the reverse
        (claunch-is4o4, claunch-k3acp)."""
        key = (job["id"], job["generation"])
        if self.spawn_reported.get(key) == reason:
            return
        self.spawn_reported[key] = reason
        spawn_events.report(
            job["owner"], kind="fail", reason=reason,
            site="delayed_sweep.attend", job=job["id"], sha=job.get("sha", ""),
            generation=job.get("generation"), permanent=True,
            # The block below already puts this in front of the person, and
            # this guard -- not the event's 900-second fold -- is what says
            # "once": a retry-watcher bump is a person asking again and must
            # bring both back together.
            notice=False, fold=False,
        )
        try:
            session = self.manager.get(job["owner"])
            if session.exited:
                return
            notice = ("---\n# claunch delayed-sweep: watcher not created -- machine-generated\n"
                      f"job: {job['id']}\n"
                      f"owner: {job['owner']}\n"
                      f"repo: {job['repo']}\n"
                      f"sha: {job['sha']}\n"
                      f"reason: {reason}\n"
                      "protocol: the daemon refused this spawn with an answer a retry "
                      "cannot change, so it will not try again. The sweep itself still "
                      "runs; only the watching session is missing. Fix the cause named "
                      "above, then ask for the watcher again with "
                      f"'tools/delayed_sweep.py retry-watcher --job {job['id']}' — the "
                      "job keeps its status, its verdict and its covered merges, and the "
                      f"daemon gives it a watcher on the next tick. To drop the job "
                      f"instead: 'cancel --job {job['id']} --reason ...'.\n---")
            with delivery_wake("notice"):
                await session.deliver(notice)
        except KeyError:
            pass
        except Exception as exc:  # noqa: BLE001 - the report must not fail the tick
            log.warning("sweep watcher refusal for %s was not delivered: %s",
                        job["id"], _one_line(str(exc) or exc.__class__.__name__))

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delayed sweep tick failed")
            await asyncio.sleep(self.poll)
