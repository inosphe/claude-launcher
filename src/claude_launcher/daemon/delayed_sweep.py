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
from . import instance_manifest, paths
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
        env[config.LAUNCHER_HOME_ENV] = origin["home"]
        # The config file goes with the home. Another instance's daemon may
        # run this job, and its own config file under the origin's home made
        # the suite's cli.main reconcile that instance's profiles into the
        # origin's home (claunch-edy0w). A job queued before origins recorded
        # it inherits the daemon's; cli.main's pairing check refuses that.
        if origin.get("config"):
            env[config.LAUNCHER_SYNC_ENV] = origin["config"]
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
                "cflow_inputs": {"job": job["id"], "python": job["python"], "tool": str(tool),
                                 "database": str(queue.database)},
                "beads": False}

    async def attend(self, queue, policy):
        """Form batches, then give each unwatched batch tip a child session.

        A tip is swept whether or not it has a watcher; a spawn that fails is
        tried again after SPAWN_BACKOFF and never holds the sweep back."""
        await asyncio.to_thread(queue.dispatch, *policy)
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
            origin = json.loads(job["origin"])
            if (origin.get("instance") != paths.instance()
                    or origin.get("base") != str(paths.base_home().resolve())):
                continue
            if time.monotonic() - self.spawn_tried.get(job["id"], -SPAWN_BACKOFF) < SPAWN_BACKOFF:
                continue
            try:
                if self.manager.get(job["owner"]).exited:
                    continue
            except KeyError:
                continue
            self.spawn_tried[job["id"]] = time.monotonic()
            try:
                name = await asyncio.to_thread(spawn, job["owner"], self.watch_body(job))
            except Exception:
                log.exception("sweep watcher for %s was not created", job["id"])
                continue
            queue.attach_child(job["id"], name)
            live += 1

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delayed sweep tick failed")
            await asyncio.sleep(self.poll)
