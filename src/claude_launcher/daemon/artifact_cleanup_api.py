"""Session-bound cleanup routes; blocking filesystem work stays off-loop."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
from aiohttp import web

from .artifact_cleanup import Cleanup, CleanupError

log = logging.getLogger(__name__)


#: How often a scheduled cleanup looks for a free measurement window. One
#: minute is far shorter than the useful life of a basetemp and long enough
#: that a queue of schedules does not spin against the window.
SCHEDULED_POLL = 60.0


def install(app):
    service = Cleanup(app["manager"])
    # The daemon's clock drives scheduled applies from here (__main__ starts it).
    app["artifact_cleanup"] = service
    # Per session, not one for the daemon: a plan walks every file its session
    # owns, and one session's long walk must not hold every other session's
    # request past the client's timeout (claunch-30rkn).
    locks = {}
    planning = {}

    @contextlib.asynccontextmanager
    async def lock_for(session):
        # Dropped when its last user leaves, so the map holds live work only.
        entry = locks.setdefault(session, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del locks[session]

    async def plan(session):
        async with lock_for(session):
            return await asyncio.to_thread(service.plan, session)

    def planned(session, job):
        if planning.get(session) is job:
            del planning[session]
        if not job.cancelled():
            job.exception()  # retrieved here; every waiter gets it from await

    async def handle(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"session", "plan_id"}:
                raise CleanupError("only session and plan_id are accepted; paths are not accepted")
            session = body.get("session")
            token = body.get("plan_id")
            if not isinstance(session, str) or not session:
                raise CleanupError("a managed session is required")
            if token is not None and (not isinstance(token, str) or not token):
                raise CleanupError("plan_id must be a nonempty string")
            if token is None:
                # Single flight: a caller that gave up leaves its walk running,
                # so its retry joins that walk instead of queueing a second.
                job = planning.get(session)
                if job is None:
                    job = planning[session] = asyncio.ensure_future(plan(session))
                    job.add_done_callback(lambda done: planned(session, done))
                return web.json_response(await asyncio.shield(job))
            async with lock_for(session):
                # Nonblocking exclusive acquisition: no test may start while
                # deleting. A daemon-owned grant survives the caller's exit.
                window = app["window"]
                grant = await window.acquire("sweep", session=None, pid=os.getpid(),
                                             bound=True, label=f"artifact cleanup: {session}",
                                             workers=1, wait=0)
                if not grant.get("granted"):
                    # Nobody will ask again: the caller's turn ends here, and
                    # auto-release is exactly the step that runs with no person
                    # at the terminal (claunch-q4cqy.2). Keep the request and
                    # let scheduled_clock() run it when the window frees up.
                    reason = grant.get("reason", "measurement window unavailable")
                    try:
                        service.schedule(session, reason)
                        status = "scheduled"
                    except (CleanupError, OSError) as exc:
                        reason = f"{reason} (could not schedule: {exc})"
                        status = "cleanup_pending"
                    return web.json_response({"status": status, "plan_id": token,
                                              "reason": reason})
                try:
                    job = asyncio.create_task(asyncio.to_thread(service.apply, session, token))
                    try:
                        result = await asyncio.shield(job)
                    except asyncio.CancelledError:
                        # to_thread keeps running after cancellation; keep its
                        # exclusive grant until the actual deletion finishes.
                        await job
                        raise
                    return web.json_response(result)
                finally:
                    window.release(grant["grant_id"])
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            return web.json_response({"error": str(exc), "status": "cleanup_pending"}, status=400)

    app.router.add_post("/api/artifact-cleanup", handle)


async def scheduled_clock(app, service, interval=SCHEDULED_POLL):
    """Run each session's scheduled cleanup once the window frees up.

    The failure this exists for is silent: an apply refused for a busy window
    leaves the caller's turn with nothing to retry, and the basetemps stay on
    disk until the session ends (claunch-q4cqy.2 measured four sessions that
    way). A schedule is replanned at run time -- plans expire after ten minutes
    -- and both the ownership check and the fingerprint check still run before
    anything is deleted.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            sessions = await asyncio.to_thread(service.scheduled)
        except Exception:  # noqa: BLE001 -- a bad pass must not end the clock
            log.warning("cleanup: could not list scheduled sessions", exc_info=True)
            continue
        for session in sessions:
            window = app["window"]
            grant = await window.acquire("sweep", session=None, pid=os.getpid(),
                                         bound=True, label=f"scheduled cleanup: {session}",
                                         workers=1, wait=0)
            if not grant.get("granted"):
                continue  # still busy; the next pass asks again
            try:
                await asyncio.to_thread(service.run_scheduled, session)
            except Exception:  # noqa: BLE001 -- one session must not stop the others
                log.warning("cleanup: scheduled pass failed for %s", session, exc_info=True)
            finally:
                window.release(grant["grant_id"])
