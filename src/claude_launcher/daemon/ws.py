"""WebSocket terminal attachment for a session.

Protocol (matches the SPA's app.js and any non-browser client):

Query parameters: ``?scrollback=1`` asks to be seeded with the daemon's
scrollback (one binary frame, before the repaint, main buffer only) so the
client's own terminal can serve the wheel natively. Off by default — the seed
is up to five thousand lines, which a browser has somewhere to put and a
terminal on the end of ``claunch attach`` does not. A client that asks for
nothing gets what it always got.

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
  mode it may not have been connected for),
  ``{"type":"mouse","tracking":bool}`` (the program took the mouse, or gave
  it back — see "Who owns the wheel" below),
  ``{"type":"scrolled","offset":N}``
  (the server's clamped scroll position for this socket, sent before the
  repaint answering a ``scroll``), ``{"type":"pong"}``, and
  ``{"type":"shutdown"}`` — the daemon itself is stopping/restarting, sent
  before its sessions are terminated so a viewer can tell this apart from the
  session's program exiting on its own.

Who owns the wheel: a program that turns mouse tracking on (``?1000h`` and
friends — claude does, behind the alternate screen, and leaves it on) is asking
for wheel ticks itself. It scrolls its own view from its own model, to a depth
no terminal could reconstruct, and the client must forward the ticks as mouse
reports rather than spend them on anything else. Measured on this project's own
sessions, a claude terminal yields **one or two lines** of daemon-side history
for four hundred kilobytes of output — it repaints the whole grid every frame
instead of scrolling it — so the virtual scroll below has nothing to serve such
a session anyway. ``init.mouse`` and the ``mouse`` frame say which regime a
socket is in; ``ScreenState.repaint_sequence`` re-asserts the modes themselves
so a late-joining terminal reports the wheel like an early one.

Virtual scroll (for the other regime — a program that leaves the mouse alone):
while a socket is scrolled back (``scrolled.offset > 0``) the
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


def _wants_scrollback(request: web.Request) -> bool:
    """Whether this client asked to be seeded with the daemon's scrollback.

    Opt-in, and deliberately so. The seed is worth up to
    :data:`~claude_launcher.daemon.screen.HISTORY_SEED_LINES` lines, which is
    what a browser's xterm wants (it has a scrollback to put them in, and the
    wheel over it is then the browser's own) and what a terminal on the other
    end of ``claunch attach`` did not ask for. Absent the flag nothing is
    sent — so a client that says nothing keeps the behaviour it has always
    had, and one written later inherits the quiet side by default rather than
    having to know to turn it off.
    """
    return request.query.get("scrollback") in ("1", "true")


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
                    # And whether the program has taken the mouse. When it
                    # has, wheel ticks are the program's — it scrolls its own
                    # view, deeper than any scrollback the daemon could keep —
                    # and a viewer that swallows them to scroll something else
                    # leaves the program believing nobody touched the wheel.
                    "mouse": session.screen.mouse_tracking,
                }
            )
        )
        await _synced(session)
        # On the main buffer, hand the viewer the scrollback before the grid,
        # so its own terminal holds what the daemon holds and the wheel below
        # it is the browser's own.
        #
        # Only when the client asked (``?scrollback=1``). The default is to
        # send nothing, and that direction is the point: a viewer that says
        # nothing gets what it has always got. Seeding by default would push
        # up to five thousand lines into `claunch attach`'s terminal — a
        # change nobody opted into, on a client that never asked for a
        # scrollback and cannot use one the way a browser does. "Say nothing,
        # get nothing" also means the next client to arrive inherits the safe
        # side rather than this defect.
        #
        # Skipped on the alternate screen whatever the client asked: those
        # rows would land in a buffer that keeps no scrollback, and a program
        # there has usually taken the mouse anyway.
        if _wants_scrollback(request) and not session.screen.alt_screen:
            seed = session.screen.history_sequence()
            if seed:
                await ws.send_bytes(seed)
        await ws.send_bytes(session.screen.repaint_sequence(0))

        sender = asyncio.ensure_future(_pump_to_client(ws, queue, session, state))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    # A binary frame is a human at a keyboard (attach or the
                    # web terminal); the mark parks automated deliveries so
                    # they don't type into a message being composed. The
                    # keystrokes go with it, because *when* they last typed
                    # is only half the question — the other half is whether
                    # what they typed is still sitting in the composer
                    # unsent, and only these bytes can say (Session.
                    # note_human_input / draft_state_from_bytes).
                    session.note_human_input(at_terminal=True, data=msg.data)
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


# --------------------------------------------------------------------------- #
# the CLI tab: one raw, unmanaged shell (daemon/clipty.py)
#
# A smaller protocol than the session terminal's, for exactly what a bare
# shell needs. Same frame lanes: binary output both ways, JSON control
# frames. There is no scrollback negotiation or viewer state — the browser
# keeps xterm's own scrollback, the daemon keeps no screen — so the control
# set is just init (may carry ``exited`` for a shell that is already dead
# when a viewer attaches), resize and restart.
# --------------------------------------------------------------------------- #
async def cli_ws(request: web.Request) -> web.WebSocketResponse:
    # Auth already happened: /api/ prefix, so the shared middleware validated
    # a Bearer header (CLI/scripts) or the session cookie (the SPA).
    shell = request.app["shell"]

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    request.app["websockets"].add(ws)

    # First viewer of this daemon incarnation brings the shell up; afterwards
    # it lives on its own until it exits (see ShellPty.start_once).
    shell.start_once()
    queue, replay = shell.attach()
    try:
        await ws.send_str(
            json.dumps(
                {
                    "type": "init",
                    "cols": shell.cols,
                    "rows": shell.rows,
                    "pid": shell.pid,
                    # True when the viewer joined after the shell already
                    # died — the client shows the restart control instead of
                    # pretending keystrokes can land anywhere.
                    "exited": shell.exited,
                }
            )
        )
        # The ring: what this shell printed while no one was watching, so a
        # fresh viewer is caught up before the live stream starts.
        if replay:
            await ws.send_bytes(replay)

        sender = asyncio.ensure_future(_pump_cli(ws, queue))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await shell.write_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await _cli_control(ws, shell, msg.data)
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
        shell.unsubscribe(queue)
        if not ws.closed:
            await ws.close()
    return ws


async def _pump_cli(ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
    while True:
        kind, payload = await queue.get()
        if kind == "data":
            await ws.send_bytes(payload)
        elif kind == "exit":
            await ws.send_str(json.dumps({"type": "exit", "code": payload}))
        elif kind == "resize":
            cols, rows = payload
            await ws.send_str(
                json.dumps({"type": "resize", "cols": cols, "rows": rows})
            )
        elif kind == "init":
            # A restart: the child was replaced under this socket. Say which
            # incarnation it is now, so the viewer leaves its "exited" state.
            cols, rows, pid = payload
            await ws.send_str(
                json.dumps(
                    {
                        "type": "init",
                        "cols": cols,
                        "rows": rows,
                        "pid": pid,
                        "exited": False,
                    }
                )
            )


async def _cli_control(
    ws: web.WebSocketResponse, shell, raw: str
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
            shell.resize(int(msg["cols"]), int(msg["rows"]))
        except (KeyError, ValueError, TypeError):
            pass
    elif kind == "restart":
        shell.restart()


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
        elif kind == "mouse":
            if state.offset > 0 and payload:
                # The program just took the mouse, and this viewer is holding
                # a frozen snapshot the wheel can no longer move — from here
                # the wheel belongs to the program. Drop to live first, or the
                # viewer is stranded in a history nothing will scroll out of.
                await _unfreeze(ws, session, state)
            await ws.send_str(json.dumps({"type": "mouse", "tracking": payload}))
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
        # Sync FIRST: the render is deferred (ScreenFeeder), so history_len
        # read before this counts only the lines that have reached the grid.
        # Clamping against that number pins the viewer short of the newest
        # history — and while a burst is still rendering it can be 0, which
        # clamps every scroll to 0 and reads as a wheel that does nothing.
        # It is worst exactly when the session is busy, which is when someone
        # reaches for the wheel.
        await _synced(session)
        history = session.screen.history_len
        state.offset = max(0, min(state.offset + lines, history))
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
        #
        # ``draft`` is the client saying the event was text going *into* the
        # composer (a composition, an input event) rather than a bare key it
        # cannot classify. Only that opens a draft: a held modifier must not
        # leave one open behind it, since nothing the person types next would
        # ever close it.
        session.note_human_input(
            at_terminal=True, composing=bool(msg.get("draft"))
        )
