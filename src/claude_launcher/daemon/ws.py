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
  (``{"type":"init","cols":..,"rows":..,"status":..,"pid":..,"boot_id":..,
  "exited":..,"exit_code":..}``), then one binary frame repainting the current
  screen so a fresh viewer sees live state. Because the repaint comes with
  every socket, a client that lost one may simply open another against the
  same terminal: ``pid`` and ``boot_id`` together say whether it is the same
  program it was talking to. ``exited`` says whether there is a program there
  at all — see "Landing on a session that already finished" below.
- client -> server, binary: keystrokes/paste, written verbatim to the PTY.
- text frames are JSON control messages:
  client: ``{"type":"resize","cols":..,"rows":..}``, ``{"type":"repaint"}``
  (resend the current screen — used by viewers on focus regain, since another
  viewer may have resized the session meanwhile),
  ``{"type":"scroll","lines":N}`` (view history held by the daemon: N>0 moves
  further back, N<0 back toward live, anything past the bounds clamps —
  ``-999999`` snaps to live), ``{"type":"focus","focused":bool}`` (whether
  this retained viewer is currently on screen), and ``{"type":"ping"}``.
  server: ``{"type":"state","status":...}``, ``{"type":"exit","code":...}``,
  ``{"type":"resize","cols":..,"rows":..}``, ``{"type":"buffer","alt":..}``
  (the program entered or left the alternate screen — the client learns the
  mode it may not have been connected for),
  ``{"type":"mouse","tracking":bool}`` (the program took the mouse, or gave
  it back — see "Who owns the wheel" below),
  ``{"type":"scrolled","offset":N}``
  (the server's clamped scroll position for this socket, sent before the
  repaint answering a ``scroll``), ``{"type":"pong"}``,
  ``{"type":"notice","id":..,"text":..,"ttl":..,"level":..}`` (a line for
  the person at this viewer, to show over the terminal for ``ttl`` seconds —
  see "Notices" below), and
  ``{"type":"shutdown"}`` — the daemon itself is stopping/restarting, sent
  before its sessions are terminated so a viewer can tell this apart from the
  session's program exiting on its own.

Notices: the daemon (``Session.notify``, ``POST /api/sessions/{name}/notice``)
can say something to whoever is *looking* at a session without typing into
it. Every viewer gets the ``notice`` frame; the web terminal draws it as an
element over its xterm. A viewer whose terminal is a real one — ``claunch
attach`` — cannot draw elements, so it opens the socket with ``?overlay=1``
and the daemon composes the line into the bytes it sends that socket: drawn
over row 1 after each output chunk while the notice is up, row 1 restored
from the rendered grid when it expires (``daemon/notice.py``, which also
keeps the draw out of the middle of a split escape sequence). A client may
also send ``{"type":"notice","text":..,"ttl":..,"level":..}`` itself to put
a line up for its own viewer only — how attach shows what it learned about
the local console, which the daemon cannot know.

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

Landing on a session that already finished: a viewer may attach to a record
whose child is long gone (the web UI's ``#/s/<name>`` for a killed session,
``claunch attach`` on an exited one). Such a socket never receives an ``exit``
frame — that one is published by the child ending, and this one ended before
anybody subscribed — so ``init`` carries ``exited``/``exit_code`` instead, the
same way ``cli_ws`` tells a viewer it landed on a dead shell. A client that
reads only the ``exit`` frame would treat the socket as a live pipe, and the
repaint hands it the program's own mouse modes back (``?1000h``/``?1002h``/
``?1003h`` are in the replayed screen): under ``?1003h`` a mouse *movement*
over the terminal is a report, the report is a write, the write finds no
child, and the socket used to close on it — which a link machine reads as an
outage and answers by reconnecting, repainting, and being closed again. Hence
also the exit frame sent below when a write finds the child gone: a bare
close is the one thing a viewer cannot tell apart from a broken network.

Auth: the route sits under ``/api/``, so the shared middleware enforces the
Bearer header (CLI/scripts — WebSocket client libraries can set headers) or
the login cookie (browsers) before the upgrade completes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from typing import Optional

from aiohttp import WSMsgType, web

from . import connections as conn_mod
from .notice import Notice, Overlay
from .session import Session, SessionGone

log = logging.getLogger("claunch.daemon.ws")


# xterm.js answers OSC 10/11 foreground/background-colour queries through its
# ordinary ``onData`` event. Codex treats those answers as keyboard input:
# the ESC bytes become Escape keypresses and the remaining payload lands in
# its composer. Keep the filter narrow to the complete replies xterm emits
# for Codex, not general terminal control traffic such as cursor or device
# reports.
_CODEX_OSC_COLOR_RESPONSE = re.compile(
    rb"^(?:\x1b](?:10|11);rgb:[0-9A-Fa-f]{1,4}/[0-9A-Fa-f]{1,4}/[0-9A-Fa-f]{1,4}\x1b\\)+$"
)


def _is_codex_osc_color_response(harness: str, data: bytes) -> bool:
    """Whether ``data`` is an automatic xterm colour reply for Codex.

    The reply is a terminal-emulator response rather than a person typing.
    It must therefore neither enter the PTY nor update the terminal draft
    state used to hold automated deliveries.
    """
    return harness == "codex" and bool(_CODEX_OSC_COLOR_RESPONSE.fullmatch(data))


@dataclasses.dataclass
class ViewerState:
    """Per-socket scroll-back state for one terminal viewer.

    A separate instance per WebSocket is what lets one viewer browse history
    while another sits live on the same terminal: each pump suppresses
    ``data`` only for the socket whose offset is non-zero.
    """

    offset: int = 0
    focus_token: Optional[object] = None
    #: The notice up for this viewer (if any) and where its byte stream is;
    #: ``overlay_bytes`` says the viewer asked for notices composed into the
    #: stream (``?overlay=1``) rather than only the control frame.
    overlay: Overlay = dataclasses.field(default_factory=Overlay)
    overlay_bytes: bool = False
    expiry: Optional["asyncio.Task[None]"] = None


def _wants_overlay(request: web.Request) -> bool:
    """Whether this client wants notices drawn into its byte stream.

    Opt-in like the scrollback seed, and for the same reason: a browser has
    somewhere better to put a notice than row 1 of the grid, and a client
    that says nothing must keep getting exactly the program's bytes.
    """
    return request.query.get("overlay") in ("1", "true")


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


def _viewer_left(ws: web.WebSocketResponse, where: str) -> None:
    """A frame could not be written because the viewer had already gone.

    Closing a tab, a laptop lid, or ``claunch attach`` under ^C drops the
    socket mid-frame, and aiohttp reports that as a
    ``ClientConnectionResetError`` ("Cannot write to closing transport") out
    of whichever send was in flight. There is nothing to recover: the reader
    is gone and the socket is finished either way. Left to propagate it
    reaches the server's own handler and writes a twenty-line traceback for
    an event that is ordinary — enough of them to make the daemon log
    unreadable — so it is caught, noted at debug, and the handler returns.
    """
    log.debug("viewer disconnected mid-frame (%s)", where)


#: How long a repaint or scroll waits for the rendered grid to catch up with
#: the byte stream before it is answered from the grid as it stands. The
#: wait is unbounded by nature — the feeder's queue empties only when the
#: program pauses — and a viewer's output pump (:func:`_unfreeze`) and its
#: sync lane both sit in it, so it is capped here: a snapshot two seconds
#: behind a flooding session is a usable screen, and the next chunk repaints
#: it anyway.
SYNC_TIMEOUT = 2.0

#: How often the daemon pings an attached socket. aiohttp allows half of it
#: for the pong and closes the socket when none comes back, so this number
#: is also how long a viewer may go without answering: 60 gives it 30
#: seconds.
#:
#: It was 30 (a 15-second window), and 15 seconds is inside what a loaded
#: browser takes. A second dashboard page is the case that was reported: two
#: pages share one renderer, the renderer is what answers for the page, and
#: while it is busy the ping goes unanswered. The daemon then closes the
#: socket -- ``terminal websocket closed code=1006 error=TimeoutError('No
#: PONG received after 15.0 seconds')``, five of them in the daemon log of
#: 2026-09-18 -- the page reconnects into the same load and is closed again,
#: which is the terminal that will not come up (claunch-u6lz).
#:
#: A dead peer is still found, 60 seconds later than before; nothing else
#: reads this number. What it may not be is unbounded: a socket nobody is on
#: the other end of holds a viewer subscription and its screen.
#:
#: The receive loop is the only place a pong is read, so anything that
#: blocks that loop for a whole window costs the socket whatever this is set
#: to -- which is why the frames a fresh socket opens with are written from
#: the sender task (see :func:`terminal_ws`). A module constant so tests can
#: shorten it.
HEARTBEAT = 60.0



async def _synced(session, timeout: float = SYNC_TIMEOUT) -> None:
    """Let the rendered grid catch up before it is replayed to a viewer.

    A repaint is a snapshot: sent mid-render it would show a half-drawn
    screen and stay that way until the next output arrived. Dead sessions
    have no feeder and nothing pending, hence the getattr. Bounded by
    ``timeout`` (see :data:`SYNC_TIMEOUT`).
    """
    synced = getattr(session, "screen_synced", None)
    if synced is None:
        return
    try:
        await asyncio.wait_for(synced(), timeout)
    except asyncio.TimeoutError:
        log.debug("render sync timed out after %.1fs; repainting as is", timeout)


class _SyncLane:
    """The controls that wait on the rendered grid, served off the receive loop.

    ``repaint`` and ``scroll`` answer with a snapshot of the grid, so they
    wait for the feeder to catch up (:func:`_synced`). Awaited inline in the
    socket's receive loop, that wait held every keystroke behind it: a
    person who switched into a Codex session mid-burst (attach sends a
    repaint on focus-in; the web terminal a scroll before any key typed
    while scrolled back) then typed, pressed Escape, pressed Ctrl-C — and
    nothing reached the PTY until the burst ended, while the session-line
    box, which goes through ``/keys`` and not this socket, kept working
    (2026-09-11, claunch-wpd0).

    So those two controls queue here and one task serves them in order,
    and keystrokes never wait behind them. Frames waiting together are
    coalesced: scroll deltas sum, and a repaint is subsumed by a scroll,
    which repaints anyway. Everything else (resize, typing, focus, ping,
    notice) is cheap and stays inline.
    """

    def __init__(self, ws, session, state: "ViewerState", opened=None) -> None:
        self._ws = ws
        self._session = session
        self._state = state
        self._scroll = 0
        self._scrolled = False
        self._repaint = False
        self._task: Optional[asyncio.Task] = None
        #: The socket's opening frames, still being written (terminal_ws).
        #: A snapshot served ahead of them would paint over a grid the viewer
        #: has not been sent yet, so the lane holds until they are out.
        self._opened = opened

    @property
    def busy(self) -> bool:
        """Whether a control is being served (or waiting to be) right now."""
        return self._task is not None and not self._task.done()

    def submit(self, raw: str) -> bool:
        """Take ``raw`` if it is a control this lane serves; False otherwise,
        and the caller handles it inline."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        kind = msg.get("type")
        if kind == "scroll":
            try:
                lines = int(msg["lines"])
            except (KeyError, ValueError, TypeError):
                return True  # malformed, and _handle_control would drop it too
            self._scroll += lines
            self._scrolled = True
        elif kind == "repaint":
            self._repaint = True
        else:
            return False
        if not self.busy:
            self._task = asyncio.get_running_loop().create_task(self._run())
        return True

    async def _run(self) -> None:
        try:
            if self._opened is not None:
                await self._opened.wait()
            while self._scrolled or self._repaint:
                if self._scrolled:
                    lines, self._scroll = self._scroll, 0
                    self._scrolled = self._repaint = False
                    raw = json.dumps({"type": "scroll", "lines": lines})
                else:
                    self._repaint = False
                    raw = json.dumps({"type": "repaint"})
                await _handle_control(self._ws, self._session, raw, self._state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the socket is going, or went
            log.debug("sync lane stopped", exc_info=True)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


async def terminal_ws(request: web.Request) -> web.WebSocketResponse:
    """The terminal on a socket of its own: one upgrade, one session.

    Still the only way a non-browser client attaches (``claunch attach``),
    and still what the dashboard falls back to. The dashboard's own path is
    now :func:`attach_terminal` over a shared socket -- same protocol, same
    code below, one connection for every session a tab visits.
    """
    # Auth already happened: this route lives under /api/, so the middleware
    # validated a Bearer header (CLI/scripts) or the session cookie (the SPA
    # calls /api/auth/session before opening any terminal socket).
    manager = request.app["manager"]
    session: Session = manager.get(request.match_info["name"])

    ws = web.WebSocketResponse(heartbeat=HEARTBEAT)
    await ws.prepare(request)

    request.app["websockets"].add(ws)
    # What this socket is, for `GET /api/connections` and the log line below.
    # Written at the open, not only at the close: a viewer that cannot get a
    # socket up leaves nothing behind, so "how many are open right now" is the
    # only reading that answers whether new ones are being refused.
    conns = conn_mod.install(request.app)
    record = conns.opened("terminal", session.sdef.name, request, ws=ws)
    log.info(
        "terminal websocket opened session=%s peer=%s:%s open=%d",
        session.sdef.name, record["peer_ip"], record["peer_port"],
        conns.open_count(),
    )
    try:
        await attach_terminal(
            ws,
            session,
            request.app,
            want_scrollback=_wants_scrollback(request),
            overlay=_wants_overlay(request),
        )
    finally:
        request.app["websockets"].discard(ws)
        if not ws.closed:
            await ws.close()
        conns.closed(record, ws.close_code, ws.exception())
        log.info(
            "terminal websocket closed session=%s code=%s error=%r open=%d",
            session.sdef.name, ws.close_code, ws.exception(),
            conns.open_count(),
        )
    return ws


async def attach_terminal(
    ws,
    session: Session,
    app,
    *,
    want_scrollback: bool,
    overlay: bool,
) -> None:
    """Attach ``ws`` to ``session`` and serve it until one of them ends.

    ``ws`` is anything with the small write/read surface a socket has:
    :class:`aiohttp.web.WebSocketResponse` when the terminal has an upgrade
    to itself, :class:`daemon.channel.ChannelSocket` when it is one channel
    of a shared one. Everything the protocol is lives here, once, so the two
    carriers cannot drift into two dialects.

    The caller owns the socket: it registers and closes it, and it writes
    the connection record. This owns the attachment.
    """
    queue = session.subscribe()
    # Someone is looking at this session. This route is the only way to watch
    # one — the web terminal and `claunch attach` both arrive here — so this
    # is where a visit is, and the rail's "last looked in" line is stamped on
    # the socket's two edges rather than on a timer.
    session.note_visit()
    state = ViewerState(focus_token=queue, overlay_bytes=overlay)
    boot_id = app["boot_id"]
    # Set once the frames a fresh socket opens with have all been written.
    # The controls that answer with a snapshot wait for it, so nothing is
    # painted over a screen the viewer has not been given yet.
    opened = asyncio.Event()

    async def prologue() -> None:
        """The frames every fresh socket opens with: init, the scrollback it
        asked for, then the grid as it stands.

        Written from the sender task, not from this coroutine before the
        receive loop starts. A client that is slow to drain (a browser with a
        second dashboard page competing for the same renderer) stops reading
        its socket, the write blocks on the full window, and a receive loop
        that has not started yet reads no PONG -- so aiohttp closes the
        socket after :data:`HEARTBEAT`/2 and the viewer sees a terminal that
        never comes up, retrying into the same wedge. Measured 2026-09-18:
        ``terminal websocket closed code=1006 error=TimeoutError('No PONG
        received after 15.0 seconds')`` against a client that stopped reading
        (claunch-u6lz). With the write on the sender task the receive loop is
        already running, so the pong is answered while the seed drains.
        """
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
                    "boot_id": boot_id,
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
                    # And whether there is a program on the other end at all.
                    # A viewer that arrives after the child is gone will never
                    # be sent an ``exit`` frame (nothing is left to publish
                    # one), so this is the only place it can learn that what
                    # it is looking at is a final screen rather than a live
                    # terminal. Same field, for the same reason, as the one
                    # ``cli_ws`` puts on a dead shell's init.
                    "exited": bool(getattr(session, "exited", False)),
                    "exit_code": getattr(session, "exit_code", None),
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
        if want_scrollback and not session.screen.alt_screen:
            seed = session.screen.history_sequence()
            if seed:
                await ws.send_bytes(seed)
        await ws.send_bytes(session.screen.repaint_sequence(0))
        opened.set()

    try:
        sender = asyncio.ensure_future(
            _pump_to_client(ws, queue, session, state, prologue=prologue)
        )
        # The writes used to be inline, so a socket that died under them
        # ended this coroutine. They are on the sender task now, and a task
        # that stops has no way back here -- the receive loop would sit on a
        # socket nothing writes to. Closing it ends that loop, which is the
        # same exit the inline write produced.
        sender.add_done_callback(
            lambda t: None if t.cancelled() else asyncio.ensure_future(ws.close())
        )
        lane = _SyncLane(ws, session, state, opened=opened)
        try:
            await _pump_from_client(ws, session, state, lane)
        finally:
            lane.close()
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, Exception):
                pass
    except ConnectionResetError:
        _viewer_left(ws, "terminal")
    finally:
        if state.expiry is not None:
            state.expiry.cancel()
        session.set_viewer_focused(queue, False)
        session.unsubscribe(queue)
        # ...and the visit ended now, not when it started. A tab open all
        # afternoon would otherwise report this morning.
        session.note_visit()


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

    ws = web.WebSocketResponse(heartbeat=HEARTBEAT)
    await ws.prepare(request)
    request.app["websockets"].add(ws)
    conns = conn_mod.install(request.app)
    record = conns.opened("cli", "(cli shell)", request, ws=ws)

    # First viewer of this daemon incarnation brings the shell up; afterwards
    # it lives on its own until it exits (see ShellPty.start_once).
    shell.start_once()
    queue, replay = shell.attach()

    async def prologue() -> None:
        """This socket's opening frames, written from the sender task.

        Same reason as the session terminal's (see :func:`terminal_ws`): the
        replay ring can be large, a client that has stopped draining blocks
        the write, and a receive loop that has not started yet answers no
        PONG -- which costs the socket after :data:`HEARTBEAT`/2.
        """
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

    try:
        sender = asyncio.ensure_future(_pump_cli(ws, queue, prologue=prologue))
        sender.add_done_callback(
            lambda t: None if t.cancelled() else asyncio.ensure_future(ws.close())
        )
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
    except ConnectionResetError:
        _viewer_left(ws, "cli")
    finally:
        request.app["websockets"].discard(ws)
        shell.unsubscribe(queue)
        if not ws.closed:
            await ws.close()
        conns.closed(record, ws.close_code, ws.exception())
    return ws


async def _pump_cli(
    ws: web.WebSocketResponse, queue: asyncio.Queue, prologue=None
) -> None:
    if prologue is not None:
        await prologue()
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


async def _pump_from_client(
    ws,
    session: Session,
    state: ViewerState,
    lane: "_SyncLane",
) -> None:
    """The socket's receive loop: keystrokes to the PTY, controls to their
    handlers. Returns when the socket ends.

    Keystrokes (BINARY) are written here, inline, and must never wait behind
    a control: the two controls that answer with a snapshot of the rendered
    grid (``repaint``, ``scroll``) go to ``lane`` instead, because their wait
    for the render to catch up (:func:`_synced`) is unbounded while the
    program keeps writing.
    """
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
            if _is_codex_osc_color_response(session.sdef.harness, msg.data):
                continue
            session.note_human_input(at_terminal=True, data=msg.data)
            try:
                await session.write_bytes(msg.data)
            except SessionGone:
                # The child is gone — it died under this socket, or it
                # was already gone when the viewer arrived and the
                # repaint handed its terminal the program's mouse
                # modes back, so a mouse movement became a write. Say
                # which before the socket ends: a close with nothing
                # behind it is indistinguishable from a dropped
                # network, and a client that guesses "network" will
                # reconnect into the very same repaint and do this
                # again. A duplicate of the pump's own exit frame is
                # harmless — clients act on the first.
                try:
                    await ws.send_str(
                        json.dumps(
                            {
                                "type": "exit",
                                "code": getattr(session, "exit_code", None),
                            }
                        )
                    )
                except Exception:  # noqa: BLE001 — the socket is going anyway
                    pass
                break
        elif msg.type == WSMsgType.TEXT:
            if not lane.submit(msg.data):
                await _handle_control(ws, session, msg.data, state)
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break

async def _pump_to_client(
    ws: web.WebSocketResponse,
    queue: asyncio.Queue,
    session: Session,
    state: ViewerState,
    prologue=None,
) -> None:
    """Everything this socket writes, on one task.

    ``prologue`` is the socket's opening frames when the caller has them
    (terminal_ws). They are written here rather than inline before the
    receive loop starts, because a write to a client that has stopped
    draining blocks until the window opens, and while that write is
    outstanding nothing reads the socket -- including the PONG that keeps
    aiohttp from closing it (see :data:`HEARTBEAT`). The queue is subscribed
    before this task is created, so nothing the session printed meanwhile is
    lost, and it is drained only after the prologue has gone out.
    """
    if prologue is not None:
        await prologue()
    while True:
        kind, payload = await queue.get()
        if kind == "data":
            if state.offset > 0:
                continue  # frozen: the viewer reads history, not live bytes
            if state.overlay_bytes:
                # One frame: the chunk, then the notice redrawn over row 1
                # (nothing when no notice is up, or the chunk ends inside a
                # sequence the program has not finished).
                payload = payload + state.overlay.after_output(
                    payload, session.screen.cols
                )
            await ws.send_bytes(payload)
        elif kind == "notice":
            await _show_notice(ws, session, state, payload)
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
    await ws.send_bytes(session.screen.repaint_sequence(0) + _redraw(session, state))


def _redraw(session: Session, state: ViewerState) -> bytes:
    """The notice drawn again, to follow a repaint that just painted over
    it; empty for a viewer without one (or without the overlay)."""
    if not state.overlay_bytes or state.overlay.notice is None:
        return b""
    return state.overlay.draw(session.screen.cols)


async def _show_notice(
    ws: web.WebSocketResponse, session: Session, state: ViewerState, notice: Notice
) -> None:
    """Put ``notice`` up for this viewer: the control frame always, the
    row-1 draw for a viewer that asked for it, and the timer that takes it
    down. A notice arriving while one is up replaces it and its timer."""
    await ws.send_str(json.dumps(notice.frame()))
    if state.overlay_bytes:
        data = state.overlay.show(notice, session.screen.cols)
        if data:
            await ws.send_bytes(data)
    else:
        state.overlay.notice = notice
    if state.expiry is not None:
        state.expiry.cancel()
    state.expiry = asyncio.ensure_future(_expire_notice(ws, session, state, notice))


async def _expire_notice(
    ws: web.WebSocketResponse, session: Session, state: ViewerState, notice: Notice
) -> None:
    await asyncio.sleep(notice.ttl)
    if state.overlay.notice is not notice:
        return  # replaced meanwhile; the newer one's timer owns the clear
    if not state.overlay_bytes:
        state.overlay.notice = None
        return
    try:
        # The grid must have caught up with the bytes this viewer has seen,
        # or row 1 would be restored to something older than what the
        # program last drew there.
        await _synced(session)
        data = state.overlay.clear(session.screen.row_sequence(0, state.offset))
        if data and not ws.closed:
            await ws.send_bytes(data)
    except (ConnectionResetError, RuntimeError):
        _viewer_left(ws, "notice")


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
        await ws.send_bytes(
            session.screen.repaint_sequence(state.offset) + _redraw(session, state)
        )
    elif kind == "notice":
        # The viewer putting a line up for itself: what attach learned about
        # the terminal it is running in, which only that client can know.
        # Same path as a daemon notice, addressed to this socket alone.
        text = msg.get("text")
        if isinstance(text, str) and text.strip():
            await _show_notice(
                ws,
                session,
                state,
                Notice.make(text, ttl=msg.get("ttl"), level=str(msg.get("level") or "info")),
            )
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
        await ws.send_bytes(
            session.screen.repaint_sequence(state.offset) + _redraw(session, state)
        )
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
    elif kind == "focus":
        # Parked web terminals retain their sockets to preserve their local
        # state. Attachment alone therefore does not say which terminal is
        # currently visible to a person.
        if state.focus_token is not None:
            session.set_viewer_focused(
                state.focus_token, bool(msg.get("focused"))
            )
