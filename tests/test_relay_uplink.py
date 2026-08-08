"""Tests for the relay uplink: wire codec + the uplink's stream plumbing.

Async tests follow this repo's convention of an inner ``run()`` driven by
``asyncio.run`` (no pytest-asyncio dependency). A fake WebSocket stands in for
aiohttp's client socket so the uplink's data path is exercised without any Rust
build; a loopback echo server stands in for the daemon's HTTP port.
"""

from __future__ import annotations

import asyncio
import struct

from claude_launcher.daemon import relay_wire as w
from claude_launcher.daemon.relay_uplink import RelayUplink


# --------------------------------------------------------------------------- #
# wire codec
# --------------------------------------------------------------------------- #
def test_register_roundtrip():
    room = bytes(range(16))
    frame = w.register(room, "work-pc", "tok-123")
    assert frame[:16] == room
    assert frame[16] == 3  # Tunnel channel
    plen = struct.unpack(">H", frame[17:19])[0]
    assert plen == len(frame) - w.HEADER_LEN
    payload = frame[w.HEADER_LEN :]
    assert payload[0] == w.REGISTER
    assert payload[1] == len("work-pc")
    assert payload[2:9] == b"work-pc"
    assert payload[9:] == b"tok-123"


def test_stream_data_split():
    room = bytes(16)
    big = b"x" * (w.MAX_STREAM_DATA * 2 + 5)
    frames = w.iter_stream_data(room, 7, big)
    assert len(frames) == 3
    dec = w.FrameDecoder()
    reassembled = b""
    for f in frames:
        for _room, payload in dec.feed(f):
            m = w.decode_payload(payload)
            assert m.kind == w.STREAM_DATA and m.sid == 7
            reassembled += m.data
    assert reassembled == big


def test_decoder_reassembles_split_and_coalesced():
    room = bytes(16)
    f1 = w.stream_data(room, 1, b"first")
    f2 = w.stream_eof(room, 1)
    dec = w.FrameDecoder()
    out = []
    out += dec.feed(f1[:5])
    out += dec.feed(f1[5:] + f2[:3])
    out += dec.feed(f2[3:])
    msgs = [w.decode_payload(p) for _r, p in out]
    assert [m.kind for m in msgs] == [w.STREAM_DATA, w.STREAM_EOF]
    assert msgs[0].data == b"first"


def test_ping_pong_roundtrip():
    room = bytes(16)
    for build, kind in ((w.ping, w.PING), (w.pong, w.PONG)):
        frame = build(room, 0xDEADBEEF)
        _r, payload = w.FrameDecoder().feed(frame)[0]
        m = w.decode_payload(payload)
        assert m.kind == kind and m.token == 0xDEADBEEF


def test_decode_ignores_unknown_type():
    # a terminal-profile DATA_OUT (0x02) is not a tunnel message → ignored
    assert w.decode_payload(bytes([0x02, 1, 2, 3])) is None
    assert w.decode_payload(b"") is None


def test_peer_list_roundtrip():
    room = bytes(16)
    frame = w.peer_list(room, 42)
    _r, payload = w.FrameDecoder().feed(frame)[0]
    assert payload == bytes([w.PEER_LIST]) + struct.pack(">I", 42)

    # relay-side reply: [req u32][count u8]([len u8][name])*
    body = struct.pack(">I", 42) + bytes([2, 3]) + b"pca" + bytes([7]) + b"work-pc"
    m = w.decode_payload(bytes([w.PEER_LIST_OK]) + body)
    assert m.kind == w.PEER_LIST_OK and m.req == 42
    assert m.names == ("pca", "work-pc")
    # empty list (peering disabled on the relay)
    m0 = w.decode_payload(bytes([w.PEER_LIST_OK]) + struct.pack(">I", 7) + b"\x00")
    assert m0.names == ()
    # truncated name table → the whole message is dropped, not misparsed
    trunc = struct.pack(">I", 9) + bytes([2, 3]) + b"pc"
    assert w.decode_payload(bytes([w.PEER_LIST_OK]) + trunc) is None


def test_uplink_peer_list_requires_cap():
    async def run():
        up = RelayUplink(url="ws://x", token="t", name="pc",
                         local_host="127.0.0.1", local_port=1)
        up._ws = _FakeWS()
        up.connected = True
        up.peering = True
        up.listing = False  # old relay: CAP_PEERING only
        try:
            await up.peer_list()
        except Exception as exc:
            assert "peer listing" in str(exc)
        else:
            raise AssertionError("peer_list should refuse without CAP_PEER_LIST")

        # with the cap: request goes out, the reply resolves the future
        up.listing = True
        task = asyncio.ensure_future(up.peer_list())
        room, sent = await up._ws.next_sent()
        assert sent.kind == w.PEER_LIST
        reply = (
            bytes([w.PEER_LIST_OK])
            + struct.pack(">I", sent.req)
            + bytes([1, 3])
            + b"pcb"
        )
        await up._handle(reply)
        assert await asyncio.wait_for(task, 2.0) == ["pcb"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# fake WebSocket + helpers
# --------------------------------------------------------------------------- #
class _FakeWS:
    """Stand-in for aiohttp's ClientWebSocketResponse used by the uplink.

    Frames the uplink sends are decoded into ``self.sent``; the test drives the
    relay side by feeding raw payloads to ``uplink._handle``.
    """

    def __init__(self):
        self.sent = asyncio.Queue()
        self._dec = w.FrameDecoder()

    async def send_bytes(self, data):
        for room, payload in self._dec.feed(data):
            m = w.decode_payload(payload)
            if m is not None:
                await self.sent.put((room, m))

    async def close(self):
        pass

    async def next_sent(self, timeout=2.0):
        return await asyncio.wait_for(self.sent.get(), timeout)


def _payload(frame: bytes) -> bytes:
    return frame[w.HEADER_LEN :]


async def _start_echo_server():
    """Loopback TCP server that upper-cases one read, then EOFs."""

    async def handle(reader, writer):
        data = await reader.read(1024)
        writer.write(data.upper())
        await writer.drain()
        writer.write_eof()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


# --------------------------------------------------------------------------- #
# uplink stream plumbing
# --------------------------------------------------------------------------- #
def test_uplink_pipes_stream_to_local_server():
    async def run():
        server, port = await _start_echo_server()
        room = bytes([9] * 16)
        up = RelayUplink(url="ws://x", token="t", name="pc",
                         local_host="127.0.0.1", local_port=port)
        up._room = room
        up._ws = _FakeWS()

        await up._handle(_payload(w.stream_open(room, 1)))
        await up._handle(_payload(w.stream_data(room, 1, b"hello relay")))
        await up._handle(_payload(w.stream_eof(room, 1)))

        got, saw_end = b"", False
        for _ in range(8):
            try:
                _r, m = await up._ws.next_sent()
            except asyncio.TimeoutError:
                break
            if m.kind == w.STREAM_DATA and m.sid == 1:
                got += m.data
            elif m.kind in (w.STREAM_EOF, w.STREAM_CLOSE):
                saw_end = True
        up.stop()
        server.close()
        assert got == b"HELLO RELAY"
        assert saw_end

    asyncio.run(run())


def test_uplink_answers_ping_with_pong():
    async def run():
        room = bytes([1] * 16)
        up = RelayUplink(url="ws://x", token="t", name="pc",
                         local_host="127.0.0.1", local_port=1)
        up._room = room
        up._ws = _FakeWS()
        await up._handle(_payload(w.ping(room, 42)))
        _r, m = await up._ws.next_sent()
        up.stop()
        assert m.kind == w.PONG and m.token == 42

    asyncio.run(run())


def test_uplink_closes_stream_on_local_connect_failure():
    async def run():
        room = bytes([2] * 16)
        # port 1 refuses → uplink must answer STREAM_CLOSE
        up = RelayUplink(url="ws://x", token="t", name="pc",
                         local_host="127.0.0.1", local_port=1)
        up._room = room
        up._ws = _FakeWS()
        await up._open_stream(5)
        _r, m = await up._ws.next_sent()
        up.stop()
        assert m.kind == w.STREAM_CLOSE and m.sid == 5

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# config gate
# --------------------------------------------------------------------------- #
def test_config_disabled_without_url():
    from claude_launcher.daemon import relay_uplink

    assert relay_uplink.config_from_env_and_dict(
        {}, local_host="127.0.0.1", local_port=1
    ) is None


def test_config_requires_token(monkeypatch):
    from claude_launcher.daemon import relay_uplink

    monkeypatch.delenv("CLAUNCH_RELAY_TOKEN", raising=False)
    assert relay_uplink.config_from_env_and_dict(
        {"url": "ws://r"}, local_host="127.0.0.1", local_port=1
    ) is None


def test_config_token_from_env(monkeypatch):
    from claude_launcher.daemon import relay_uplink

    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN", "envtok")
    up = relay_uplink.config_from_env_and_dict(
        {"url": "wss://r", "name": "pc"}, local_host="127.0.0.1", local_port=9
    )
    assert up is not None
    assert up.token == "envtok" and up.name == "pc"
