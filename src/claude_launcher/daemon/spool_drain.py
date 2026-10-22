"""Apply the writes sessions queued while this daemon was unreachable
(claunch-19liw.2.1).

A session's shell does not start a daemon; a write it could not send and
that may land late is queued on disk (:mod:`..request_spool`). This clock
applies the queue at startup and then every :data:`DRAIN_INTERVAL` seconds,
so a request queued during a stall is picked up without a restart.

A request is applied once. Its id is recorded in the spool's ``seen`` file
when it is applied, by this clock or by the live handler (``h_mesh_send``
reads the same header): a request that timed out on its way in was often
applied by a daemon that was only slow, and its queued copy is then dropped
here. The file outlives restarts for the same reason.

What is kept from the original request: the sender (the body's ``from``)
and the time it was queued, carried on the message's ``ref`` as
``spooled: {request_id, queued_at}``. A sender session that has ended since
is still delivered for, and logged. A request that cannot be applied is
moved to ``spool/failed/`` with the error, never deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Optional

from aiohttp import web

from .. import request_spool

log = logging.getLogger(__name__)

#: Seconds between two drains after the one at startup.
DRAIN_INTERVAL = 10.0

_MESH_SEND = re.compile(r"^/api/mesh/(?P<mesh>[^/?]+)/messages$")


def seen_ids(app: web.Application) -> request_spool.SeenIds:
    """The daemon's one record of applied request ids (set by :func:`install`)."""
    return app["spool_seen"]


def _sender_state(app: web.Application, sender: str) -> str:
    manager = app.get("manager")
    if manager is None or not sender:
        return "unknown"
    try:
        return str(manager.get(sender).status())
    except Exception:  # ManagerError: not a session of this daemon
        return "unknown"


async def _apply(app: web.Application, doc: dict) -> dict:
    method = str(doc.get("method") or "")
    path = str(doc.get("path") or "")
    body = doc.get("body") if isinstance(doc.get("body"), dict) else {}
    m = _MESH_SEND.match(path)
    if method != "POST" or m is None:
        raise ValueError(f"not a spoolable request: {method} {path}")
    sender = str(body.get("from") or doc.get("session") or "")
    to = body.get("to")
    text = body.get("body")
    if not sender or not isinstance(to, (str, list)) or not to or not isinstance(text, str):
        raise ValueError("mesh send needs 'from', 'to' and 'body'")
    ref = dict(body["ref"]) if isinstance(body.get("ref"), dict) else {}
    ref["spooled"] = {"request_id": doc.get("id"), "queued_at": doc.get("queued_at")}
    sections = body.get("sections")
    return await app["mesh"].send(
        m.group("mesh"),
        sender,
        to,
        text,
        external=bool(body.get("external")),
        type=str(body.get("type") or "say"),
        reply_to=str(body.get("reply_to") or "") or None,
        sections=sections if isinstance(sections, dict) else None,
        ref=ref,
    )


async def drain_once(app: web.Application) -> int:
    """Apply every pending request once; returns how many were applied."""
    seen = seen_ids(app)
    applied = 0
    for path, doc in request_spool.pending():
        rid = str(doc["id"])
        if rid in seen:
            log.info("spool: %s already applied; dropping the queued copy", rid)
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        sender = str((doc.get("body") or {}).get("from") or doc.get("session") or "")
        try:
            result = await _apply(app, doc)
        except Exception as exc:  # anything: the file is kept in failed/
            target = request_spool.fail(path, doc, f"{type(exc).__name__}: {exc}")
            log.warning("spool: %s from %s could not be applied (%s); kept at %s",
                        rid, sender, exc, target)
            continue
        seen.add(rid)
        with contextlib.suppress(OSError):
            path.unlink()
        applied += 1
        log.info(
            "spool: applied %s %s from %s (session %s, queued %s) -> %s",
            rid, doc.get("path"), sender, _sender_state(app, sender),
            doc.get("queued_at"), (result or {}).get("id"),
        )
    return applied


class SpoolDrainer:
    def __init__(self, app: web.Application, interval: float = DRAIN_INTERVAL):
        self.app = app
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await drain_once(self.app)
            except Exception:
                log.exception("spool drain failed")
            await asyncio.sleep(self.interval)


async def _start(app: web.Application) -> None:
    app["spool_drain"].start()


async def _stop(app: web.Application) -> None:
    await app["spool_drain"].stop()


def install(app: web.Application) -> SpoolDrainer:
    drainer = SpoolDrainer(app)
    app["spool_seen"] = request_spool.SeenIds()
    app["spool_drain"] = drainer
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
    return drainer
