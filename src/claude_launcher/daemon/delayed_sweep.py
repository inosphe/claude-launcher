"""Drain the durable sweep queue without holding a leader's workflow task."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from ..delayed_sweep import Queue
from .. import test_window
from . import paths

log = logging.getLogger("claunch.daemon.delayed_sweep")


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
            if job["status"] not in ("passed", "failed") or job["notified"]:
                continue
            try:
                session = self.manager.get(job["owner"])
                if session.exited:
                    continue
                notice = ("---\n# claunch delayed-sweep: result -- machine-generated\n"
                          f"job: {job['id']}\nrepo: {job['repo']}\n"
                          f"sha: {job['sha']}\nstatus: {job['status']}\n"
                          f"receipt: {job['receipt']}\n"
                          "protocol: call flow_status, read tools/delayed_sweep.py status "
                          "for this repository. A failed active pause requires the "
                          "leader's handle-regression route and a repair child. "
                          "Live deployment is separate.\n---")
                if await session.deliver(notice):
                    queue.notified(job["id"])
            except KeyError:
                pass
        if self.runner and self.runner.returncode is None:
            return
        window = self.window.status() if self.window is not None else {}
        idle = not window.get("holders") and not window.get("queue")
        job = queue.claim(background=idle)
        if not job:
            return
        scratch = paths.session_scratch_dir(job["owner"])
        scratch.mkdir(parents=True, exist_ok=True)
        logfile = scratch / ("delayed-sweep-" + job["id"] + ".log")
        script = Path(job["tool"]).with_name("delayed_sweep.py")
        env = dict(os.environ)
        # A daemon-owned test must outlive its originating leader session.
        env.pop("CLAUNCH_SESSION", None)
        env.pop(test_window.WINDOW_MODE_ENV, None)
        for name in (test_window.WINDOW_GRANT_ENV, test_window.WINDOW_CLASS_ENV,
                     test_window.WINDOW_WORKERS_ENV, test_window.WINDOW_REPORT_ENV):
            env.pop(name, None)
        env["CLAUNCH_SCRATCH"] = str(scratch)
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
