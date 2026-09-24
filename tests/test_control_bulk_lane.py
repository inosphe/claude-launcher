"""Read answers go in a bulk lane behind the socket's small frames.

The control socket carries every terminal the page is looking at, its pongs,
and every read it polls. A rail listing all sessions is 1.2MB and the Beads
queues 1.4MB; written in arrival order, a pong or a keystroke's echo queued
behind one waited for all of it to reach the page -- through the relay
tunnel, seconds (claunch-iss86, max 2.2s on the badge). What these pin:

- a frame from the urgent lane (pong, terminal output) goes ahead of a read
  answer that was queued first;
- a page that asks for parts gets a large answer in ``read_part`` frames
  that put back together into the same answer, and no more than
  ``BULK_WINDOW`` characters of parts are out unacknowledged;
- a read answer waits while the socket's buffer is above the low-water mark;
- the pong reports how long the ping was inside the daemon.
"""

from __future__ import annotations

import asyncio
import json

from claude_launcher.daemon import channel, sendq


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
    the next write through."""

    def __init__(self, gated=True):
        self.sent = []
        self.gated = gated
        self.permits = asyncio.Semaphore(0)
        self.closed = False
        self._writer = _Writer()

    def exception(self):
        return None

    async def _write(self, data):
        self.sent.append(data)
        if self.gated:
            await self.permits.acquire()

    async def send_str(self, data):
        await self._write(data)

    async def send_bytes(self, data):
        await self._write(data)

    async def close(self, *, code=1000, message=b""):
        self.closed = True


def _kind(frame):
    if isinstance(frame, bytes):
        return "bytes"
    return json.loads(frame)["type"]


def test_a_pong_and_terminal_output_go_ahead_of_a_queued_read_answer():
    async def run():
        ws = _GatedWS()
        carrier = channel.Carrier(ws, None, None)
        first = asyncio.ensure_future(carrier.send_bulk({"type": "read_result", "id": 1}))
        await asyncio.sleep(0.01)  # on the wire
        second = asyncio.ensure_future(carrier.send_bulk({"type": "read_result", "id": 2}))
        term = asyncio.ensure_future(carrier.send_bytes(b"\x00\x01out"))
        await asyncio.sleep(0)  # the terminal's frame is queued before the pong
        await carrier.send_soon(json.dumps({"type": "pong"}))
        await asyncio.sleep(0.01)
        for _ in range(4):
            ws.permits.release()
        await asyncio.wait_for(asyncio.gather(first, second, term), 2)
        assert [_kind(f) for f in ws.sent] == ["read_result", "bytes", "pong", "read_result"]
        await carrier.shutdown()

    asyncio.run(run())


def test_a_large_answer_goes_in_parts_held_to_the_window():
    async def run():
        ws = _GatedWS(gated=False)
        carrier = channel.Carrier(ws, None, None)
        carrier.PART_CHARS = 10
        carrier.BULK_WINDOW = 30
        answer = {"type": "read_result", "id": 7, "answers": {"/a": "x" * 60},
                  "errors": {}, "statuses": {}}
        sending = asyncio.ensure_future(carrier.send_bulk(answer, parts=True, key=7))
        await asyncio.sleep(0.05)
        parts = [json.loads(f) for f in ws.sent]
        assert [p["type"] for p in parts] == ["read_part"] * 3, "three parts fill the window"
        assert [p["seq"] for p in parts] == [0, 1, 2]
        assert all(p["id"] == 7 and p["more"] for p in parts)
        assert not sending.done()

        # A pong is not held by the window.
        await carrier.send_soon(json.dumps({"type": "pong"}))
        await asyncio.sleep(0.01)
        assert _kind(ws.sent[-1]) == "pong"

        # Each acknowledgement lets one more part out, until the last.
        seq = 0
        while not sending.done():
            carrier.ack({"type": "read_ack", "id": 7, "seq": seq})
            seq += 1
            await asyncio.sleep(0.01)
        parts = [json.loads(f) for f in ws.sent if _kind(f) == "read_part"]
        assert not parts[-1]["more"] and all(p["more"] for p in parts[:-1])
        assert json.loads("".join(p["data"] for p in parts)) == answer
        await carrier.shutdown()

    asyncio.run(run())


def test_a_small_answer_and_a_page_without_parts_get_one_frame():
    async def run():
        ws = _GatedWS(gated=False)
        carrier = channel.Carrier(ws, None, None)
        carrier.PART_CHARS = 10
        big = {"type": "read_result", "id": 1, "answers": {"/a": "x" * 60}}
        await asyncio.wait_for(carrier.send_bulk(big), 2)  # an older page
        await asyncio.wait_for(carrier.send_bulk({"id": 2}, parts=True, key=2), 2)
        assert [json.loads(f) for f in ws.sent] == [big, {"id": 2}]
        await carrier.shutdown()

    asyncio.run(run())


def test_a_read_answer_waits_on_a_full_socket_but_a_pong_does_not():
    async def run():
        ws = _GatedWS(gated=False)
        ws._writer.transport.buffered = sendq.LOW_WATER + 1
        carrier = channel.Carrier(ws, None, None)
        answer = asyncio.ensure_future(carrier.send_bulk({"type": "read_result", "id": 1}))
        await asyncio.sleep(0.05)
        assert ws.sent == []
        await carrier.send_soon(json.dumps({"type": "pong"}))
        await asyncio.sleep(0.02)
        assert [_kind(f) for f in ws.sent] == ["pong"]
        ws._writer.transport.buffered = 0
        await asyncio.wait_for(answer, 2)
        assert [_kind(f) for f in ws.sent] == ["pong", "read_result"]
        await carrier.shutdown()

    asyncio.run(run())


def test_shutdown_releases_a_read_waiting_on_acknowledgements():
    async def run():
        ws = _GatedWS(gated=False)
        carrier = channel.Carrier(ws, None, None)
        carrier.PART_CHARS = 10
        carrier.BULK_WINDOW = 10
        sending = asyncio.ensure_future(
            carrier.send_bulk({"id": 3, "a": "x" * 50}, parts=True, key=3))
        await asyncio.sleep(0.02)
        await carrier.shutdown()
        try:
            await asyncio.wait_for(sending, 2)
        except ConnectionResetError:
            pass

    asyncio.run(run())


def test_the_pong_says_how_long_the_ping_was_in_the_daemon():
    from claude_launcher.daemon import api
    import time

    class _App(dict):
        pass

    class _Request:
        app = _App(relay_state=lambda: {"configured": False},
                   loop_lag=type("L", (), {"snapshot": staticmethod(lambda: {})})())

    received = time.monotonic() - 0.25
    pong = json.loads(api._control_pong_text(_Request(), {"type": "ping", "t": 5}, received))
    assert pong["t"] == 5 and pong["daemon_ms"] >= 250.0
    bare = json.loads(api._control_pong_text(_Request(), {"type": "ping"}, received))
    assert bare == {"type": "pong"}, "a liveness ping gets the bare answer it always did"
