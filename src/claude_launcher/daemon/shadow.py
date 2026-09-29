"""Remote-shadow sessions: another daemon's mesh members, seen from here.

Two daemons joined through a relay share meshes (``daemon/mesh.py``), and a
mesh's roster names sessions that run on the other daemon. This module lets
the operator of one daemon *look at* those sessions without being given the
other daemon's control surface:

* a **card** -- name, handle, role, note, the cached briefing digest, the
  cflow position and the session's status, and nothing else;
* a **terminal** that only outputs -- the same init/repaint/PTY-bytes
  protocol the local terminal speaks (``daemon/ws.py``), carried over a live
  relay bridge (``RelayUplink.peer_open``);
* the **session line** ("type for this session") -- one line of text,
  journaled with the viewing daemon as its origin.

The permissions are the point, and they are enforced where the session is
(the *host*), not where it is viewed:

* Every ``/peer/shadow/*`` call carries the mesh link token of the viewing
  daemon, and the target must be a member of THAT mesh on the host
  (``MeshManager.peer_shadow_member``) -- the rule peer ops already read by.
  A session in no shared mesh cannot be shadowed at all.
* The terminal is served through :class:`HostSocket`, whose receive side
  never yields a message: ``attach_terminal`` gets no keystroke, no resize,
  no scroll, nothing -- by construction, not by a filter someone can widen.
* The session line refuses key names (``Escape``, ``C-c``), never submits a
  person's half-typed line on the host, never queues into an exited session,
  and can be switched off by the host (``daemon.shadow_input: false``).
* Kill, pause, note editing, raw keys, image paste and every other control a
  local card has are simply absent: there is no endpoint to call.

On the viewing side the shadow is kept apart from local sessions for the
same reason: every local cache and route is keyed by the bare session name,
and a remote ``s1`` must never be taken for the local ``s1``. Shadows are
addressed by ``(machine, session)`` and served under ``/api/shadows``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import AsyncIterator, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web

from . import briefing, keys as keys_mod
from . import session_input
from .mesh import MeshError, PeerUnreachable
from .peer_client import PeerHttpError, ResponseStream
from .session import KeyboardHeld, SessionGone

log = logging.getLogger("claunch.daemon.shadow")

# --------------------------------------------------------------------------- #
# the stream's framing: [kind u8][len u32 BE][payload]
# --------------------------------------------------------------------------- #
#: A JSON control frame of the terminal protocol (init, state, exit, resize...).
FRAME_TEXT = 0x01
#: Raw PTY output bytes.
FRAME_BYTES = 0x02
#: Nothing: the host proving the viewer is still there (a write to a viewer
#: that left fails, which is how an idle session's stream finds out).
FRAME_KEEPALIVE = 0x03
_HEAD = struct.Struct(">BI")
#: A frame larger than this is a broken stream, not a big repaint.
MAX_FRAME = 8 * 1024 * 1024
#: How often the host writes a keepalive frame on an otherwise quiet stream.
KEEPALIVE = 15.0
#: How often the host checks whether the viewer's connection is still there.
#: A viewer that leaves closes its bridge, and the loopback connection under
#: the request closes with it. On aiohttp 3.14 that alone cancels this
#: handler (measured: 0.3ms after the close, tests/test_shadow.py
#: test_quiet_session_releases_a_viewer_that_left). This check is the second
#: way out, for a server that does not cancel: the attachment -- a
#: subscription to the session -- is then released within this long rather
#: than at the next write, which a quiet session may never make.
WATCH_INTERVAL = 1.0


def pack(kind: int, payload: bytes = b"") -> bytes:
    return _HEAD.pack(kind, len(payload)) + payload


class FrameReader:
    """Split a byte stream back into ``(kind, payload)`` frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        self._buf += data
        out: List[Tuple[int, bytes]] = []
        while len(self._buf) >= _HEAD.size:
            kind, size = _HEAD.unpack_from(self._buf)
            if size > MAX_FRAME:
                raise PeerHttpError(f"shadow frame too large ({size} bytes)")
            if len(self._buf) < _HEAD.size + size:
                break
            payload = bytes(self._buf[_HEAD.size:_HEAD.size + size])
            del self._buf[:_HEAD.size + size]
            out.append((kind, payload))
        return out


# --------------------------------------------------------------------------- #
# host side: what a linked peer may see of one of our members
# --------------------------------------------------------------------------- #
#: The cflow fields a card carries. A position, not a run: no journal, no
#: instructions, no reports -- those are the host's to show.
CFLOW_FIELDS = (
    "workflow", "run", "step_id", "title", "status", "visit",
    "steps_completed", "chooser", "reason",
)
#: The briefing digest fields a card carries (``briefing.digest``).
BRIEFING_FIELDS = ("one_line", "state", "goal", "now", "progress")


def cflow_position(entry: Optional[dict]) -> Optional[dict]:
    """A run entry (``api._cflow_entry``) cut to the card's fields; None
    when the session drives no run."""
    if not isinstance(entry, dict):
        return None
    if entry.get("status") in (None, "idle", "error"):
        return None
    return {k: entry[k] for k in CFLOW_FIELDS if entry.get(k) is not None}


def card(session, member, *, cflow: Optional[dict] = None) -> dict:
    """The card of one of our members, as a peer is shown it.

    A whitelist, deliberately: anything not named here (cwd, args, profile,
    branch, pid, issue, token usage...) stays on this daemon.
    """
    sdef = session.sdef
    digest = briefing.digest(sdef.name) or None
    if digest is not None:
        digest = {k: digest[k] for k in BRIEFING_FIELDS if digest.get(k)}
    return {
        "session": sdef.name,
        "handle": member.handle,
        "role": member.role,
        "roles": list(member.roles),
        "harness": sdef.harness,
        "status": session.status(),
        "exited": bool(getattr(session, "exited", False)),
        "note": getattr(sdef, "note", None) or None,
        "briefing": digest or None,
        "cflow": cflow_position(cflow),
    }


class HostSocket:
    """The socket ``ws.attach_terminal`` serves a shadow viewer through.

    Its write side frames the terminal protocol onto a streamed HTTP
    response; its read side yields nothing, ever, and only ends when the
    stream does. So the attachment's receive loop -- the one place a
    keystroke, a resize or a scroll is taken from a viewer -- never runs its
    body for a shadow. There is nothing to filter because nothing arrives.
    """

    def __init__(self, resp: web.StreamResponse) -> None:
        self._resp = resp
        self._lock = asyncio.Lock()
        self._ended = asyncio.Event()
        self._close_code: Optional[int] = None
        self._exception: Optional[BaseException] = None

    async def send_str(self, data: str) -> None:
        await self._write(pack(FRAME_TEXT, data.encode("utf-8")))

    async def send_bytes(self, data: bytes) -> None:
        await self._write(pack(FRAME_BYTES, bytes(data)))

    async def keepalive(self) -> None:
        await self._write(pack(FRAME_KEEPALIVE))

    async def _write(self, frame: bytes) -> None:
        if self._ended.is_set():
            return
        try:
            async with self._lock:
                await self._resp.write(frame)
        except (ConnectionError, OSError, RuntimeError) as exc:
            self._exception = exc
            self._end(1006)

    def _end(self, code: int) -> None:
        if self._ended.is_set():
            return
        self._close_code = code
        self._ended.set()

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self._end(code)

    @property
    def closed(self) -> bool:
        return self._ended.is_set()

    @property
    def close_code(self) -> Optional[int]:
        return self._close_code

    def exception(self) -> Optional[BaseException]:
        return self._exception

    def __aiter__(self) -> "HostSocket":
        return self

    async def __anext__(self):
        await self._ended.wait()
        raise StopAsyncIteration


async def serve_stream(request: web.Request, session, app) -> web.StreamResponse:
    """Serve ``session``'s terminal, output only, to a linked peer."""
    from . import ws as ws_mod

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/x-claunch-shadow",
            "Cache-Control": "no-store",
        },
    )
    await resp.prepare(request)
    sock = HostSocket(resp)

    async def keepalive() -> None:
        quiet = 0.0
        while not sock.closed:
            await asyncio.sleep(WATCH_INTERVAL)
            transport = request.transport
            if transport is None or transport.is_closing():
                await sock.close(code=1006)
                return
            quiet += WATCH_INTERVAL
            if quiet >= KEEPALIVE:
                quiet = 0.0
                await sock.keepalive()

    app["websockets"].add(sock)
    beat = asyncio.ensure_future(keepalive())
    try:
        await ws_mod.attach_terminal(
            sock, session, app, want_scrollback=False, overlay=False,
        )
    except (ConnectionError, OSError):
        pass
    finally:
        beat.cancel()
        app["websockets"].discard(sock)
    return resp


#: The longest line a peer may type (characters).
MAX_LINE = 20000
#: How long the session line waits for a person typing on the host to pause.
#: Shorter than the local line's hold: the call is one relay round trip, and
#: the answer to a busy keyboard is "try again", never "type over it".
REMOTE_TYPING_WAIT = 10.0


class ShadowRefused(Exception):
    """A shadow request the host turns down; ``status`` is its HTTP code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def input_enabled(cfg: Optional[dict]) -> bool:
    """Whether this daemon takes the session line from peers
    (``daemon.shadow_input``, on unless set false)."""
    if not isinstance(cfg, dict):
        return True
    value = cfg.get("shadow_input", True)
    return value is not False and str(value).lower() not in ("0", "false", "no", "off")


async def type_line(session, text, input_id, *, origin: str) -> dict:
    """Type one line from a peer into ``session``: the session line, only.

    Text only: a line that is a key name (``Escape``, ``C-c``, ``Up``) is
    pasted as the words it spells, and a multi-line one as one paste --
    never as keystrokes. A person with an unsent line on the host is never
    typed over and never has that line submitted for them; the peer is
    told to retry. Journaled in the session's input log with ``origin``.
    """
    if not isinstance(text, str) or not text.strip():
        raise ShadowRefused(400, "'text' must be a non-empty string")
    if len(text) > MAX_LINE:
        raise ShadowRefused(400, f"a line is at most {MAX_LINE} characters")
    if not isinstance(input_id, str) or not input_id.strip():
        raise ShadowRefused(400, "'input_id' must be a non-empty string")
    name = session.sdef.name
    if getattr(session, "exited", False):
        raise ShadowRefused(409, f"session {name!r} has exited — nothing was typed")
    prior = session_input.latest(name, input_id)
    if prior and prior.get("status") == "sent":
        return {"ok": True, "bytes": 0, "duplicate": True}
    quiet = await session.await_keyboard_quiet(
        terminal_only=True, timeout=REMOTE_TYPING_WAIT
    )
    if not quiet and session.draft_open():
        raise ShadowRefused(
            409,
            f"session {name!r}: someone is typing there right now — "
            f"nothing was sent. Retry in a moment.",
        )
    session_input.write(name, "input_accepted", request_id=input_id, text=text,
                        status="accepted", pid=session.pid, origin=origin)
    single = "\n" not in text and "\r" not in text
    try:
        if single and keys_mod.has_text([text]):
            data = await session.send_keys([text, "Enter"])
        else:
            data = await session.paste(text, enter=True)
    except (KeyboardHeld, SessionGone) as exc:
        session_input.write(name, "input_failed", request_id=input_id, text=text,
                            status="failed", pid=session.pid, origin=origin)
        raise ShadowRefused(409, str(exc)) from None
    except Exception:
        session_input.write(name, "input_failed", request_id=input_id, text=text,
                            status="failed", pid=session.pid, origin=origin)
        raise
    session_input.write(name, "input_sent", request_id=input_id, text=text,
                        status="sent", pid=session.pid, origin=origin)
    return {"ok": True, "bytes": len(data)}


# --------------------------------------------------------------------------- #
# viewer side
# --------------------------------------------------------------------------- #
#: How long a fetched card answers for its (mesh, machine) before it is
#: asked again. The rail polls every few seconds; the host is asked at most
#: this often however many pages are open.
CARD_TTL = 5.0
#: How long one card fetch may take before its row reads as unreachable.
CARD_TIMEOUT = 8.0


class Directory:
    """The viewer's list of shadows, with each host's cards cached.

    Built fresh from the mesh rosters on every read (:meth:`MeshManager.
    shadow_targets`), so a member that leaves disappears at once; only the
    host's answers are cached, per (mesh, machine), for :data:`CARD_TTL`.
    """

    def __init__(self, mesh_manager) -> None:
        self._mm = mesh_manager
        self._cache: Dict[Tuple[str, str], Tuple[float, dict]] = {}
        self._inflight: Dict[Tuple[str, str], asyncio.Future] = {}

    async def _cards(self, mesh: str, machine: str) -> dict:
        key = (mesh, machine)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < CARD_TTL:
            return hit[1]
        running = self._inflight.get(key)
        if running is not None:
            return await asyncio.shield(running)
        fut = asyncio.ensure_future(self._fetch(mesh, machine))
        self._inflight[key] = fut
        try:
            result = await fut
        finally:
            self._inflight.pop(key, None)
        self._cache[key] = (time.monotonic(), result)
        return result

    async def _fetch(self, mesh: str, machine: str) -> dict:
        try:
            payload = await asyncio.wait_for(
                self._mm.shadow_cards(mesh, machine), CARD_TIMEOUT
            )
        except asyncio.TimeoutError:
            return {"error": f"daemon {machine!r} did not answer in {CARD_TIMEOUT:g}s"}
        except (MeshError, PeerUnreachable) as exc:
            return {"error": str(exc) or type(exc).__name__}
        cards = {}
        for row in payload.get("cards") or []:
            if isinstance(row, dict) and isinstance(row.get("session"), str):
                cards[row["session"]] = row
        return {"cards": cards}

    async def list(self) -> List[dict]:
        targets = self._mm.shadow_targets()
        wanted = sorted({
            (m["mesh"], t["machine"])
            for t in targets for m in t["meshes"] if m["linked"]
        })
        answers = dict(zip(
            wanted,
            await asyncio.gather(*(self._cards(mesh, host) for mesh, host in wanted)),
        ))
        rows = []
        for t in targets:
            card_row = None
            errors = []
            for m in t["meshes"]:
                if not m["linked"]:
                    errors.append(f"no link to {t['machine']!r} in mesh {m['mesh']!r}")
                    continue
                got = answers.get((m["mesh"], t["machine"])) or {}
                if got.get("error"):
                    errors.append(got["error"])
                    continue
                found = (got.get("cards") or {}).get(t["session"])
                if found is not None and card_row is None:
                    card_row = found
            rows.append({
                "machine": t["machine"],
                "session": t["session"],
                "meshes": [
                    {k: m[k] for k in ("mesh", "handle", "role", "roles", "linked")}
                    for m in t["meshes"]
                ],
                "card": _sanitize_card(card_row),
                "error": None if card_row is not None else (
                    "; ".join(dict.fromkeys(errors))
                    or "the host did not list this session"
                ),
            })
        return rows


def _sanitize_card(row: Optional[dict]) -> Optional[dict]:
    """A host's card, cut to the fields this daemon shows.

    The host already sends only these; the cut is repeated here so a peer
    running other code cannot put anything else on this daemon's page.
    """
    if not isinstance(row, dict):
        return None
    out: dict = {}
    for key in ("session", "handle", "role", "harness", "status", "note"):
        value = row.get(key)
        if isinstance(value, str):
            out[key] = value
    roles = row.get("roles")
    if isinstance(roles, list):
        out["roles"] = [r for r in roles if isinstance(r, str)]
    out["exited"] = bool(row.get("exited"))
    brief = row.get("briefing")
    if isinstance(brief, dict):
        kept = {k: brief[k] for k in BRIEFING_FIELDS if isinstance(brief.get(k), str)}
        out["briefing"] = kept or None
    flow = row.get("cflow")
    if isinstance(flow, dict):
        kept = {
            k: flow[k] for k in CFLOW_FIELDS
            if isinstance(flow.get(k), (str, int)) and not isinstance(flow.get(k), bool)
        }
        out["cflow"] = kept or None
    return out


class ShadowStream:
    """A host's ``/peer/shadow/stream`` answer, read off a live bridge."""

    def __init__(self, bridge) -> None:
        self._bridge = bridge
        self._resp = ResponseStream()
        self._frames = FrameReader()
        self._pending: List[Tuple[int, bytes]] = []

    @property
    def overflowed(self) -> bool:
        return bool(getattr(self._bridge, "overflowed", False))

    async def open(self) -> "ShadowStream":
        """Read up to the response head; raise the host's refusal if it is one."""
        body = b""
        while not self._resp.head_done:
            chunk = await self._bridge.read()
            if chunk is None:
                raise PeerUnreachable("the host closed the shadow stream before answering")
            body = self._resp.feed(chunk)
        if self._resp.status >= 400:
            rest = bytearray(body)
            while len(rest) < 64 * 1024:
                chunk = await self._bridge.read()
                if chunk is None:
                    break
                rest += self._resp.feed(chunk)
                if self._resp.finished:
                    break
            try:
                detail = json.loads(bytes(rest).decode("utf-8")).get("error")
            except (ValueError, UnicodeDecodeError, AttributeError):
                detail = None
            raise MeshError(detail or f"HTTP {self._resp.status}")
        if body:
            self._pending.extend(self._frames.feed(body))
        return self

    async def frames(self) -> AsyncIterator[Tuple[int, bytes]]:
        while True:
            while self._pending:
                yield self._pending.pop(0)
            if self._resp.finished:
                return
            chunk = await self._bridge.read()
            if chunk is None:
                return
            self._pending.extend(self._frames.feed(self._resp.feed(chunk)))

    async def close(self) -> None:
        await self._bridge.close()


#: Which control frames of the terminal protocol reach a shadow viewer.
#: Everything else a host might send is dropped here, not in the page.
VIEWER_FRAMES = frozenset(
    ("init", "state", "exit", "resize", "buffer", "mouse", "shutdown")
)


async def bridge_to_viewer(ws, mesh_manager, machine: str, session: str) -> None:
    """Feed ``machine``'s ``session`` to browser socket ``ws``, output only.

    Nothing the browser sends is forwarded -- the stream to the host was
    opened with a fixed request and has no way back -- and it is read only
    so the socket's close is noticed.
    """
    try:
        bridge = await mesh_manager.shadow_stream(machine, session)
    except (MeshError, PeerUnreachable) as exc:
        await ws.send_str(json.dumps({"type": "shadow_error", "error": str(exc)}))
        return
    stream = ShadowStream(bridge)
    try:
        await stream.open()
    except (MeshError, PeerUnreachable, PeerHttpError) as exc:
        await stream.close()
        await ws.send_str(json.dumps({"type": "shadow_error", "error": str(exc)}))
        return

    async def pump() -> None:
        try:
            async for kind, payload in stream.frames():
                if kind == FRAME_BYTES:
                    await ws.send_bytes(payload)
                elif kind == FRAME_TEXT:
                    try:
                        frame = json.loads(payload.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(frame, dict) and frame.get("type") in VIEWER_FRAMES:
                        await ws.send_str(json.dumps(frame))
            await ws.send_str(json.dumps({
                "type": "shadow_ended", "overflowed": stream.overflowed,
            }))
        except (ConnectionError, PeerHttpError) as exc:
            log.debug("shadow stream %s/%s ended: %s", machine, session, exc)
        finally:
            await ws.close()

    task = asyncio.ensure_future(pump())
    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
            # Dropped on purpose: a shadow takes no input over its terminal.
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await stream.close()
