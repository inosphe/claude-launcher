"""Outbound relay uplink: expose this daemon through a psmux-relay backend.

The daemon dials the relay over a single outbound WebSocket (so it works from
machines that can't accept inbound connections — company PCs behind NAT), sends
REGISTER(name, token), and thereafter services relay-opened streams by piping
each to the daemon's own loopback HTTP port. A browser that logs into the relay
and opens ``/t/<name>/`` reaches the full daemon web UI — the daemon's own
Bearer/cookie auth still applies, so the relay login is a second, outer gate.

Reconnection mirrors the psmux agent's discipline (protocol spec §6):
keepalive PING, a receive watchdog, and exponential backoff with jitter. The
uplink is a pure add-on path — if the relay is down the local daemon is
unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import aiohttp

from . import relay_wire as w
from . import sendq

log = logging.getLogger("claunch.daemon.relay")

#: Keepalive cadence, which is also how often the relay round-trip is
#: sampled for the web UI's latency badge (claunch-8ufey). A PING is 33 bytes;
#: every 5s keeps the number shown no older than one beat, and a relay that
#: slows down shows up within seconds rather than after a 20s gap.
PING_INTERVAL = 5.0
#: Unanswered PINGs remembered at once. A PONG older than this many beats is
#: not worth a number: the one after it already said more.
_PINGS_KEPT = 8
RECV_WATCHDOG = 60.0
CONNECT_TIMEOUT = 10.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 30.0
_STABLE_AFTER = 30.0  # a connection alive this long resets backoff


class PeerError(Exception):
    """Raised when a peer-bridged request cannot be made or fails."""


class _Stream:
    """One relay stream bound to a loopback TCP connection to the daemon."""

    def __init__(self, sid: int, writer: asyncio.StreamWriter) -> None:
        self.sid = sid
        self.writer = writer


#: How many response bytes a live bridge (:meth:`RelayUplink.peer_open`) may
#: hold that its reader has not taken yet. Past it the bridge is ended rather
#: than grown: the reader is a viewer that stopped keeping up, and the shadow
#: terminal it feeds repaints from scratch on its next stream anyway.
LIVE_BUFFER_MAX = 4 * 1024 * 1024


class _PeerStream:
    """An outbound bridged stream (this daemon → relay → peer backend).

    Collected whole by default (:meth:`RelayUplink.peer_http`, a response read
    to EOF). ``live`` hands each chunk to a queue instead, for a response that
    does not end while someone reads it (:meth:`RelayUplink.peer_open`).
    """

    def __init__(self, sid: int, *, live: bool = False) -> None:
        self.sid = sid
        self.chunks: list = []
        self.done = asyncio.Event()  # set on EOF/CLOSE (response complete)
        self.queue: Optional[asyncio.Queue] = asyncio.Queue() if live else None
        self.queued = 0  # live: bytes in the queue not yet read
        self.overflowed = False

    def feed(self, data: bytes) -> None:
        if self.queue is None:
            self.chunks.append(data)
            return
        if self.done.is_set():
            return
        self.queued += len(data)
        if self.queued > LIVE_BUFFER_MAX:
            self.overflowed = True
            self.finish()
            return
        self.queue.put_nowait(data)

    def finish(self) -> None:
        if self.done.is_set():
            return
        self.done.set()
        if self.queue is not None:
            self.queue.put_nowait(None)


class PeerBridge:
    """A live bridge to a peer backend: read the response as it arrives.

    Returned by :meth:`RelayUplink.peer_open`. :meth:`read` answers the next
    chunk of raw response bytes, or ``None`` once the peer ended the stream
    (or the reader fell :data:`LIVE_BUFFER_MAX` behind, or the uplink died).
    :meth:`close` ends it from this side; call it whichever way the reading
    stopped.
    """

    def __init__(self, uplink: "RelayUplink", ps: _PeerStream) -> None:
        self._uplink = uplink
        self._ps = ps
        self._closed = False

    @property
    def overflowed(self) -> bool:
        return self._ps.overflowed

    async def read(self) -> Optional[bytes]:
        if self._ps.queue is None:
            return None
        chunk = await self._ps.queue.get()
        if chunk is not None:
            self._ps.queued -= len(chunk)
        return chunk

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ps.finish()
        await self._uplink._end_peer_stream(self._ps.sid)


class RelayUplink:
    """Manages the lifetime of the uplink: (re)connect loop + stream plumbing."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        name: str,
        local_host: str,
        local_port: int,
        verify_tls: bool = True,
        id: str = "",
    ) -> None:
        self.url = url
        self.token = token
        self.name = name
        self.local_host = local_host
        self.local_port = local_port
        self.verify_tls = verify_tls
        #: Local handle for this uplink among the daemon's others (config
        #: ``id``). Distinct from ``name``, which is what the relay directory
        #: calls this backend and may repeat across relays.
        self.id = id or name or url

        self._room = secrets.token_bytes(w.ROOM_ID_LEN)
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._streams: Dict[int, _Stream] = {}
        # The sender (see _raw_send): frames waiting to go out, and the one
        # task that writes them.
        self._urgent: Deque[Tuple[bytes, asyncio.Future]] = deque()
        self._bulk: Dict[int, Deque[Tuple[bytes, asyncio.Future]]] = {}
        self._turns: Deque[int] = deque()  # sids with data queued, in turn
        self._wake: Optional[asyncio.Event] = None
        self._sender: Optional[asyncio.Task] = None
        self._sender_ws = None
        self._last_recv = 0.0
        self._stop = asyncio.Event()
        #: True while registered with the relay (surfaced as relay status in
        #: the API/CLI/web so users always see whether the mesh can span
        #: machines right now).
        self.connected = False
        #: True when the relay advertised CAP_PEERING in REGISTER_OK — checked
        #: before every PEER_OPEN (an old relay drops unknown types silently).
        self.peering = False
        #: True when the relay also answers PEER_LIST (CAP_PEER_LIST).
        self.listing = False
        self._peer_streams: Dict[int, _PeerStream] = {}
        self._peer_waiters: Dict[int, asyncio.Future] = {}
        self._next_req = 1
        #: token -> monotonic send time of each PING still waiting on a PONG.
        self._pings: Dict[int, float] = {}
        #: Last measured round trip to the relay, in ms, and when it landed.
        self.rtt_ms: Optional[float] = None
        self._rtt_at = 0.0

    async def run(self) -> None:
        """Reconnect loop. Runs until :meth:`stop` is called."""
        backoff = BACKOFF_BASE
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure → reconnect
                log.info("relay uplink disconnected: %s", exc)
            if self._stop.is_set():
                break
            # Reset backoff if the last connection held long enough.
            if time.monotonic() - started >= _STABLE_AFTER:
                backoff = BACKOFF_BASE
            delay = min(backoff, BACKOFF_MAX)
            delay *= 1.0 + (secrets.randbelow(500) - 250) / 1000.0  # ±25% jitter
            log.info("reconnecting to relay in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, delay))
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, BACKOFF_MAX)

    def stop(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------------- #
    async def _session(self) -> None:
        """One connect → register → serve cycle. Returns on disconnect."""
        # aiohttp ignores this for ws:// (non-TLS); False disables verification
        # for wss:// against a self-signed relay (README's cert-less model).
        ssl = True if self.verify_tls else False
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                self.url, ssl=ssl, max_msg_size=0, autoping=False, heartbeat=None
            ) as ws:
                self._ws = ws
                self._streams = {}
                # A number from the previous connection describes a path that
                # no longer exists; the badge reads "measuring" until the
                # first PONG of this one.
                self._pings = {}
                self.rtt_ms = None
                self._rtt_at = 0.0
                self._last_recv = time.monotonic()
                await self._raw_send(w.register(self._room, self.name, self.token))
                if not await self._await_register_ok(ws):
                    log.warning("relay rejected REGISTER (bad token?) — will retry")
                    return
                log.info("registered with relay as %r at %s", self.name, self.url)
                self.connected = True

                ping = asyncio.ensure_future(self._keepalive())
                watchdog = asyncio.ensure_future(self._watchdog())
                try:
                    await self._recv_loop(ws)
                finally:
                    self.connected = False
                    self.peering = False
                    self.listing = False
                    ping.cancel()
                    watchdog.cancel()
                    await self._close_all_streams()
                    self._fail_peer_state()
                    self._ws = None
                    self._stop_sender()

    async def _await_register_ok(self, ws) -> bool:
        decoder = w.FrameDecoder()
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        if msg.type != aiohttp.WSMsgType.BINARY:
            return False
        for _room, payload in decoder.feed(msg.data):
            decoded = w.decode_payload(payload)
            if decoded and decoded.kind == w.REGISTER_OK:
                self._last_recv = time.monotonic()
                self.peering = bool(decoded.caps & w.CAP_PEERING)
                self.listing = bool(decoded.caps & w.CAP_PEER_LIST)
                # Any frames after REGISTER_OK in the same message are handled
                # by the recv loop; stash the decoder so we don't lose them.
                self._decoder = decoder
                return True
        self._decoder = decoder
        return False

    async def _recv_loop(self, ws) -> None:
        decoder = getattr(self, "_decoder", None) or w.FrameDecoder()
        self._decoder = decoder
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                self._last_recv = time.monotonic()
                for _room, payload in decoder.feed(msg.data):
                    await self._handle(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                              aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _handle(self, payload: bytes) -> None:
        m = w.decode_payload(payload)
        if m is None:
            return
        if m.kind in (w.PEER_OPEN_OK, w.PEER_OPEN_ERR, w.PEER_LIST_OK):
            waiter = self._peer_waiters.pop(m.req, None)
            if waiter is not None and not waiter.done():
                if m.kind == w.PEER_OPEN_OK:
                    waiter.set_result(m.sid)
                elif m.kind == w.PEER_LIST_OK:
                    waiter.set_result(list(m.names))
                else:
                    waiter.set_exception(PeerError(_peer_err_text(m.code)))
            return
        # A sid we initiated (peer bridge) takes precedence: those streams
        # collect response bytes instead of piping to the loopback daemon.
        peer = self._peer_streams.get(m.sid) if m.sid else None
        if peer is not None and m.kind in (w.STREAM_DATA, w.STREAM_EOF, w.STREAM_CLOSE):
            if m.kind == w.STREAM_DATA:
                peer.feed(m.data)
            else:
                peer.finish()
            return
        if m.kind == w.STREAM_OPEN:
            await self._open_stream(m.sid)
        elif m.kind == w.STREAM_DATA:
            st = self._streams.get(m.sid)
            if st is not None:
                st.writer.write(m.data)
                try:
                    await st.writer.drain()
                except (ConnectionError, OSError):
                    await self._drop_stream(m.sid, notify=True)
        elif m.kind == w.STREAM_EOF:
            # Relay signals "browser finished sending the request" — but we must
            # NOT half-close (write_eof) the loopback socket. The daemon's HTTP
            # server already knows the request is complete from framing
            # (end-of-headers / Content-Length); a TCP FIN here instead reads as
            # a client disconnect and aiohttp abandons the response, so the relay
            # proxies an empty reply → 502. Teardown happens on STREAM_CLOSE.
            pass
        elif m.kind == w.STREAM_CLOSE:
            await self._drop_stream(m.sid, notify=False)
        elif m.kind == w.PING:
            await self._raw_send(w.pong(self._room, m.token))
        elif m.kind == w.PONG:
            self._pong(m.token)

    def _pong(self, token: int) -> None:
        """Turn the relay's echo of one of our PINGs into a round trip.

        Only a token this uplink sent counts: the relay echoes the token
        verbatim, so an unknown one measures nothing. PINGs sent before the
        answered one are dropped with it -- their PONGs, if they ever come,
        would describe a moment the newer sample already covers.
        """
        sent = self._pings.pop(token, None)
        if sent is None:
            return
        now = time.monotonic()
        self._pings = {t: at for t, at in self._pings.items() if at > sent}
        self.rtt_ms = (now - sent) * 1000.0
        self._rtt_at = now

    def latency(self) -> dict:
        """The relay round trip as the status API reports it.

        ``rtt_ms`` is the last completed sample and ``rtt_age`` how many
        seconds ago it landed. ``pending_ms`` is the age of the oldest PING
        still unanswered, when there is one older than the last round trip:
        a relay that has gone slow answers late, and until it does the last
        completed sample still looks healthy -- this is the number that does
        not.
        """
        now = time.monotonic()
        pending = None
        if self._pings:
            waited = (now - min(self._pings.values())) * 1000.0
            if self.rtt_ms is None or waited > self.rtt_ms:
                pending = round(waited, 1)
        return {
            "rtt_ms": None if self.rtt_ms is None else round(self.rtt_ms, 1),
            "rtt_age": None if self.rtt_ms is None else round(now - self._rtt_at, 1),
            "pending_ms": pending,
        }

    async def _open_stream(self, sid: int) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.local_host, self.local_port),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("stream %d: local connect failed: %s", sid, exc)
            await self._raw_send(w.stream_close(self._room, sid))
            return
        self._streams[sid] = _Stream(sid, writer)
        asyncio.ensure_future(self._pump_local(sid, reader))

    async def _pump_local(self, sid: int, reader: asyncio.StreamReader) -> None:
        """Local daemon → relay direction for one stream."""
        try:
            while True:
                chunk = await reader.read(32 * 1024)
                if not chunk:
                    await self._raw_send(w.stream_eof(self._room, sid))
                    break
                for frame in w.iter_stream_data(self._room, sid, chunk):
                    await self._raw_send(frame)
        except (ConnectionError, OSError):
            pass
        finally:
            # Local side ended; tell the relay and forget the stream. We keep the
            # writer alive until an explicit close to allow the response tail.
            if sid in self._streams:
                await self._raw_send(w.stream_close(self._room, sid))
                await self._drop_stream(sid, notify=False)

    async def _drop_stream(self, sid: int, *, notify: bool) -> None:
        st = self._streams.pop(sid, None)
        if st is None:
            return
        if notify:
            await self._raw_send(w.stream_close(self._room, sid))
        try:
            st.writer.close()
        except (OSError, RuntimeError):
            pass

    async def _close_all_streams(self) -> None:
        for sid in list(self._streams):
            await self._drop_stream(sid, notify=False)

    # --------------------------------------------------------------------- #
    # peer bridges (this daemon → relay → another backend)
    # --------------------------------------------------------------------- #
    async def peer_http(self, peer: str, request: bytes, *, timeout: float = 30.0) -> bytes:
        """One raw HTTP/1.1 request over a relay bridge to backend ``peer``.

        The request must be self-delimiting (``Connection: close`` +
        ``Content-Length``), mirroring the relay ingress convention of
        1 request = 1 stream. Returns the raw response bytes (head + body).
        Raises :class:`PeerError` when the relay is down or too old
        (no CAP_PEERING), the peer is unknown/unreachable, or the bridge
        dies before the response completes.
        """
        sid = await self._peer_handshake(peer)
        ps = _PeerStream(sid)
        self._peer_streams[sid] = ps
        try:
            for frame in w.iter_stream_data(self._room, sid, request):
                await self._raw_send(frame)
            await self._raw_send(w.stream_eof(self._room, sid))
            try:
                await asyncio.wait_for(ps.done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise PeerError(f"peer {peer!r} response timed out") from None
            resp = b"".join(ps.chunks)
            if not resp:
                raise PeerError(f"peer {peer!r} closed the bridge without a response")
            return resp
        finally:
            await self._end_peer_stream(sid)

    async def peer_open(self, peer: str, request: bytes) -> PeerBridge:
        """Open a bridge to backend ``peer`` whose response is read live.

        The same PEER_OPEN and request as :meth:`peer_http`, but nothing
        waits for the response to end: the returned :class:`PeerBridge`
        hands out its bytes as they arrive, for as long as the peer keeps
        the stream open and the caller keeps reading. Raises
        :class:`PeerError` for the same reasons ``peer_http`` does, all of
        them before any response byte.
        """
        sid = await self._peer_handshake(peer)
        ps = _PeerStream(sid, live=True)
        self._peer_streams[sid] = ps
        try:
            for frame in w.iter_stream_data(self._room, sid, request):
                await self._raw_send(frame)
        except BaseException:
            await self._end_peer_stream(sid)
            raise
        return PeerBridge(self, ps)

    async def _peer_handshake(self, peer: str) -> int:
        """PEER_OPEN to ``peer``; the stream id the relay granted."""
        if self._ws is None or not self.connected:
            raise PeerError("relay uplink is not connected")
        if not self.peering:
            raise PeerError(
                "relay does not allow backend peering "
                "(enable allow_backend_peering in relay.toml, or upgrade the relay)"
            )
        req_id = self._next_req
        self._next_req = ((self._next_req + 1) & 0xFFFFFFFF) or 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._peer_waiters[req_id] = fut
        await self._raw_send(w.peer_open(self._room, req_id, peer))
        try:
            return await asyncio.wait_for(fut, timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self._peer_waiters.pop(req_id, None)
            raise PeerError(f"PEER_OPEN to {peer!r} timed out") from None

    async def _end_peer_stream(self, sid: int) -> None:
        """Forget an outbound bridge and tell the relay it is closed."""
        self._peer_streams.pop(sid, None)
        try:
            await self._raw_send(w.stream_close(self._room, sid))
        except Exception:  # noqa: BLE001 — the uplink is going; nothing to tell
            log.debug("stream %d: close not sent", sid, exc_info=True)

    async def peer_list(self, *, timeout: float = 10.0) -> list:
        """Names of the other backends registered on this relay.

        Raises :class:`PeerError` when the uplink is down or the relay is
        too old to answer (no CAP_PEER_LIST).
        """
        if self._ws is None or not self.connected:
            raise PeerError("relay uplink is not connected")
        if not self.listing:
            raise PeerError(
                "relay does not support peer listing — upgrade the relay "
                "(and enable allow_backend_peering)"
            )
        req_id = self._next_req
        self._next_req = ((self._next_req + 1) & 0xFFFFFFFF) or 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._peer_waiters[req_id] = fut
        await self._raw_send(w.peer_list(self._room, req_id))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._peer_waiters.pop(req_id, None)
            raise PeerError("PEER_LIST timed out") from None

    def _fail_peer_state(self) -> None:
        """Uplink died: fail pending PEER_OPENs, complete in-flight bridges."""
        for fut in self._peer_waiters.values():
            if not fut.done():
                fut.set_exception(PeerError("relay uplink disconnected"))
        self._peer_waiters.clear()
        for ps in self._peer_streams.values():
            ps.finish()
        self._peer_streams.clear()

    async def _keepalive(self) -> None:
        # The first PING goes out at once, so the latency badge has a number
        # right after registration instead of one beat later.
        while True:
            await self._raw_send(w.ping(self._room, self._next_ping_token()))
            await asyncio.sleep(PING_INTERVAL)

    def _next_ping_token(self) -> int:
        """Pick the next PING token and remember when it was sent."""
        now = time.monotonic()
        token = int(now * 1000) & 0xFFFFFFFFFFFFFFFF
        while token in self._pings:
            token = (token + 1) & 0xFFFFFFFFFFFFFFFF
        if len(self._pings) >= _PINGS_KEPT:
            self._pings.pop(min(self._pings, key=self._pings.get))
        self._pings[token] = now
        return token

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(RECV_WATCHDOG / 3)
            if time.monotonic() - self._last_recv > RECV_WATCHDOG:
                log.info("relay uplink watchdog: no frames for %.0fs — forcing reconnect",
                         RECV_WATCHDOG)
                ws = self._ws
                if ws is not None:
                    await ws.close()
                return

    # --------------------------------------------------------------------- #
    # the sender
    # --------------------------------------------------------------------- #
    # Every stream the relay opens -- each browser request, the page's
    # control socket, a peer bridge -- and the keepalive share this one
    # connection. Written in arrival order, a PING or the control socket's
    # next frame queued behind a 1.3MB read waited for all of it to cross the
    # link (claunch-iss86: the round trip to the relay measured 80ms at its
    # floor and 100-408ms at other times, cause not separated). So frames
    # are sorted as they come in:
    #
    # - anything that is not stream data (PING/PONG, OPEN/EOF/CLOSE, peer
    #   requests) goes first, in arrival order;
    # - stream data goes one frame per stream in turn, so a large response
    #   takes its share of the link, not all of it;
    # - and stream data waits while the socket already holds more than
    #   sendq.LOW_WATER, since a frame handed over behind a full buffer would
    #   lose its place anyway.
    #
    # EOF and CLOSE of a stream that still has data queued go behind that
    # data, not ahead of it: ahead, the relay would end the response before
    # its tail arrived.

    async def _raw_send(self, frame: bytes) -> None:
        """Queue ``frame`` and return once it is written (or dropped with the
        connection). Callers keep waiting on their own frames, which is the
        backpressure between a local stream and a slow link."""
        ws = self._ws
        if ws is None:
            return
        if self._sender is None or self._sender.done() or self._sender_ws is not ws:
            self._start_sender(ws)
        done = asyncio.get_running_loop().create_future()
        kind, sid = w.kind_and_sid(frame)
        if sid is not None and (kind == w.STREAM_DATA or sid in self._bulk):
            lane = self._bulk.get(sid)
            if lane is None:
                lane = self._bulk[sid] = deque()
                self._turns.append(sid)
            lane.append((frame, done))
        else:
            self._urgent.append((frame, done))
        self._wake.set()
        try:
            await done
        except (ConnectionError, OSError, RuntimeError, aiohttp.ClientError):
            pass

    def _start_sender(self, ws) -> None:
        old = self._sender
        if old is not None and not old.done():
            old.cancel()
        self._fail_queued(ConnectionResetError("relay connection replaced"))
        self._wake = asyncio.Event()
        self._sender_ws = ws
        self._sender = asyncio.ensure_future(self._send_loop(ws))

    def _next_frame(self) -> Optional[Tuple[bytes, asyncio.Future, bool]]:
        if self._urgent:
            frame, done = self._urgent.popleft()
            return frame, done, False
        while self._turns:
            sid = self._turns.popleft()
            lane = self._bulk.get(sid)
            if not lane:
                self._bulk.pop(sid, None)
                continue
            frame, done = lane.popleft()
            if lane:
                self._turns.append(sid)
            else:
                del self._bulk[sid]
            return frame, done, True
        return None

    async def _send_loop(self, ws) -> None:
        wake = self._wake
        while True:
            if not self._urgent and self._turns:
                # Stream data is next: hold it while the socket is full, but
                # let a control frame that arrives meanwhile through first.
                if not await sendq.below_low_water(ws, lambda: bool(self._urgent)):
                    continue
            item = self._next_frame()
            if item is None:
                wake.clear()
                await wake.wait()
                continue
            frame, done, _data = item
            try:
                await ws.send_bytes(frame)
            except asyncio.CancelledError:
                # Whoever waits on it hears a dropped connection, the thing
                # _raw_send already absorbs -- not a cancellation of its own.
                if not done.done():
                    done.set_exception(ConnectionResetError("relay sender stopped"))
                raise
            except Exception as exc:  # noqa: BLE001 -- told to whoever waits
                if not done.done():
                    done.set_exception(exc)
            else:
                if not done.done():
                    done.set_result(None)

    def _fail_queued(self, exc: BaseException) -> None:
        """Tell everyone waiting on a queued frame that it will not go out."""
        pending = list(self._urgent)
        for lane in self._bulk.values():
            pending.extend(lane)
        self._urgent.clear()
        self._bulk.clear()
        self._turns.clear()
        for _frame, done in pending:
            if not done.done():
                done.set_exception(exc)

    def _stop_sender(self) -> None:
        sender, self._sender, self._sender_ws = self._sender, None, None
        if sender is not None and not sender.done():
            sender.cancel()
        self._fail_queued(ConnectionResetError("relay uplink disconnected"))


def _latency_of(up) -> dict:
    """An uplink's relay round trip, or the empty reading of one.

    Every row carries the three keys so a reader never has to ask whether
    they are there; a disconnected uplink reports no sample.
    """
    read = getattr(up, "latency", None)
    if not up.connected or read is None:
        return {"rtt_ms": None, "rtt_age": None, "pending_ms": None}
    return read()


def unconfigured_state() -> dict:
    """The relay status of a daemon with no uplink at all.

    Lives beside :meth:`RelayPool.state` so the two shapes cannot drift: a
    reader must be able to take the same keys whether or not a relay is
    configured.
    """
    return {
        "configured": False,
        "connected": False,
        "name": None,
        "url": None,
        "count": 0,
        "connected_count": 0,
        "relays": [],
    }


class RelayPool:
    """Every relay uplink this daemon holds open at once.

    One daemon may register with several relays — a work relay and a home
    relay, say — so that a mesh member on either side can reach it without the
    operator choosing between them. The pool owns the uplinks, runs them
    concurrently, and presents the same surface a single uplink did
    (``connected``, ``name``, ``peer_http``, ``peer_list``) so the federation
    wiring does not have to know how many there are.

    Peer addressing stays the relay's: a backend is addressed by name, and the
    pool works out WHICH relay currently carries that name. It learns the
    mapping from PEER_LIST and caches it, falling back to trying each
    peering-capable uplink in turn when no relay can list.
    """

    def __init__(self, uplinks) -> None:
        self.uplinks = list(uplinks)
        #: peer name -> the uplink that last reached it. Invalidated on
        #: failure, so a backend that moves relays is re-found rather than
        #: retried forever against the relay it left.
        self._routes: Dict[str, RelayUplink] = {}

    # --------------------------------------------------------------------- #
    @property
    def name(self) -> str:
        """The backend name this daemon answers to.

        The mesh has one machine identity, so the pool reports the first
        uplink's name. Configuring different names per relay is allowed by the
        schema but the mesh only ever uses this one.
        """
        return self.uplinks[0].name if self.uplinks else ""

    @property
    def url(self) -> str:
        return self.uplinks[0].url if self.uplinks else ""

    @property
    def connected(self) -> bool:
        """True while AT LEAST ONE relay is registered.

        This is what the mesh asks before it treats remote members as
        reachable, and one live relay is enough for that to be true.
        """
        return any(up.connected for up in self.uplinks)

    def state(self) -> dict:
        """Status for the API/CLI/web: the aggregate plus a row per relay.

        The top-level ``configured``/``connected``/``name``/``url`` keys keep
        the shape a single uplink reported, so readers that predate multiple
        relays keep working; ``relays`` and ``connected_count`` are what a
        reader shows when there is more than one.
        """
        rows = [
            {
                "id": up.id,
                "name": up.name,
                "url": up.url,
                "connected": up.connected,
                "peering": up.peering,
                "listing": up.listing,
                **_latency_of(up),
            }
            for up in self.uplinks
        ]
        return {
            "configured": bool(self.uplinks),
            "connected": self.connected,
            "name": self.name or None,
            "url": self.url or None,
            "count": len(rows),
            "connected_count": sum(1 for r in rows if r["connected"]),
            "relays": rows,
        }

    async def run(self) -> None:
        """Run every uplink's reconnect loop until :meth:`stop`."""
        if not self.uplinks:
            return
        await asyncio.gather(*(up.run() for up in self.uplinks))

    def stop(self) -> None:
        for up in self.uplinks:
            up.stop()

    # --------------------------------------------------------------------- #
    def _live(self, attr: str) -> list:
        return [up for up in self.uplinks if up.connected and getattr(up, attr)]

    async def peer_list(self, *, timeout: float = 10.0) -> list:
        """Union of the backends registered on any of this daemon's relays.

        A name reachable through two relays appears once. Relays that fail to
        answer do not sink the call — their error is only reported when NONE
        of them answered, because a partial list is still the truth about the
        relays that are up.
        """
        lister = self._live("listing")
        if not lister:
            raise PeerError(self._why_no_peering("listing"))
        results = await asyncio.gather(
            *(up.peer_list(timeout=timeout) for up in lister),
            return_exceptions=True,
        )
        names: list = []
        errors: list = []
        answered = False
        for up, res in zip(lister, results):
            if isinstance(res, BaseException):
                errors.append(f"{up.id}: {res}")
                continue
            answered = True
            for peer in res:
                # Remember where each name lives so peer_http can go straight
                # there instead of probing every relay.
                self._routes.setdefault(peer, up)
                if peer not in names:
                    names.append(peer)
        if not answered:
            raise PeerError("; ".join(errors) or "no relay answered PEER_LIST")
        return names

    async def peer_http(self, peer: str, request: bytes, *,
                        timeout: float = 30.0) -> bytes:
        """One bridged HTTP request to backend ``peer`` over whichever relay
        currently carries it.

        The cached route is tried first. When it fails — or when there is no
        route yet and no relay can list — every peering-capable uplink is
        tried in turn, and the collected reasons are raised together so the
        operator sees why each relay refused rather than only the last one.
        """
        candidates = self._live("peering")
        if not candidates:
            raise PeerError(self._why_no_peering("peering"))
        order = self._order_for(peer, candidates)
        if len(order) > 1:
            # No route yet: ask the relays who they carry, then retry ordering.
            try:
                await self.peer_list()
            except PeerError:
                pass
            order = self._order_for(peer, candidates)
        errors = []
        for up in order:
            try:
                resp = await up.peer_http(peer, request, timeout=timeout)
            except PeerError as exc:
                errors.append(f"{up.id}: {exc}")
                if self._routes.get(peer) is up:
                    del self._routes[peer]
                continue
            self._routes[peer] = up
            return resp
        raise PeerError(
            f"peer {peer!r} unreachable on any relay -- " + "; ".join(errors)
        )

    async def peer_open(self, peer: str, request: bytes) -> PeerBridge:
        """A live bridge (:meth:`RelayUplink.peer_open`) over whichever relay
        carries ``peer``. Only the opening is retried across relays: once
        bytes flow, the bridge is bound to the relay it opened on."""
        candidates = self._live("peering")
        if not candidates:
            raise PeerError(self._why_no_peering("peering"))
        order = self._order_for(peer, candidates)
        if len(order) > 1:
            try:
                await self.peer_list()
            except PeerError:
                pass
            order = self._order_for(peer, candidates)
        errors = []
        for up in order:
            try:
                bridge = await up.peer_open(peer, request)
            except PeerError as exc:
                errors.append(f"{up.id}: {exc}")
                if self._routes.get(peer) is up:
                    del self._routes[peer]
                continue
            self._routes[peer] = up
            return bridge
        raise PeerError(
            f"peer {peer!r} unreachable on any relay -- " + "; ".join(errors)
        )

    def _order_for(self, peer: str, candidates: list) -> list:
        """Candidates with the known route for ``peer`` first."""
        known = self._routes.get(peer)
        if known in candidates:
            return [known] + [up for up in candidates if up is not known]
        return list(candidates)

    def _why_no_peering(self, attr: str) -> str:
        if not self.uplinks:
            return "no relay uplink is configured"
        if not self.connected:
            return "no relay uplink is connected"
        verb = ("support peer listing" if attr == "listing"
                else "allow backend peering")
        names = ", ".join(up.id for up in self.uplinks if up.connected)
        return f"no connected relay ({names}) can {verb}"


def _peer_err_text(code: int) -> str:
    return {
        w.PEER_ERR_UNKNOWN_BACKEND: "peer backend is not registered with the relay",
        w.PEER_ERR_DISABLED: "relay has backend peering disabled (allow_backend_peering)",
        w.PEER_ERR_UNREACHABLE: "peer backend is unreachable",
    }.get(code, f"peer open failed (code {code})")


def _env_suffix(ident: str) -> str:
    """``CLAUNCH_RELAY_TOKEN_<SUFFIX>`` form of a relay handle."""
    return "".join(c if c.isalnum() else "_" for c in ident).upper()


def _relay_env(key: str, ident: str, *, allow_bare: bool) -> Optional[str]:
    """Environment override for one relay setting.

    With several relays configured the bare ``CLAUNCH_RELAY_*`` names are
    ambiguous — one value cannot mean three different uplinks — so they are
    read only when a single relay is configured. Per-relay values are always
    read and always win: ``CLAUNCH_RELAY_TOKEN_HOME`` for the relay whose
    handle is ``home``.
    """
    scoped = os.environ.get(f"CLAUNCH_RELAY_{key}_{_env_suffix(ident)}")
    if scoped:
        return scoped
    if allow_bare:
        return os.environ.get(f"CLAUNCH_RELAY_{key}")
    return None


def config_from_env_and_dict(cfg: dict, *, local_host: str, local_port: int,
                             allow_bare_env: bool = True,
                             default_name: str = "") -> Optional[RelayUplink]:
    """Build an uplink from one ``relay`` config block, or None if not enabled.

    The environment overrides the config file: ``CLAUNCH_RELAY_TOKEN`` (a
    secret that need not live on disk), plus ``CLAUNCH_RELAY_URL`` and
    ``CLAUNCH_RELAY_NAME`` so a named daemon instance can be pointed at a
    relay per-process while sharing the config file with its siblings. Each
    also has a per-relay form (``CLAUNCH_RELAY_TOKEN_<HANDLE>``) that takes
    precedence; ``allow_bare_env`` is what :func:`pool_from_config` turns off
    when more than one relay is configured, so the bare names cannot silently
    point every uplink at the same relay.
    """
    if not isinstance(cfg, dict):
        return None
    ident = str(cfg.get("id") or "").strip()
    env = lambda key: _relay_env(key, ident, allow_bare=allow_bare_env)  # noqa: E731
    url = (env("URL") or str(cfg.get("url") or "")).strip()
    if not url:
        return None
    token = env("TOKEN") or str(cfg.get("token") or "")
    if not token:
        log.warning("relay uplink %s configured but no token (set "
                    "CLAUNCH_RELAY_TOKEN or daemon.relay.token) — uplink disabled",
                    ident or url)
        return None
    name = (env("NAME") or str(cfg.get("name") or "") or default_name).strip()
    if not name:
        import socket

        name = socket.gethostname()
    verify_tls = cfg.get("verify_tls", True)
    return RelayUplink(
        url=url,
        token=token,
        name=name,
        local_host=local_host,
        local_port=local_port,
        verify_tls=bool(verify_tls),
        id=ident,
    )


def pool_from_config(rows, *, local_host: str, local_port: int,
                     default_name: str = "") -> Optional["RelayPool"]:
    """Build a :class:`RelayPool` from a list of relay config blocks.

    ``default_name`` is the backend name for rows that name none and have no
    ``CLAUNCH_RELAY_NAME`` either — the daemon passes its instance-suffixed
    hostname so sibling instances sharing a config file do not all register
    under one directory entry.

    Returns None when no block yields a usable uplink, which is the same
    "relay not configured" answer the single-uplink path gave.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        # No relay in the config file does not mean no relay: CLAUNCH_RELAY_URL
        # and CLAUNCH_RELAY_TOKEN alone configure one, which is how a named
        # daemon instance gets its own relay identity without a file at all.
        # One empty row gives the environment something to fill.
        rows = [{}]
    allow_bare = len(rows) <= 1
    uplinks = []
    for row in rows:
        up = config_from_env_and_dict(
            row, local_host=local_host, local_port=local_port,
            allow_bare_env=allow_bare, default_name=default_name,
        )
        if up is not None:
            uplinks.append(up)
    if not uplinks:
        return None
    return RelayPool(uplinks)
