"""The uplink's sender puts small frames ahead of bulk ones (claunch-iss86).

Every tunnelled stream and the keepalive share the uplink's one connection.
Written in arrival order, a PING queued behind a large response waited for
all of it. The round trip to the relay measured 80ms at its floor and
100-408ms at other times (cause not separated). What these pin:

- a frame that is not stream data goes ahead of queued stream data;
- queued stream data goes one frame per stream in turn;
- EOF/CLOSE of a stream stay behind that stream's own queued data;
- stream data waits while the socket's buffer is above the low-water mark,
  and a control frame still gets through meanwhile;
- a dropped connection releases whoever waited, without a cancellation.
"""

from __future__ import annotations

import asyncio

from claude_launcher.daemon import relay_wire as w
from claude_launcher.daemon import sendq
from claude_launcher.daemon.relay_uplink import RelayUplink

ROOM = bytes([7] * 16)


class _Transport:
    def __init__(self):
        self.buffered = 0

    def get_write_buffer_size(self):
        return self.buffered


class _Writer:
    def __init__(self):
        self.transport = _Transport()


class _GatedWS:
    """Records each frame as it is written, then waits for the test to let
    the next write through: a link that takes one frame at a time."""

    def __init__(self, gated=True):
        self.written = []
        self.gated = gated
        self.permits = asyncio.Semaphore(0)
        self._writer = _Writer()

    async def send_bytes(self, data):
        self.written.append(data)
        if self.gated:
            await self.permits.acquire()

    async def close(self):
        pass


def _uplink(ws):
    up = RelayUplink(url="ws://x", token="t", name="pc",
                     local_host="127.0.0.1", local_port=1)
    up._room = ROOM
    up._ws = ws
    return up


def _label(frame):
    kind, sid = w.kind_and_sid(frame)
    name = {w.STREAM_DATA: "D", w.STREAM_EOF: "E", w.STREAM_CLOSE: "C",
            w.PING: "P", w.PONG: "Q"}[kind]
    if kind == w.STREAM_DATA:
        return f"D{sid}:{w.decode_payload(frame[w.HEADER_LEN:]).data.decode()}"
    return f"{name}{sid if sid is not None else ''}"


async def _release_all(ws, count):
    for _ in range(count):
        ws.permits.release()
    await asyncio.sleep(0)


def test_kind_and_sid_reads_the_frame_header():
    assert w.kind_and_sid(w.stream_data(ROOM, 9, b"x")) == (w.STREAM_DATA, 9)
    assert w.kind_and_sid(w.stream_close(ROOM, 3)) == (w.STREAM_CLOSE, 3)
    assert w.kind_and_sid(w.ping(ROOM, 1)) == (w.PING, None)
    assert w.kind_and_sid(b"") == (None, None)


def test_a_ping_goes_ahead_of_queued_stream_data():
    async def run():
        ws = _GatedWS()
        up = _uplink(ws)
        sends = [asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 1, b"%d" % i)))
                 for i in range(3)]
        await asyncio.sleep(0.01)  # the first data frame is on the wire
        ping = asyncio.ensure_future(up._raw_send(w.ping(ROOM, 5)))
        await asyncio.sleep(0)
        await _release_all(ws, 4)
        await asyncio.wait_for(asyncio.gather(*sends, ping), 2)
        assert [_label(f) for f in ws.written] == ["D1:0", "P", "D1:1", "D1:2"]

    asyncio.run(run())


def test_streams_take_turns():
    async def run():
        ws = _GatedWS()
        up = _uplink(ws)
        big = [asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 1, b"%d" % i)))
               for i in range(3)]
        small = asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 2, b"a")))
        await _release_all(ws, 4)
        await asyncio.wait_for(asyncio.gather(*big, small), 2)
        assert [_label(f) for f in ws.written] == ["D1:0", "D2:a", "D1:1", "D1:2"]

    asyncio.run(run())


def test_eof_and_close_stay_behind_their_streams_data():
    async def run():
        ws = _GatedWS()
        up = _uplink(ws)
        data = [asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 4, b"%d" % i)))
                for i in range(2)]
        eof = asyncio.ensure_future(up._raw_send(w.stream_eof(ROOM, 4)))
        close = asyncio.ensure_future(up._raw_send(w.stream_close(ROOM, 4)))
        other = asyncio.ensure_future(up._raw_send(w.stream_close(ROOM, 8)))
        await _release_all(ws, 5)
        await asyncio.wait_for(asyncio.gather(*data, eof, close, other), 2)
        # The other stream's CLOSE has nothing queued to wait behind, so it
        # goes first; stream 4's own end waits for stream 4's data.
        assert [_label(f) for f in ws.written] == ["C8", "D4:0", "D4:1", "E4", "C4"]

    asyncio.run(run())


def test_stream_data_waits_on_a_full_socket_but_a_ping_does_not():
    async def run():
        ws = _GatedWS(gated=False)
        ws._writer.transport.buffered = sendq.LOW_WATER + 1
        up = _uplink(ws)
        data = asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 1, b"x")))
        await asyncio.sleep(0.05)
        assert ws.written == []
        await asyncio.wait_for(up._raw_send(w.ping(ROOM, 9)), 2)
        assert [_label(f) for f in ws.written] == ["P"]
        ws._writer.transport.buffered = 0
        await asyncio.wait_for(data, 2)
        assert [_label(f) for f in ws.written] == ["P", "D1:x"]

    asyncio.run(run())


def test_a_dropped_connection_releases_the_waiting_senders():
    async def run():
        ws = _GatedWS()
        up = _uplink(ws)
        sends = [asyncio.ensure_future(up._raw_send(w.stream_data(ROOM, 1, b"%d" % i)))
                 for i in range(3)]
        await asyncio.sleep(0.01)
        up._ws = None
        up._stop_sender()
        # _raw_send absorbs a dropped connection, as it did when it wrote
        # frames itself; a CancelledError here would end the caller's task.
        await asyncio.wait_for(asyncio.gather(*sends), 2)
        assert all(s.done() and not s.cancelled() for s in sends)

    asyncio.run(run())


def test_a_new_connection_starts_a_new_sender():
    async def run():
        first = _GatedWS(gated=False)
        up = _uplink(first)
        await up._raw_send(w.ping(ROOM, 1))
        second = _GatedWS(gated=False)
        up._ws = second
        await asyncio.wait_for(up._raw_send(w.ping(ROOM, 2)), 2)
        assert len(first.written) == 1 and len(second.written) == 1
        up._stop_sender()

    asyncio.run(run())
