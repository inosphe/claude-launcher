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
from typing import Dict, Optional

import aiohttp

from . import relay_wire as w

log = logging.getLogger("claunch.daemon.relay")

PING_INTERVAL = 20.0
RECV_WATCHDOG = 60.0
CONNECT_TIMEOUT = 10.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 30.0
_STABLE_AFTER = 30.0  # a connection alive this long resets backoff


class _Stream:
    """One relay stream bound to a loopback TCP connection to the daemon."""

    def __init__(self, sid: int, writer: asyncio.StreamWriter) -> None:
        self.sid = sid
        self.writer = writer


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
    ) -> None:
        self.url = url
        self.token = token
        self.name = name
        self.local_host = local_host
        self.local_port = local_port
        self.verify_tls = verify_tls

        self._room = secrets.token_bytes(w.ROOM_ID_LEN)
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._streams: Dict[int, _Stream] = {}
        self._send_lock = asyncio.Lock()
        self._last_recv = 0.0
        self._stop = asyncio.Event()

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
                self._last_recv = time.monotonic()
                await self._raw_send(w.register(self._room, self.name, self.token))
                if not await self._await_register_ok(ws):
                    log.warning("relay rejected REGISTER (bad token?) — will retry")
                    return
                log.info("registered with relay as %r at %s", self.name, self.url)

                ping = asyncio.ensure_future(self._keepalive())
                watchdog = asyncio.ensure_future(self._watchdog())
                try:
                    await self._recv_loop(ws)
                finally:
                    ping.cancel()
                    watchdog.cancel()
                    await self._close_all_streams()
                    self._ws = None

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

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            token = int(time.monotonic() * 1000) & 0xFFFFFFFFFFFFFFFF
            await self._raw_send(w.ping(self._room, token))

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

    async def _raw_send(self, frame: bytes) -> None:
        ws = self._ws
        if ws is None:
            return
        async with self._send_lock:
            try:
                await ws.send_bytes(frame)
            except (ConnectionError, OSError, RuntimeError, aiohttp.ClientError):
                pass


def config_from_env_and_dict(cfg: dict, *, local_host: str, local_port: int
                             ) -> Optional[RelayUplink]:
    """Build an uplink from a ``relay`` config block, or None if not enabled.

    Secrets prefer the environment so they need not live in the config file:
    ``CLAUNCH_RELAY_TOKEN`` overrides ``token``.
    """
    if not isinstance(cfg, dict):
        return None
    url = str(cfg.get("url") or "").strip()
    if not url:
        return None
    token = os.environ.get("CLAUNCH_RELAY_TOKEN") or str(cfg.get("token") or "")
    if not token:
        log.warning("relay uplink configured but no token (set CLAUNCH_RELAY_TOKEN "
                    "or daemon.relay.token) — uplink disabled")
        return None
    name = str(cfg.get("name") or "").strip()
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
    )
