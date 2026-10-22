"""The daemon's upkeep of the routing shims (claunch-19liw.2.3).

The daemon is the only process that starts or stops a shim
(:func:`routing.own_lifecycle`, set in ``__main__`` once the singleton lock is
held). This module is its two duties:

* ``POST /api/routing/ensure`` — a process outside the daemon (``claunch
  run``, a heartbeat) that needs a shim asks for it here; the daemon starts
  it if it is not running and keeps it while that process lives.
* a reconcile pass at startup and every :data:`routing.RECONCILE_S` — start
  the shims the config wants, retire the ones nothing needs any more
  (:func:`routing.reconcile`).

The pass runs off the loop: it probes ports and may wait out a shim start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiohttp import web

from .. import routing

log = logging.getLogger("claunch.daemon.shims")


def _live_sessions(manager) -> set:
    if manager is None:
        return set()
    return {s.sdef.name for s in manager.list() if s.status() != "exited"}


def reconcile_once(manager) -> dict:
    result = routing.reconcile(_live_sessions(manager))
    if result["started"] or result["retired"] or result["errors"]:
        log.info("routing shims: wanted %s, retired %s%s",
                 ", ".join(result["started"]) or "none",
                 ", ".join(result["retired"]) or "none",
                 "; errors: " + "; ".join(result["errors"]) if result["errors"] else "")
    return result


async def _loop(app: web.Application) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, reconcile_once, app.get("shims_manager"))
        except Exception:  # noqa: BLE001 - upkeep must never take the daemon down
            log.exception("routing shim reconcile failed")
        await asyncio.sleep(routing.RECONCILE_S)


async def _start(app: web.Application) -> None:
    if routing.owns_lifecycle():
        app["shims_task"] = asyncio.get_running_loop().create_task(
            _loop(app), name="routing-shims")


async def _stop(app: web.Application) -> None:
    task: Optional[asyncio.Task] = app.get("shims_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def _ensure_for(body: dict) -> str:
    url = routing.ensure_shim(
        str(body["upstream"]), body.get("spec") or {},
        body.get("provider") or None, route=body.get("route") or None)
    pid = body.get("pid")
    if isinstance(pid, int) and pid > 0:
        routing.add_user(routing._port_of(url), pid=pid)
    return url


async def h_ensure(request: web.Request) -> web.Response:
    """The shim a process outside the daemon needs (see the module doc)."""
    if not routing.owns_lifecycle():
        return web.json_response(
            {"error": "this daemon does not manage routing shims"}, status=409)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict) or not body.get("upstream") or not isinstance(
            body.get("spec") or {}, dict):
        return web.json_response(
            {"error": "need upstream and an object spec"}, status=400)
    try:
        url = await asyncio.get_running_loop().run_in_executor(None, _ensure_for, body)
    except routing.RoutingError as exc:
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response({"url": url})


def install(app: web.Application, manager) -> None:
    app["shims_manager"] = manager
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
    app.router.add_post(routing.ENSURE_API, h_ensure)
