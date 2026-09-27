"""The page's control socket over a WebRTC DataChannel (claunch-mhzt4).

Through the relay a page's every read and keystroke crosses the Cloudflare
edge twice: 454-761ms per round trip measured from the user's network, where
a DataChannel straight to this host answered in 11-13ms. This module is the
daemon's end of that DataChannel. It changes nothing about what the control
socket carries -- it gives the same socket a shorter road:

- **Signalling** rides the control socket the page already holds through the
  relay, as ``p2p_offer`` / ``p2p_ice`` / ``p2p_bye`` frames (see
  :func:`api.h_control_ws`). That socket is authenticated, so the answer --
  and the DTLS fingerprint inside it -- only ever reaches a page that could
  already read everything.
- **Transport**: each authenticated DataChannel is bridged to a loopback
  ``/api/control/ws`` of this daemon. Reads, read parts and their acks,
  pongs and terminal channels are served by the exact code that serves the
  relay socket; nothing is duplicated. The loopback socket is opened with
  the daemon token, which grants what the page's cookie grants -- the auth
  middleware accepts the two interchangeably -- and nothing more.
- **Auth** of the DataChannel itself: the answer carries a single-use nonce
  and the channel's first message must be it; anything else closes the
  peer. The DTLS fingerprint pins the peer to the browser that made the
  offer; the nonce pins the channel to a signalling exchange this daemon
  answered.
- **Candidates**: aiortc's own (host, STUN srflx), plus the ports
  :mod:`.p2p_nat` predicts for this NAT, appended to the answer.

aiortc is an optional extra (``claunch[p2p]``) and is imported only when an
offer arrives; without it :func:`available` is false, the page is never told
P2P exists, and the daemon runs exactly as before.

DataChannel framing: every message is binary, one header byte then at most
:data:`FRAGMENT` bytes. Bit 0 set = the control-socket frame is binary (else
UTF-8 text); bit 1 set = more fragments follow. Reliable ordered delivery
makes reassembly a concatenation.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp

from . import p2p_nat

log = logging.getLogger("claunch.daemon.p2p")

#: Largest payload in one DataChannel message. Browsers and aiortc agree on
#: 16 KiB without negotiating anything larger.
FRAGMENT = 16 * 1024
FLAG_BINARY = 0x01
FLAG_MORE = 0x02
#: Stop reading the loopback socket while this much is queued in the
#: DataChannel; resume when it drains below LOW_WATER. The loopback socket's
#: own TCP buffer then fills and the Carrier's bulk lane waits, which is the
#: same backpressure the relay path has. Nothing is dropped.
HIGH_WATER = 1024 * 1024
LOW_WATER = 256 * 1024
#: A peer whose DataChannel has not presented its nonce by then is closed.
AUTH_TIMEOUT = 30.0
MAX_PEERS = 16
CLOSE_TIMEOUT = 5.0
#: What the bridge sends the loopback socket so its pongs say ``via: p2p``.
VIA_HEADER = "X-Claunch-Via"

_AIORTC: Optional[bool] = None


def available() -> bool:
    """Whether aiortc can be imported here (checked once)."""
    global _AIORTC
    if _AIORTC is None:
        try:
            import aiortc  # noqa: F401
            _AIORTC = True
        except Exception:  # noqa: BLE001 -- any import failure means "not here"
            _AIORTC = False
    return _AIORTC


# ------------------------------------------------------------------ framing
def encode(data) -> List[bytes]:
    """One control-socket frame as DataChannel messages."""
    if isinstance(data, str):
        body, flag = data.encode("utf-8"), 0
    else:
        body, flag = bytes(data), FLAG_BINARY
    if not body:
        return [bytes([flag])]
    out = []
    for at in range(0, len(body), FRAGMENT):
        chunk = body[at:at + FRAGMENT]
        more = FLAG_MORE if at + FRAGMENT < len(body) else 0
        out.append(bytes([flag | more]) + chunk)
    return out


class Reassembler:
    """DataChannel messages back into control-socket frames."""

    def __init__(self) -> None:
        self._parts: List[bytes] = []
        self._binary: Optional[bool] = None

    def feed(self, message) -> Optional[Tuple[bool, object]]:
        """``(is_binary, str|bytes)`` when ``message`` completes a frame."""
        if isinstance(message, str):
            raise ValueError("DataChannel frames are binary")
        if not message:
            raise ValueError("empty DataChannel message")
        head, body = message[0], bytes(message[1:])
        binary = bool(head & FLAG_BINARY)
        if self._binary is not None and binary != self._binary:
            raise ValueError("fragment kind changed mid-frame")
        self._binary = binary
        self._parts.append(body)
        if head & FLAG_MORE:
            return None
        whole = b"".join(self._parts)
        self._parts, self._binary = [], None
        return (True, whole) if binary else (False, whole.decode("utf-8"))


# ------------------------------------------------------------------ peers
class _Peer:
    def __init__(self, key: str, pc, nonce: str, created: float) -> None:
        self.key = key
        self.pc = pc
        self.nonce = nonce
        self.created = created
        self.remote_set = False
        self.pending_ice: List[Optional[dict]] = []
        self.authed = False
        self.dc = None
        self.bridge: Optional[asyncio.Task] = None
        self.closed = False


class Hub:
    """Every P2P peer of this daemon.

    ``local_port`` / ``token`` address and authenticate the loopback control
    socket each DataChannel is bridged to. ``bridge_key`` is a per-process
    secret the bridge sends in :data:`VIA_HEADER`; the control socket marks
    its pongs ``via: p2p`` only when the header carries it (see
    :func:`via_p2p`), so no other client can switch the page's tunnel badge.
    """

    def __init__(self, *, local_port: int, token: str, stun: List[str],
                 probe_stun: List[str], predict: int = 16,
                 profiler: Optional[p2p_nat.NatProfiler] = None) -> None:
        self.local_port = local_port
        self.token = token
        self.stun = [s for s in stun if isinstance(s, str)]
        self.predict = max(0, int(predict or 0))
        self.profiler = profiler or p2p_nat.NatProfiler(
            [s for s in probe_stun if isinstance(s, str)])
        self.bridge_key = secrets.token_hex(16)
        self._peers: Dict[str, _Peer] = {}
        #: Candidates that arrived before their offer was registered: the
        #: offer is answered on a task of its own, so the socket's next frame
        #: (the first trickled candidate) can be read before it runs.
        self._early: Dict[str, List[Optional[dict]]] = {}
        self._tasks: set = set()
        self._http: Optional[aiohttp.ClientSession] = None
        self._stopped = False

    # What the page is told in the control socket's init frame.
    def browser_info(self) -> dict:
        return {"stun": list(self.stun)}

    def status(self) -> dict:
        prof = self.profiler.profile
        return {
            "peers": sum(1 for p in self._peers.values() if p.authed and not p.closed),
            "pending": sum(1 for p in self._peers.values() if not p.authed and not p.closed),
            "nat": prof.as_dict() if prof else None,
        }

    def warm(self) -> None:
        """Measure the NAT now, so the first offer does not wait for it."""
        if self.predict and self.profiler.servers:
            self._spawn(self.profiler.get())

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -------------------------------------------------------------- signalling
    async def offer(self, owner: str, frame: dict,
                    send: Callable[[dict], "asyncio.Future"]) -> None:
        """Answer one ``p2p_offer``. ``owner`` names the signalling socket
        (peer keys are per socket, so two tabs cannot address each other's
        peers); ``send`` puts a frame on it."""
        pid = str(frame.get("id") or "")
        sdp = frame.get("sdp")
        if not pid or not isinstance(sdp, str) or self._stopped:
            await send({"type": "p2p_error", "id": pid, "error": "bad offer"})
            return
        if not available():
            await send({"type": "p2p_error", "id": pid, "error": "p2p unavailable"})
            return
        live = [p for p in self._peers.values() if not p.closed]
        if len(live) >= MAX_PEERS:
            await send({"type": "p2p_error", "id": pid, "error": "too many peers"})
            return
        from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                            RTCSessionDescription)

        key = f"{owner}/{pid}"
        old = self._peers.get(key)
        if old is not None:
            await self._close(old)
        pc = RTCPeerConnection(RTCConfiguration(
            iceServers=[RTCIceServer(urls=u) for u in self.stun]))
        peer = _Peer(key, pc, secrets.token_urlsafe(24), time.monotonic())
        self._peers[key] = peer
        peer.pending_ice.extend(self._early.pop(key, []))
        self._wire(peer)
        try:
            nat = self._spawn(self.profiler.get()) if self.predict else None
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            peer.remote_set = True
            for cand in peer.pending_ice:
                await self._add_ice(peer, cand)
            peer.pending_ice = []
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)  # gathers (aiortc does not trickle)
            profile = await nat if nat is not None else None
            recent = 0
            browser_ip = _first_srflx_ip(sdp)
            if browser_ip:
                recent = self.profiler.note_attempt(browser_ip)
            text = add_candidates(pc.localDescription.sdp,
                                  p2p_nat.predict(profile, self.predict, recent))
        except Exception as exc:  # noqa: BLE001 -- one offer, not the socket
            log.info("P2P offer %s failed: %s", key, exc)
            await self._close(peer)
            await send({"type": "p2p_error", "id": pid, "error": str(exc) or "offer failed"})
            return
        self._spawn(self._expire(peer))
        await send({"type": "p2p_answer", "id": pid, "sdp": text, "nonce": peer.nonce})

    async def ice(self, owner: str, frame: dict) -> None:
        """A trickled browser candidate (``candidate: null`` = no more)."""
        key = f"{owner}/{frame.get('id')}"
        cand = frame.get("candidate")
        if cand is not None and not isinstance(cand, dict):
            return
        peer = self._peers.get(key)
        if peer is None:
            if len(self._early) < 4 * MAX_PEERS:
                held = self._early.setdefault(key, [])
                if len(held) < 64:
                    held.append(cand)
            return
        if peer.closed:
            return
        if not peer.remote_set:
            peer.pending_ice.append(cand)
            return
        await self._add_ice(peer, cand)

    async def bye(self, owner: str, frame: dict) -> None:
        peer = self._peers.get(f"{owner}/{frame.get('id')}")
        if peer is not None:
            await self._close(peer)

    async def socket_gone(self, owner: str) -> None:
        """The signalling socket closed: peers still negotiating on it lose
        the only road their candidates had. Authenticated peers stay -- the
        page moves off the relay socket on purpose once the DataChannel is
        up."""
        for key in [k for k in self._early if k.startswith(owner + "/")]:
            del self._early[key]
        for peer in [p for p in self._peers.values()
                     if p.key.startswith(owner + "/") and not p.authed]:
            await self._close(peer)

    async def _add_ice(self, peer: _Peer, cand: Optional[dict]) -> None:
        from aiortc.sdp import candidate_from_sdp

        try:
            if cand is None or not cand.get("candidate"):
                await peer.pc.addIceCandidate(None)
                return
            line = str(cand["candidate"])
            if line.startswith("candidate:"):
                line = line[len("candidate:"):]
            ice = candidate_from_sdp(line)
            ice.sdpMid = cand.get("sdpMid")
            ice.sdpMLineIndex = cand.get("sdpMLineIndex")
            if ice.sdpMid is None and ice.sdpMLineIndex is None:
                ice.sdpMLineIndex = 0
            await peer.pc.addIceCandidate(ice)
        except Exception:  # noqa: BLE001 -- one bad candidate, not the peer
            log.debug("P2P %s: candidate refused", peer.key, exc_info=True)

    async def _expire(self, peer: _Peer) -> None:
        await asyncio.sleep(AUTH_TIMEOUT)
        if not peer.authed and not peer.closed:
            log.info("P2P %s: no authenticated channel in %.0fs", peer.key, AUTH_TIMEOUT)
            await self._close(peer)

    # ------------------------------------------------------------ the channel
    def _wire(self, peer: _Peer) -> None:
        pc = peer.pc

        @pc.on("connectionstatechange")
        async def _state() -> None:
            if pc.connectionState in ("failed", "closed"):
                await self._close(peer)

        @pc.on("datachannel")
        def _channel(dc) -> None:
            if peer.dc is not None:  # one channel per peer
                dc.close()
                return
            peer.dc = dc
            inbox: asyncio.Queue = asyncio.Queue()
            asm = Reassembler()

            @dc.on("message")
            def _message(message) -> None:
                try:
                    frame = asm.feed(message)
                except ValueError as exc:
                    log.info("P2P %s: bad frame (%s), closing", peer.key, exc)
                    self._spawn(self._close(peer))
                    return
                if frame is None:
                    return
                if not peer.authed:
                    is_binary, data = frame
                    if (not is_binary and isinstance(data, str) and peer.nonce
                            and hmac.compare_digest(data, peer.nonce)):
                        peer.authed = True
                        peer.nonce = ""  # single use
                        peer.bridge = self._spawn(self._bridge(peer, inbox))
                    else:
                        log.info("P2P %s: channel did not authenticate, closing", peer.key)
                        self._spawn(self._close(peer))
                    return
                inbox.put_nowait(frame)

            @dc.on("close")
            def _closed() -> None:
                self._spawn(self._close(peer))

    async def _bridge(self, peer: _Peer, inbox: asyncio.Queue) -> None:
        """Carry frames between the DataChannel and a loopback control socket
        until either side ends."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        url = f"http://127.0.0.1:{self.local_port}/api/control/ws"
        headers = {"Authorization": f"Bearer {self.token}",
                   VIA_HEADER: f"p2p {self.bridge_key}"}
        dc = peer.dc
        drained = asyncio.Event()
        drained.set()
        dc.bufferedAmountLowThreshold = LOW_WATER

        @dc.on("bufferedamountlow")
        def _low() -> None:
            drained.set()

        try:
            async with self._http.ws_connect(url, headers=headers, max_msg_size=0,
                                             autoping=True) as ws:
                async def upstream() -> None:  # page -> daemon
                    while True:
                        is_binary, data = await inbox.get()
                        if is_binary:
                            await ws.send_bytes(data)
                        else:
                            await ws.send_str(data)

                async def downstream() -> None:  # daemon -> page
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            parts = encode(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            parts = encode(msg.data)
                        else:
                            break
                        for part in parts:
                            if dc.readyState != "open":
                                return
                            dc.send(part)
                        if dc.bufferedAmount > HIGH_WATER:
                            drained.clear()
                            await drained.wait()

                up = asyncio.ensure_future(upstream())
                down = asyncio.ensure_future(downstream())
                try:
                    await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    up.cancel()
                    down.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the peer ends, the daemon does not
            log.info("P2P %s: bridge ended: %s", peer.key, exc)
        finally:
            await self._close(peer)

    async def _close(self, peer: _Peer) -> None:
        if peer.closed:
            return
        peer.closed = True
        peer.nonce = ""
        if self._peers.get(peer.key) is peer:
            del self._peers[peer.key]
        if peer.bridge is not None and peer.bridge is not asyncio.current_task():
            peer.bridge.cancel()
        try:
            # Bounded: aiortc's close was seen to wait forever for an SCTP
            # shutdown in the loopback tests (cause unknown), and whoever
            # awaits this -- a bridge's finally, stop() -- must not.
            await asyncio.wait_for(peer.pc.close(), CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("P2P %s: close did not finish in %.0fs", peer.key, CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001
            log.debug("P2P %s: close failed", peer.key, exc_info=True)

    async def stop(self) -> None:
        self._stopped = True
        for peer in list(self._peers.values()):
            await self._close(peer)
        for task in list(self._tasks):
            task.cancel()
        if self._http is not None:
            await self._http.close()


def via_p2p(request, hub: Optional[Hub]) -> bool:
    """Whether this control socket is a P2P bridge: the header must carry
    this process's bridge key, which only :meth:`Hub._bridge` knows."""
    if hub is None:
        return False
    value = request.headers.get(VIA_HEADER, "")
    expected = f"p2p {hub.bridge_key}"
    return bool(value) and hmac.compare_digest(value, expected)


def add_candidates(sdp: str, addrs) -> str:
    """``sdp`` with srflx candidate lines for ``addrs`` appended after its
    own. Their related address is the first host candidate, whose socket is
    the one aiortc checks from."""
    addrs = list(addrs)
    if not addrs:
        return sdp
    nl = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(nl)
    at = [i for i, line in enumerate(lines) if line.startswith("a=candidate:")]
    if not at:
        return sdp
    host = next((lines[i].split() for i in at if " typ host" in lines[i]), None)
    if host is None or len(host) < 6:
        return sdp
    have = {(lines[i].split()[4], lines[i].split()[5]) for i in at}
    extra = []
    for n, (ip, port) in enumerate(addrs):
        if (ip, str(port)) in have:
            continue
        # Below aiortc's own srflx (1694498815), so a real mapping wins a tie.
        prio = 1694498815 - 1 - n
        extra.append(f"a=candidate:pred{n} 1 udp {prio} {ip} {port} typ srflx "
                     f"raddr {host[4]} rport {host[5]}")
    last = at[-1] + 1
    return nl.join(lines[:last] + extra + lines[last:])


def _first_srflx_ip(sdp: str) -> Optional[str]:
    for line in sdp.splitlines():
        if line.startswith("a=candidate:") and " typ srflx" in line:
            parts = line.split()
            if len(parts) > 4:
                return parts[4]
    return None
