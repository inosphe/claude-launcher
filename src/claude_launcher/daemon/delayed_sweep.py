"""Drain the durable sweep queue without holding a leader's workflow task."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path

from ..delayed_sweep import NOTIFIED, Queue
from .. import config, test_window
from . import instance_manifest, paths

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


class Clock:
    def __init__(self, manager, queue=None, poll=15, window=None):
        self.manager = manager
        self.queue = queue
        self.poll = poll
        self.task = None
        self.runner = None
        self.window = window

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
                if await session.deliver(notice):
                    queue.notified(job["id"])
            except KeyError:
                pass
        # Queue.claim persists runner leases and permits one repair to wait
        # ahead of the background runner. Resource exclusivity belongs to Window.
        window = self.window.status() if self.window is not None else {}
        idle = not window.get("holders") and not window.get("queue")
        job = queue.claim(background=idle)
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

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delayed sweep tick failed")
            await asyncio.sleep(self.poll)
