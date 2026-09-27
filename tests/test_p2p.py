"""The control socket over a WebRTC DataChannel (claunch-mhzt4).

A page reached through the relay crossed the Cloudflare edge on every read
and keystroke: 454-761ms per round trip from the user's network, against
11-13ms on a DataChannel straight to the daemon host (Phase 0 numbers on the
issue). These pin the daemon's end of that channel: the frames it is split
into, the NAT rule it predicts ports from, who may open a bridge, and that
what arrives over the channel is the control socket itself -- reads, parts,
acks and pongs served by the code that serves the relay socket.

The loopback tests run two aiortc peers in this process, the "browser" one
offering exactly as the page does (offer first, candidates trickled after).
aiortc is an optional extra (`.[p2p]`), so those tests -- and the one that
needs the daemon to be able to do p2p -- skip where it is not installed; the
framing, NAT and refusal tests run everywhere.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import channel, p2p, p2p_nat
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

needs_aiortc = pytest.mark.skipif(
    importlib.util.find_spec("aiortc") is None,
    reason="aiortc not installed (optional extra: .[p2p])",
)


# --------------------------------------------------------------- framing
def _roundtrip(data):
    asm = p2p.Reassembler()
    out = None
    parts = p2p.encode(data)
    for i, part in enumerate(parts):
        got = asm.feed(part)
        if i < len(parts) - 1:
            assert got is None, "only the last fragment completes a frame"
        out = got
    return parts, out


def test_a_small_text_frame_is_one_message():
    parts, out = _roundtrip('{"type":"ping","t":1}')
    assert len(parts) == 1 and parts[0][0] == 0
    assert out == (False, '{"type":"ping","t":1}')


def test_a_large_frame_is_split_and_joined_back_exactly():
    """UTF-8 split mid-character across fragments must still decode."""
    text = "가" * 20000  # 60000 bytes: four fragments, cut inside characters
    parts, out = _roundtrip(text)
    assert len(parts) == 4
    assert all(len(p) <= p2p.FRAGMENT + 1 for p in parts)
    assert [p[0] & p2p.FLAG_MORE for p in parts] == [2, 2, 2, 0]
    assert out == (False, text)


def test_binary_frames_keep_their_kind_and_bytes():
    blob = bytes(range(256)) * 200
    parts, out = _roundtrip(blob)
    assert all(p[0] & p2p.FLAG_BINARY for p in parts)
    assert out == (True, blob)
    assert _roundtrip(b"")[1] == (True, b"")


def test_the_reassembler_refuses_what_the_encoder_never_sends():
    asm = p2p.Reassembler()
    with pytest.raises(ValueError):
        asm.feed("text")
    with pytest.raises(ValueError):
        asm.feed(b"")
    asm.feed(bytes([p2p.FLAG_MORE]) + b"half")
    with pytest.raises(ValueError):
        asm.feed(bytes([p2p.FLAG_BINARY]) + b"other kind")


# --------------------------------------------------------------- NAT rule
def _obs(ip, **ports):
    return {f"stun:{name}:3478": [(ip, p) if p is not None else None for p in seq]
            for name, seq in ports.items()}


def test_the_measured_nat_is_sequential_with_its_base():
    """Phase 0: a fresh destination got 10400, each new socket +1; another
    destination already counted up to 10418 by earlier flows."""
    prof = p2p_nat.classify(_obs("103.150.62.229", cf=[10418, 10419, 10420],
                                 goog=[10400, 10401, 10402]))
    assert prof.kind == "sequential"
    assert prof.public_ip == "103.150.62.229" and prof.base == 10400


def test_ports_that_jump_predict_nothing():
    prof = p2p_nat.classify(_obs("1.2.3.4", a=[40000, 51234, 40017], b=[3, 4, 5]))
    assert prof.kind == "unknown"
    assert p2p_nat.predict(prof, 16) == []


def test_a_dropped_answer_leaves_that_server_out_not_the_verdict():
    prof = p2p_nat.classify(_obs("1.2.3.4", a=[10400, None, 10402], b=[500, 501, 502]))
    assert prof.kind == "sequential" and prof.base == 500


def test_no_answers_or_two_public_addresses_predict_nothing():
    assert p2p_nat.classify({"stun:x:1": [None, None]}).kind == "unreachable"
    two = {"stun:a:1": [("1.1.1.1", 5), ("1.1.1.1", 6)],
           "stun:b:1": [("2.2.2.2", 7), ("2.2.2.2", 8)]}
    assert p2p_nat.classify(two).kind == "unknown"


def test_the_window_starts_at_the_base_and_widens_by_live_attempts():
    prof = p2p_nat.Profile("sequential", public_ip="9.9.9.9", base=10400)
    assert p2p_nat.predict(prof, 4) == [("9.9.9.9", p) for p in range(10400, 10404)]
    assert len(p2p_nat.predict(prof, 4, recent=3)) == 7
    assert p2p_nat.predict(prof, 0) == []
    assert p2p_nat.predict(None, 16) == []


def test_the_profiler_keeps_the_lowest_base_it_has_read():
    """A later probe counts up servers the earlier one used, so its minimum
    is never below the true base -- the lowest reading is kept."""
    readings = iter([
        _obs("9.9.9.9", a=[10400, 10401, 10402]),
        _obs("9.9.9.9", a=[10403, 10404, 10405]),
    ])

    async def prober(servers):
        return next(readings)

    async def run():
        prof = p2p_nat.NatProfiler(["stun:a:3478"], ttl=0.0, prober=prober)
        first = await prof.get()
        second = await prof.get()
        return first.base, second.base

    assert asyncio.run(run()) == (10400, 10400)


def test_attempts_towards_one_address_are_counted():
    prof = p2p_nat.NatProfiler([])
    assert prof.note_attempt("5.5.5.5") == 0
    assert prof.note_attempt("5.5.5.5") == 1
    assert prof.note_attempt("6.6.6.6") == 0


def test_stun_urls_parse():
    assert p2p_nat.parse_stun_url("stun:stun.cloudflare.com:3478") == ("stun.cloudflare.com", 3478)
    assert p2p_nat.parse_stun_url("stun:example.org") == ("example.org", 3478)
    assert p2p_nat.parse_stun_url("turn:x:1") is None


# --------------------------------------------------------------- candidates
ANSWER = "\r\n".join([
    "v=0", "m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
    "a=candidate:1 1 udp 2130706431 172.20.78.18 54669 typ host",
    "a=candidate:2 1 udp 1694498815 103.150.62.229 10430 typ srflx raddr 172.20.78.18 rport 54669",
    "a=end-of-candidates", "",
])


def test_predicted_ports_follow_the_real_candidates():
    out = p2p.add_candidates(ANSWER, [("103.150.62.229", 10400), ("103.150.62.229", 10430)])
    lines = out.split("\r\n")
    at = lines.index("a=end-of-candidates")
    assert lines[at - 1].startswith("a=candidate:pred0 1 udp ")
    assert "103.150.62.229 10400 typ srflx raddr 172.20.78.18 rport 54669" in lines[at - 1]
    assert sum("10430" in line for line in lines) == 1, "an existing candidate is not repeated"
    assert p2p.add_candidates(ANSWER, []) == ANSWER


# --------------------------------------------------------------- who may say "p2p"
class _Req:
    def __init__(self, headers):
        self.headers = headers


def test_only_the_bridge_key_marks_a_socket_as_p2p():
    hub = p2p.Hub(local_port=1, token="t", stun=[], probe_stun=[], predict=0)
    assert p2p.via_p2p(_Req({p2p.VIA_HEADER: f"p2p {hub.bridge_key}"}), hub)
    assert not p2p.via_p2p(_Req({p2p.VIA_HEADER: "p2p"}), hub)
    assert not p2p.via_p2p(_Req({p2p.VIA_HEADER: "p2p guess"}), hub)
    assert not p2p.via_p2p(_Req({}), hub)
    assert not p2p.via_p2p(_Req({p2p.VIA_HEADER: f"p2p {hub.bridge_key}"}), None)


def test_the_daemon_starts_no_hub_when_off_or_without_aiortc(monkeypatch):
    from claude_launcher.daemon import __main__ as main_mod

    assert main_mod._start_p2p({"p2p_enabled": False}, 1, "t") is None
    monkeypatch.setattr(p2p, "_AIORTC", False)
    assert main_mod._start_p2p({"p2p_enabled": True}, 1, "t") is None


# --------------------------------------------------------------- the socket
async def _serve(tmp_path, *, hub=True, predict=0):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    h = None
    if hub:
        h = p2p.Hub(local_port=client.server.port, token="sekrit", stun=[],
                    probe_stun=[], predict=predict)
        app["p2p"] = h
    return client, h


async def _next(ws, kind):
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), 10)
        if frame.get("type") == kind:
            return frame


@needs_aiortc
def test_the_init_frame_names_p2p_only_when_the_daemon_can_do_it(home, tmp_path, monkeypatch):
    async def run():
        client, hub = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                assert await _next(ws, "init") == {"type": "init", "p2p": {"stun": []}}
            monkeypatch.setattr(p2p, "_AIORTC", False)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                assert await _next(ws, "init") == {"type": "init"}
                await ws.send_json({"type": "p2p_offer", "id": "x", "sdp": "v=0"})
                err = await _next(ws, "p2p_error")
                assert err["id"] == "x" and err["error"] == "p2p unavailable"
        finally:
            await client.close()

    asyncio.run(run())


def test_without_a_hub_an_offer_is_refused_and_nothing_else_changes(home, tmp_path):
    async def run():
        client, _ = await _serve(tmp_path, hub=False)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                assert await _next(ws, "init") == {"type": "init"}
                await ws.send_json({"type": "p2p_offer", "id": "a", "sdp": "v=0"})
                assert (await _next(ws, "p2p_error"))["error"] == "p2p unavailable"
                await ws.send_json({"type": "p2p_ice", "id": "a", "candidate": None})
                await ws.send_json({"type": "ping", "t": 5})
                pong = await _next(ws, "pong")
                assert pong["t"] == 5 and "via" not in pong
        finally:
            await client.close()

    asyncio.run(run())


def test_a_guessed_via_header_does_not_mark_the_pong(home, tmp_path):
    """A client that is not the bridge cannot switch off the page's tunnel
    split by sending the header (DESIGN CHECK 2)."""

    async def run():
        client, hub = await _serve(tmp_path)
        try:
            headers = dict(BEARER, **{p2p.VIA_HEADER: "p2p"})
            async with client.ws_connect("/api/control/ws", headers=headers) as ws:
                await _next(ws, "init")
                await ws.send_json({"type": "ping", "t": 1})
                assert "via" not in await _next(ws, "pong")
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------- aiortc loopback
class _Browser:
    """The page's side, in aiortc: offer first, candidates trickled after."""

    def __init__(self, ws, pid):
        from aiortc import RTCPeerConnection

        self.ws = ws
        self.pid = pid
        self.pc = RTCPeerConnection()
        self.dc = self.pc.createDataChannel("claunch-control", ordered=True)
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.asm = p2p.Reassembler()
        self.opened = asyncio.Event()
        self.closed = asyncio.Event()
        self.dc.on("open", self.opened.set)
        self.dc.on("close", self.closed.set)

        @self.dc.on("message")
        def _msg(message):
            frame = self.asm.feed(message)
            if frame is not None:
                self.inbox.put_nowait(frame)

    async def negotiate(self):
        from aiortc import RTCSessionDescription

        await self.pc.setLocalDescription(await self.pc.createOffer())
        sdp = self.pc.localDescription.sdp
        lines = sdp.split("\r\n")
        cands = [line[2:] for line in lines if line.startswith("a=candidate:")]
        bare = "\r\n".join(line for line in lines
                           if not line.startswith("a=candidate:")
                           and line != "a=end-of-candidates")
        await self.ws.send_json({"type": "p2p_offer", "id": self.pid, "sdp": bare})
        for cand in cands:
            await self.ws.send_json({"type": "p2p_ice", "id": self.pid, "candidate": {
                "candidate": cand, "sdpMid": "0", "sdpMLineIndex": 0}})
        await self.ws.send_json({"type": "p2p_ice", "id": self.pid, "candidate": None})
        answer = await _next(self.ws, "p2p_answer")
        assert answer["id"] == self.pid
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
        await asyncio.wait_for(self.opened.wait(), 15)
        return answer

    def send(self, data):
        for part in p2p.encode(data):
            self.dc.send(part)

    async def frame(self, kind=None, timeout=10):
        while True:
            is_binary, data = await asyncio.wait_for(self.inbox.get(), timeout)
            if is_binary:
                continue
            msg = json.loads(data)
            if kind is None or msg.get("type") == kind:
                return msg


async def _shut(page):
    """The test browser's own close, bounded: aiortc's close can wait on an
    SCTP shutdown that never completes (seen here, cause unknown)."""
    try:
        await asyncio.wait_for(page.pc.close(), 5)
    except asyncio.TimeoutError:
        pass


async def _gone(hub, timeout=10):
    """Until the hub holds no peer (closed or never authenticated)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not hub._peers:
            return True
        await asyncio.sleep(0.05)
    return False


@needs_aiortc
def test_the_datachannel_is_the_control_socket(home, tmp_path, monkeypatch):
    """After the nonce the channel serves init, pongs marked p2p, and reads
    in parts with their acks -- through a bridge made to wait on a full
    buffer after every frame, which must delay frames and lose none."""
    monkeypatch.setattr(channel.Carrier, "PART_CHARS", 64)
    monkeypatch.setattr(p2p, "HIGH_WATER", 0)
    monkeypatch.setattr(p2p, "LOW_WATER", 0)

    async def run():
        client, hub = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _next(ws, "init")
                page = _Browser(ws, "t1")
                answer = await page.negotiate()
                page.send(answer["nonce"])
                assert await page.frame("init") == {"type": "init"}, \
                    "a bridged socket is not offered P2P again"

                page.send(json.dumps({"type": "ping", "t": 42}))
                pong = await page.frame("pong")
                assert pong["t"] == 42 and pong["via"] == "p2p"

                page.send(json.dumps({"type": "read", "id": 9, "parts": True,
                                      "paths": ["/api/sessions", "/api/workspaces"]}))
                chunks = {}
                while True:
                    msg = await page.frame()
                    if msg.get("type") == "read_part":
                        chunks[msg["seq"]] = msg["data"]
                        page.send(json.dumps({"type": "read_ack", "id": 9, "seq": msg["seq"]}))
                        if not msg.get("more"):
                            break
                    elif msg.get("type") == "read_result":
                        chunks = None
                        break
                assert chunks and len(chunks) > 1, "parts crossed the channel"
                whole = json.loads("".join(chunks[k] for k in sorted(chunks)))
                direct = await (await client.get("/api/workspaces", headers=BEARER)).json()
                assert whole["answers"]["/api/workspaces"] == direct

                # The relay socket that carried the signalling is untouched.
                await ws.send_json({"type": "ping", "t": 7})
                relay_pong = await _next(ws, "pong")
                assert relay_pong["t"] == 7 and "via" not in relay_pong
                assert hub.status()["peers"] == 1
                await _shut(page)
                assert await _gone(hub), "the peer goes when the browser does"
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())


@needs_aiortc
def test_a_wrong_nonce_closes_the_peer_and_not_the_relay_socket(home, tmp_path):
    async def run():
        client, hub = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _next(ws, "init")
                page = _Browser(ws, "bad")
                await page.negotiate()
                page.send("not-the-nonce")
                assert await _gone(hub)
                with pytest.raises(asyncio.TimeoutError):
                    await page.frame("init", timeout=0.5)
                await ws.send_json({"type": "ping", "t": 3})
                assert (await _next(ws, "pong"))["t"] == 3
            await _shut(page)
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())


@needs_aiortc
def test_a_nonce_opens_one_channel_once(home, tmp_path):
    """A second peer presenting the first peer's (spent) nonce is closed,
    and the first peer keeps working (DESIGN CHECK 3: reuse)."""

    async def run():
        client, hub = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _next(ws, "init")
                first = _Browser(ws, "one")
                spent = (await first.negotiate())["nonce"]
                first.send(spent)
                await first.frame("init")

                second = _Browser(ws, "two")
                await second.negotiate()
                second.send(spent)
                end = time.monotonic() + 10
                while len(hub._peers) > 1:
                    assert time.monotonic() < end, "the reused nonce was accepted"
                    await asyncio.sleep(0.05)
                assert [k.rsplit("/", 1)[1] for k in hub._peers] == ["one"]

                first.send(json.dumps({"type": "ping", "t": 11}))
                assert (await first.frame("pong"))["t"] == 11
                await _shut(first)
                await _shut(second)
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())


@needs_aiortc
def test_a_nonce_that_arrives_late_finds_the_peer_gone(home, tmp_path, monkeypatch):
    """DESIGN CHECK 3: late. The peer is closed at AUTH_TIMEOUT and the
    relay socket goes on answering."""
    monkeypatch.setattr(p2p, "AUTH_TIMEOUT", 0.3)

    async def run():
        client, hub = await _serve(tmp_path)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _next(ws, "init")
                page = _Browser(ws, "late")
                answer = await page.negotiate()
                assert await _gone(hub, timeout=5)
                if page.dc.readyState == "open":
                    page.send(answer["nonce"])
                with pytest.raises(asyncio.TimeoutError):
                    await page.frame("init", timeout=0.5)
                await ws.send_json({"type": "ping", "t": 4})
                assert (await _next(ws, "pong"))["t"] == 4
                await _shut(page)
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())


@needs_aiortc
def test_a_negotiating_peer_dies_with_its_signalling_socket(home, tmp_path):
    async def run():
        client, hub = await _serve(tmp_path)
        try:
            ws = await client.ws_connect("/api/control/ws", headers=BEARER)
            await _next(ws, "init")
            page = _Browser(ws, "orphan")
            await page.negotiate()
            assert len(hub._peers) == 1
            await ws.close()
            assert await _gone(hub)
            await _shut(page)
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())


@needs_aiortc
def test_predicted_candidates_ride_the_answer(home, tmp_path, monkeypatch):
    async def fake_probe(servers):
        return {"stun:a:1": [("203.0.113.9", 20000), ("203.0.113.9", 20001)]}

    async def run():
        client, hub = await _serve(tmp_path, predict=3)
        hub.profiler = p2p_nat.NatProfiler(["stun:a:1"], prober=fake_probe)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _next(ws, "init")
                page = _Browser(ws, "pred")
                answer = await page.negotiate()
                preds = [line for line in answer["sdp"].split("\r\n") if "candidate:pred" in line]
                assert [line.split()[5] for line in preds] == ["20000", "20001", "20002"]
                assert all(" 203.0.113.9 " in line for line in preds)
                await _shut(page)
        finally:
            await hub.stop()
            await client.close()

    asyncio.run(run())
