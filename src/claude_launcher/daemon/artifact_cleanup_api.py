"""Session-bound cleanup routes; blocking filesystem work stays off-loop."""
from __future__ import annotations

import asyncio
import os
import sqlite3
from aiohttp import web

from .artifact_cleanup import Cleanup, CleanupError


def install(app):
    service = Cleanup(app["manager"])
    lock = asyncio.Lock()

    async def handle(request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"session", "plan_id"}:
                raise CleanupError("only session and plan_id are accepted; paths are not accepted")
            session = body.get("session")
            token = body.get("plan_id")
            if token is not None and (not isinstance(token, str) or not token):
                raise CleanupError("plan_id must be a nonempty string")
            async with lock:
                if token is None:
                    return web.json_response(await asyncio.to_thread(service.plan, session))
                # Nonblocking exclusive acquisition: no test may start while
                # deleting. A daemon-owned grant survives the caller's exit.
                window = app["window"]
                grant = await window.acquire("sweep", session=None, pid=os.getpid(),
                                             bound=True, label=f"artifact cleanup: {session}",
                                             workers=1, wait=0)
                if not grant.get("granted"):
                    return web.json_response({"status": "cleanup_pending", "plan_id": token,
                                              "reason": grant.get("reason", "measurement window unavailable")})
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
