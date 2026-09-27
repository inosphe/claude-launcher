"""What this host's NAT does to a new UDP flow, measured at run time.

The page reaches the daemon peer to peer only if its connectivity checks
arrive at a port the daemon's NAT has open. The daemon's own STUN answer
names one such port -- the one the NAT picked for the STUN server. On a NAT
that keeps one mapping per socket (endpoint-independent) that is also the
port the browser's packets reach, and ICE needs nothing more. The NAT this
feature was measured behind does not: it gives every destination its own
port, counting up from a fixed base as new internal sockets open flows to it
(claunch-mhzt4 Phase 0: 25 new sockets to one server got 10400..10424 with no
gap, and the first flow to any destination got 10400). The STUN answer is
then the port for the STUN server and nothing else, and plain STUN ICE failed
0/3 from the user's network.

What the page CAN be told is where the port for its own flow will land: the
daemon's first check towards the browser opens a new flow to a destination
nobody behind this NAT has talked to, so it gets the base, or a few past it
when earlier attempts towards the same browser address are still mapped.
Offering those ports as extra candidates connected 3/3 in the same test.

Nothing here is hard-coded to that NAT. :func:`probe` measures the rule --
several new sockets per STUN server, one request each -- and
:func:`classify` names it: ``sequential`` (every server saw +1 per new
socket) is the one shape that predicts; anything else predicts nothing and
the page falls back to the relay exactly as it would without this module.
The prediction is only ever extra candidates: on a NAT where it is wrong the
browser checks a few ports that do not answer, and the real srflx pair (if
any) still wins.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("claunch.daemon.p2p_nat")

MAGIC = 0x2112A442
#: How long one STUN request waits for its answer, and how often it is sent.
QUERY_TIMEOUT = 1.0
QUERY_TRIES = 2
#: New sockets per server. Three +1 steps in a row is not a coincidence a
#: randomising NAT produces; two could be.
SOCKETS_PER_SERVER = 3
#: A profile older than this is measured again before it is used. The
#: measured NAT forgot an idle destination's counter after roughly ten
#: minutes, so a base read long ago may no longer describe the next flow.
PROFILE_TTL = 300.0
#: How long an attempt towards a browser address is assumed to keep its
#: mapping (>= 423s measured, reset seen after ~10 min idle).
ATTEMPT_MEMORY = 600.0


def parse_stun_url(url: str) -> Optional[Tuple[str, int]]:
    """``stun:host:port`` (or ``stun:host``) -> ``(host, port)``."""
    if not isinstance(url, str) or not url.startswith("stun:"):
        return None
    rest = url[5:].split("?", 1)[0]
    host, _, port = rest.rpartition(":")
    if not host:
        return rest, 3478
    try:
        return host, int(port)
    except ValueError:
        return None


def binding_request() -> Tuple[bytes, bytes]:
    tid = os.urandom(12)
    return tid, struct.pack("!HHI", 1, 0, MAGIC) + tid


def mapped_address(data: bytes, tid: bytes) -> Optional[Tuple[str, int]]:
    """XOR-MAPPED-ADDRESS of a binding success answering ``tid``."""
    if len(data) < 20 or data[8:20] != tid:
        return None
    i = 20
    while i + 4 <= len(data):
        kind, length = struct.unpack("!HH", data[i:i + 4])
        value = data[i + 4:i + 4 + length]
        if kind == 0x0020 and len(value) >= 8 and value[1] == 0x01:
            port = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
            addr = struct.unpack("!I", value[4:8])[0] ^ MAGIC
            return socket.inet_ntoa(struct.pack("!I", addr)), port
        i += 4 + length + ((4 - length % 4) % 4)
    return None


class _Stun(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.waiting: Dict[bytes, asyncio.Future] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        tid = data[8:20]
        fut = self.waiting.get(tid)
        if fut is not None and not fut.done():
            got = mapped_address(data, tid)
            if got is not None:
                fut.set_result(got)

    def error_received(self, exc) -> None:  # noqa: D401 -- asyncio callback
        pass


async def query_new_socket(host: str, port: int) -> Optional[Tuple[str, int]]:
    """One fresh UDP socket, one binding request, its mapped address."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET,
                                       type=socket.SOCK_DGRAM)
    except OSError:
        return None
    if not infos:
        return None
    dest = infos[0][4]
    transport, proto = await loop.create_datagram_endpoint(
        _Stun, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
    try:
        for _ in range(QUERY_TRIES):
            tid, packet = binding_request()
            fut = loop.create_future()
            proto.waiting[tid] = fut
            transport.sendto(packet, dest)
            try:
                return await asyncio.wait_for(fut, QUERY_TIMEOUT)
            except asyncio.TimeoutError:
                continue
            finally:
                proto.waiting.pop(tid, None)
        return None
    finally:
        transport.close()


async def probe(servers: Sequence[str], per_server: int = SOCKETS_PER_SERVER
                ) -> Dict[str, List[Optional[Tuple[str, int]]]]:
    """For each STUN server, ``per_server`` new sockets asked one after the
    other. Servers run in parallel: the rule being measured is per
    destination, so one server's sockets do not move another's count."""

    async def one(url: str):
        target = parse_stun_url(url)
        if target is None:
            return url, []
        seen = []
        for _ in range(per_server):
            seen.append(await query_new_socket(*target))
        return url, seen

    pairs = await asyncio.gather(*(one(u) for u in servers))
    return dict(pairs)


@dataclass
class Profile:
    """What :func:`classify` concluded from one probe."""

    kind: str  # "sequential" | "unknown" | "unreachable"
    public_ip: Optional[str] = None
    #: Lowest first port a server saw: the port a destination nobody has
    #: talked to gets, when some probed server was fresh.
    base: Optional[int] = None
    detail: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    measured_at: float = 0.0

    def as_dict(self) -> dict:
        return {"kind": self.kind, "public_ip": self.public_ip,
                "base": self.base, "detail": self.detail}


def classify(observed: Dict[str, List[Optional[Tuple[str, int]]]]) -> Profile:
    """Name the rule the probe's answers show.

    ``sequential`` needs every server that answered all its sockets to have
    seen consecutive ports (+1 per socket), and at least one such server, all
    on one public address. A server that dropped a request says nothing
    either way and is left out; a server whose ports jump says the rule is
    not the counting one, and then nothing is predicted.
    """
    detail = {url: [a[1] if a else None for a in answers]
              for url, answers in observed.items()}
    ips = {a[0] for answers in observed.values() for a in answers if a}
    if not ips:
        return Profile("unreachable", detail=detail)
    if len(ips) != 1:
        return Profile("unknown", detail=detail)
    public_ip = next(iter(ips))
    complete = [ports for ports in detail.values()
                if len(ports) >= 2 and all(p is not None for p in ports)]
    if not complete:
        return Profile("unknown", public_ip=public_ip, detail=detail)
    for ports in complete:
        if any(b - a != 1 for a, b in zip(ports, ports[1:])):
            return Profile("unknown", public_ip=public_ip, detail=detail)
    base = min(ports[0] for ports in complete)
    return Profile("sequential", public_ip=public_ip, base=base, detail=detail)


def predict(profile: Optional[Profile], window: int, recent: int = 0
            ) -> List[Tuple[str, int]]:
    """The ``(ip, port)`` candidates to offer for the next flow.

    ``recent`` is how many earlier attempts towards the same browser address
    may still hold a mapping: each moved that destination's count by one, so
    the window is widened by as many.
    """
    if profile is None or profile.kind != "sequential" or window <= 0:
        return []
    if profile.base is None or not profile.public_ip:
        return []
    top = min(65535, profile.base + window + max(0, recent))
    return [(profile.public_ip, port) for port in range(profile.base, top)]


class NatProfiler:
    """One cached :class:`Profile` per daemon, measured on demand.

    ``base`` is kept as the lowest value this process has ever read: a later
    probe runs against servers the earlier one already counted up, so its
    minimum can only be as low as the true base or higher.
    """

    def __init__(self, servers: Sequence[str], *, ttl: float = PROFILE_TTL,
                 prober=probe) -> None:
        self.servers = list(servers)
        self.ttl = ttl
        self._probe = prober
        self._profile: Optional[Profile] = None
        self._lowest: Optional[int] = None
        self._lock = asyncio.Lock()
        self._attempts: Dict[str, List[float]] = {}

    @property
    def profile(self) -> Optional[Profile]:
        return self._profile

    async def get(self) -> Optional[Profile]:
        async with self._lock:
            fresh = (self._profile is not None
                     and time.monotonic() - self._profile.measured_at < self.ttl)
            if fresh:
                return self._profile
            if len(self.servers) < 1:
                return None
            try:
                observed = await self._probe(self.servers)
            except Exception:  # noqa: BLE001 -- a probe failing predicts nothing
                log.debug("NAT probe failed", exc_info=True)
                observed = {}
            prof = classify(observed)
            if prof.kind == "sequential" and prof.base is not None:
                if self._lowest is None or prof.base < self._lowest:
                    self._lowest = prof.base
                prof.base = self._lowest
            prof.measured_at = time.monotonic()
            self._profile = prof
            log.info("NAT profile: %s", prof.as_dict())
            return prof

    def note_attempt(self, address: str) -> int:
        """Record an attempt towards ``address``; return how many earlier
        ones may still be mapped."""
        now = time.monotonic()
        live = [t for t in self._attempts.get(address, []) if now - t < ATTEMPT_MEMORY]
        count = len(live)
        live.append(now)
        self._attempts[address] = live[-64:]
        return count
