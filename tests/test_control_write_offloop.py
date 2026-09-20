"""A blocked write must not stop the shared socket from reading.

The control socket carries every terminal the page is looking at, and the
carrier serialises writes behind one lock so two terminals cannot interleave
a frame. The receive loop wrote through that same lock: the ``pong`` it
answers a ping with, the refusal when too many reads are in flight, and the
``attached`` frame ``attach`` sends before it starts serving. A browser that
is slow to drain its socket lets the send buffer fill, and a terminal's
output write then parks inside the lock waiting for the transport.

Everything the receive loop wanted to write waited on that parked write, and
the loop waited with it: nothing typed in any terminal reached a PTY, and a
newly clicked session never attached, so the page painted from what it had
and sat there. That is the same symptom as the read blocking fixed in
claunch-gh4f, reached through the writes instead.

The rule these pin: the receive loop hands a frame to the carrier's writer
and goes back to reading, while an awaited send still waits for the socket
to take it, because the terminal pump's backpressure is built on that wait.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from claude_launcher import store
from claude_launcher.daemon import channel
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def _harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


class _StuckWS:
    """A socket whose writes park until released, as a full send buffer
    parks them. Nothing is read from it: these tests are about the writes."""

    def __init__(self):
        self.released = asyncio.Event()
        self.sent = []
        self.closed = False
        self.close_code = None

    def exception(self):
        return None

    async def _write(self, data):
        await self.released.wait()
        self.sent.append(data)

    async def send_str(self, data):
        await self._write(data)

    async def send_bytes(self, data):
        await self._write(data)

    async def close(self, *, code=1000, message=b""):
        self.closed = True
        self.close_code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_a_queued_frame_does_not_wait_for_a_parked_write():
    """The failure this is for, at the level it lives: the receive loop's
    own frame, asked for while a terminal's output is stuck."""

    async def run():
        ws = _StuckWS()
        carrier = channel.Carrier(ws, None, None)
        stuck = asyncio.ensure_future(carrier.send_bytes(b"terminal output"))
        await asyncio.sleep(0.05)
        assert not stuck.done(), "the premise: this write parks"

        await asyncio.wait_for(carrier.send_soon(json.dumps({"type": "pong"})), 1.0)

        ws.released.set()
        await asyncio.wait_for(stuck, 2.0)
        await carrier.shutdown()

    asyncio.run(run())


def test_an_awaited_send_still_waits_for_the_socket():
    """The backpressure the terminal pump is built on is not given up: a
    page slow to read must slow the pump feeding it, not fill a queue."""

    async def run():
        ws = _StuckWS()
        carrier = channel.Carrier(ws, None, None)
        out = asyncio.ensure_future(carrier.send_bytes(b"x" * 10))
        await asyncio.sleep(0.05)
        assert not out.done(), "the write returned before the socket took it"
        ws.released.set()
        await asyncio.wait_for(out, 2.0)
        assert ws.sent == [b"x" * 10]
        await carrier.shutdown()

    asyncio.run(run())


def test_frames_leave_in_the_order_they_were_asked_for():
    """One writer, so a queued frame and an awaited one keep their order --
    the property the single lock was there for."""

    async def run():
        ws = _StuckWS()
        carrier = channel.Carrier(ws, None, None)
        await carrier.send_soon(json.dumps({"type": "first"}))
        second = asyncio.ensure_future(carrier.send_bytes(b"second"))
        await asyncio.sleep(0.05)
        ws.released.set()
        await asyncio.wait_for(second, 2.0)
        assert json.loads(ws.sent[0])["type"] == "first"
        assert ws.sent[1] == b"second"
        await carrier.shutdown()

    asyncio.run(run())


def test_a_session_still_attaches_while_a_write_is_parked(home, tmp_path):
    """What the person sees: a session clicked while another terminal's
    output is stuck used to attach to nothing at all, because ``attach``
    awaited the ``attached`` frame before it started serving."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            session = mgr.get("s1")
            await session.wait_for("idle", timeout=10.0, threshold=0.5)

            ws = _StuckWS()
            carrier = channel.Carrier(ws, app, None)
            stuck = asyncio.ensure_future(carrier.send_bytes(b"output"))
            await asyncio.sleep(0.05)
            assert not stuck.done()

            await asyncio.wait_for(
                carrier.attach({"ch": 1, "session": "s1"}), 2.0
            )
            # Serving, not merely accepted: the attachment is what carries
            # the keystrokes, and a channel with no server task behind it
            # takes them and drops them.
            assert 1 in carrier.channels
            carrier.channels[1].deliver_bytes(b"hello\r")

            ws.released.set()
            await asyncio.wait_for(stuck, 2.0)
            await carrier.shutdown()
        finally:
            await mgr.shutdown_all()

    asyncio.run(run())
