"""WebSocket terminal attachment for a session.

Protocol (matches the SPA's app.js and any non-browser client):

- server -> client, binary: raw PTY output bytes (feed straight to xterm.js).
  On connect the server first sends a JSON ``init`` text frame
  (``{"type":"init","cols":..,"rows":..,"status":..,"pid":..,"boot_id":..}``),
  then one binary frame repainting the current screen so a fresh viewer sees
  live state. Because the repaint comes with every socket, a client that lost
  one may simply open another against the same terminal: ``pid`` and
  ``boot_id`` together say whether it is the same program it was talking to.
- client -> server, binary: keystrokes/paste, written verbatim to the PTY.
- text frames are JSON control messages:
  client: ``{"type":"resize","cols":..,"rows":..}``, ``{"type":"repaint"}``
  (resend the current screen — used by viewers on focus regain, since another
  viewer may have resized the session meanwhile), ``{"type":"ping"}``.
  server: ``{"type":"state","status":...}``, ``{"type":"exit","code":...}``,
  ``{"type":"resize","cols":..,"rows":..}``, ``{"type":"pong"}``.

Auth: the route sits under ``/api/``, so the shared middleware enforces the
Bearer header (CLI/scripts — WebSocket client libraries can set headers) or
the login cookie (browsers) before the upgrade completes.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import WSMsgType, web

from .session import Session, SessionGone


async def terminal_ws(request: web.Request) -> web.WebSocketResponse:
    # Auth already happened: this route lives under /api/, so the middleware
    # validated a Bearer header (CLI/scripts) or the session cookie (the SPA
    # calls /api/auth/session before opening any terminal socket).
    manager = request.app["manager"]
    session: Session = manager.get(request.match_info["name"])

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    request.app["websockets"].add(ws)
    queue = session.subscribe()
    try:
        await ws.send_str(
            json.dumps(
                {
                    "type": "init",
                    "cols": session.sdef.cols,
                    "rows": session.sdef.rows,
                    "status": session.status(),
                    # Identifies *this incarnation*: a respawn keeps the name
                    # but spawns a new child, so a viewer can tell its socket
                    # is bound to a session that has since been replaced.
                    "pid": session.pid,
                    # And which daemon that incarnation belongs to. A restart
                    # relaunches restored sessions but retires the rest with
                    # the pid they last had, so a pid on its own can repeat
                    # across daemons; a reconnecting viewer that is about to
                    # replay keystrokes needs both to be sure of its child.
                    "boot_id": request.app["boot_id"],
                }
            )
        )
        await ws.send_bytes(session.screen.repaint_sequence())

        sender = asyncio.ensure_future(_pump_to_client(ws, queue))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    try:
                        await session.write_bytes(msg.data)
                    except SessionGone:
                        break
                elif msg.type == WSMsgType.TEXT:
                    await _handle_control(ws, session, msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        request.app["websockets"].discard(ws)
        session.unsubscribe(queue)
        if not ws.closed:
            await ws.close()
    return ws


async def _pump_to_client(ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
    while True:
        kind, payload = await queue.get()
        if kind == "data":
            await ws.send_bytes(payload)
        elif kind == "state":
            await ws.send_str(json.dumps({"type": "state", "status": payload}))
        elif kind == "resize":
            cols, rows = payload
            await ws.send_str(json.dumps({"type": "resize", "cols": cols, "rows": rows}))
        elif kind == "exit":
            await ws.send_str(json.dumps({"type": "exit", "code": payload}))


async def _handle_control(ws: web.WebSocketResponse, session: Session, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    kind = msg.get("type")
    if kind == "resize":
        try:
            session.resize(int(msg["cols"]), int(msg["rows"]))
        except (KeyError, ValueError, TypeError, SessionGone):
            pass
    elif kind == "repaint":
        await ws.send_bytes(session.screen.repaint_sequence())
    elif kind == "ping":
        await ws.send_str(json.dumps({"type": "pong"}))
