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
  viewer may have resized the session meanwhile),
  ``{"type":"scroll","lines":N}`` (view history held by the daemon: N>0 moves
  further back, N<0 back toward live, anything past the bounds clamps —
  ``-999999`` snaps to live), and ``{"type":"ping"}``.
  server: ``{"type":"state","status":...}``, ``{"type":"exit","code":...}``,
  ``{"type":"resize","cols":..,"rows":..}``, ``{"type":"buffer","alt":..}``
  (the program entered or left the alternate screen — the client learns the
  mode it may not have been connected for), ``{"type":"scrolled","offset":N}``
  (the server's clamped scroll position for this socket, sent before the
  repaint answering a ``scroll``), ``{"type":"pong"}``, and
  ``{"type":"shutdown"}`` — the daemon itself is stopping/restarting, sent
  before its sessions are terminated so a viewer can tell this apart from the
  session's program exiting on its own.

Virtual scroll: while a socket is scrolled back (``scrolled.offset > 0``) the
daemon feeds it no raw PTY data — the viewer is looking at a snapshot of the
daemon's scrollback, and live bytes would smear it. ``data`` frames resume
the moment the offset returns to 0. Each socket has its own offset; one
viewer can browse history while another watches live. A grid-shape change
(resize), the TUI leaving the alternate screen, or the session exiting all
make the frozen snapshot stale, so the pump unfreezes (repaints at offset 0)
before announcing such a frame.

Auth: the route sits under ``/api/``, so the shared middleware enforces the
Bearer header (CLI/scripts — WebSocket client libraries can set headers) or
the login cookie (browsers) before the upgrade completes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

from aiohttp import WSMsgType, web

from .session import Session, SessionGone


@dataclasses.dataclass
class ViewerState:
    """Per-socket scroll-back state for one terminal viewer.

    A separate instance per WebSocket is what lets one viewer browse history
    while another sits live on the same terminal: each pump suppresses
    ``data`` only for the socket whose offset is non-zero.
    """

    offset: int = 0


async def _synced(session) -> None:
    """Let the rendered grid catch up before it is replayed to a viewer.

    A repaint is a snapshot: sent mid-render it would show a half-drawn
    screen and stay that way until the next output arrived. Dead sessions
    have no feeder and nothing pending, hence the getattr.
    """
    synced = getattr(session, "screen_synced", None)
    if synced is not None:
        await synced()


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
    state = ViewerState()
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
                    # Which buffer the program is in right now, so a viewer
                    # joining mid-TUI knows whether the wheel browses history
                    # (alt screen) or xterm's own scrollback (main buffer).
                    "alt": session.screen.alt_screen,
                }
            )
        )
        await _synced(session)
        await ws.send_bytes(session.screen.repaint_sequence(0))

        sender = asyncio.ensure_future(_pump_to_client(ws, queue, session, state))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    # A binary frame is a human at a keyboard (attach or the
                    # web terminal); the mark parks automated deliveries so
                    # they don't type into a message being composed.
                    session.note_human_input(at_terminal=True)
                    try:
                        await session.write_bytes(msg.data)
                    except SessionGone:
                        break
                elif msg.type == WSMsgType.TEXT:
                    await _handle_control(ws, session, msg.data, state)
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


async def _pump_to_client(
    ws: web.WebSocketResponse,
    queue: asyncio.Queue,
    session: Session,
    state: ViewerState,
) -> None:
    while True:
        kind, payload = await queue.get()
        if kind == "data":
            if state.offset > 0:
                continue  # frozen: the viewer reads history, not live bytes
            await ws.send_bytes(payload)
        elif kind == "buffer":
            if state.offset > 0 and not payload:
                # The TUI left the alternate screen while this viewer was
                # scrolled back: the snapshot is of a screen that no longer
                # exists, so return the viewer to live before announcing it.
                await _unfreeze(ws, session, state)
            await ws.send_str(json.dumps({"type": "buffer", "alt": payload}))
        elif kind == "state":
            await ws.send_str(json.dumps({"type": "state", "status": payload}))
        elif kind == "resize":
            cols, rows = payload
            if state.offset > 0:
                # A resize changes the grid shape, so the frozen window no
                # longer composes a real screen — drop the viewer to live
                # first, then let it refit to the new geometry.
                await _unfreeze(ws, session, state)
            await ws.send_str(json.dumps({"type": "resize", "cols": cols, "rows": rows}))
        elif kind == "exit":
            if state.offset > 0:
                # Let the viewer see the session's final grid (exited
                # sessions have no feeder, so _synced is a no-op) before the
                # exit frame tells them it is over.
                await _unfreeze(ws, session, state)
            await ws.send_str(json.dumps({"type": "exit", "code": payload}))


async def _unfreeze(ws: web.WebSocketResponse, session: Session, state: ViewerState) -> None:
    """Drop a scrolled-back viewer to live: announce, then repaint at 0."""
    state.offset = 0
    await _synced(session)
    await ws.send_str(json.dumps({"type": "scrolled", "offset": 0}))
    await ws.send_bytes(session.screen.repaint_sequence(0))


async def _handle_control(
    ws: web.WebSocketResponse,
    session: Session,
    raw: str,
    state: ViewerState,
) -> None:
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
        # Focus-regain repaints keep this viewer's scroll position — a real
        # resize would already have unfrozen it through the pump.
        await _synced(session)
        await ws.send_str(json.dumps({"type": "scrolled", "offset": state.offset}))
        await ws.send_bytes(session.screen.repaint_sequence(state.offset))
    elif kind == "scroll":
        try:
            lines = int(msg["lines"])
        except (KeyError, ValueError, TypeError):
            return
        history = session.screen.history_len
        state.offset = max(0, min(state.offset + lines, history))
        await _synced(session)
        # Echo the clamped result so the client's scroll state (and its
        # auto-unfreeze and affordance) matches the server's truth.
        await ws.send_str(json.dumps({"type": "scrolled", "offset": state.offset}))
        await ws.send_bytes(session.screen.repaint_sequence(state.offset))
    elif kind == "ping":
        await ws.send_str(json.dumps({"type": "pong"}))
    elif kind == "typing":
        # The web terminal's "a human is at this keyboard" mark for keys that
        # have not produced bytes: an IME composing a Hangul syllable, a
        # phone keyboard mid-word, a modifier held. Those keep the composer
        # changing while no BINARY frame arrives, so without this mark a
        # delivery sees a quiet keyboard and types into the half-written
        # line. Same mark as a keystroke frame — it only restarts the
        # TYPING_GUARD window, never writes anything.
        session.note_human_input(at_terminal=True)
