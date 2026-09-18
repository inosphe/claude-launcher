"""Two terminals, one connection.

A WebSocket costs a browser one of its per-server connections -- six in
Firefox -- and the dashboard opened one per session it was looking at, so
the count grew as a person moved between sessions and the next upgrade
waited in the browser's own connection queue, unsent and therefore invisible
here (claunch-gh4f). ``daemon/channel.py`` carries the terminals on the
control socket instead: two bytes of channel id in front of each binary
payload, ``"ch": N`` inside each text frame.

What these pin is that the multiplexing did not become a second terminal
protocol. The attachment code is the same function ``/api/sessions/{n}/ws``
runs, so what has to hold is the routing around it: frames reach the channel
that asked for them, two channels on one socket do not read each other's,
a channel ends without ending the socket, and the socket ending ends every
channel on it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import channel
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

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


async def _serve(mgr) -> TestClient:
    app = build_app(mgr, "sekrit", started_at=time.monotonic())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _live(mgr, name, cwd):
    mgr.create(SessionDef(name=name, harness="py", cwd=str(cwd)))
    session = mgr.get(name)
    await session.wait_for("idle", timeout=10.0, threshold=0.5)
    return session


async def _until(ws, want, ch=None, timeout=10.0):
    """The next text frame of type ``want`` (on channel ``ch`` if given).

    Binary frames and the terminal's other chatter go past: what each test
    is waiting for is one frame, and the traffic in front of it is whatever
    the session happened to be doing.
    """

    async def pump():
        while True:
            msg = await ws.receive()
            if msg.type is not WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            if frame.get("type") != want:
                continue
            if ch is not None and frame.get("ch") != ch:
                continue
            return frame

    return await asyncio.wait_for(pump(), timeout)


def test_a_terminal_arrives_on_the_channel_that_asked_for_it(home, tmp_path):
    """The attach, and the terminal's own first frame behind it, tagged."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            await _live(mgr, "s1", tmp_path)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "attach", "ch": 3, "session": "s1"})
                assert (await _until(ws, "attached", ch=3))["session"] == "s1"
                init = await _until(ws, "init", ch=3)
                assert init["exited"] is False
                assert init["ch"] == 3, "every terminal frame says whose it is"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_two_sessions_share_the_socket_without_sharing_frames(home, tmp_path):
    """The point of the change, and the thing that would be worst to get
    wrong: one connection carrying two terminals, with each one's bytes
    reaching only its own."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            await _live(mgr, "s1", tmp_path)
            await _live(mgr, "s2", tmp_path)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                # One at a time: _until drops the frames it is not waiting
                # for, so overlapping two attaches would let one channel's
                # answer be discarded while the other is being waited on.
                for ch, name in ((1, "s1"), (2, "s2")):
                    await ws.send_json({"type": "attach", "ch": ch, "session": name})
                    await _until(ws, "attached", ch=ch)
                    await _until(ws, "init", ch=ch)

                # Type into s1 only. What comes back is that child's echo,
                # and it has to arrive on channel 1. Re-sent while waiting:
                # a keystroke that lands while the PTY is still starting is
                # dropped there, and the wait would then be measuring that
                # race rather than the routing.
                async def echo():
                    while True:
                        await ws.send_bytes(channel.pack(1, b"hello\r"))
                        deadline = asyncio.get_running_loop().time() + 2.0
                        while asyncio.get_running_loop().time() < deadline:
                            try:
                                msg = await asyncio.wait_for(ws.receive(), 2.0)
                            except asyncio.TimeoutError:
                                break
                            if msg.type is not WSMsgType.BINARY:
                                continue
                            ch, payload = channel.unpack(msg.data)
                            assert ch in (1, 2)
                            if b"echo:hello" in payload:
                                return ch

                assert await asyncio.wait_for(echo(), 20) == 1
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_channel_ends_without_ending_the_socket(home, tmp_path):
    """A person leaving a session must not cost the page its reads: the
    whole arrangement is that the socket outlives every channel on it."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            await _live(mgr, "s1", tmp_path)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "attach", "ch": 5, "session": "s1"})
                await _until(ws, "attached", ch=5)
                await ws.send_json({"type": "detach", "ch": 5})
                assert (await _until(ws, "detached", ch=5))["ch"] == 5

                # Still ours to read on.
                await ws.send_json({"type": "read", "id": 9, "paths": ["/api/health"]})
                assert (await _until(ws, "read_result"))["id"] == 9
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_an_attach_that_cannot_be_served_is_answered(home, tmp_path):
    """Silence here is a terminal that sits on "connecting" indefinitely,
    which is the shape of the failure this change is for."""

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "attach", "ch": 1, "session": "nope"})
                bad = await _until(ws, "attach_error", ch=1)
                assert "nope" in bad["error"]
        finally:
            await client.close()

    asyncio.run(run())


def test_the_socket_will_not_carry_an_unbounded_number_of_channels(home, tmp_path):
    """A ceiling on this side, so a client cannot make one connection hold
    an arbitrary number of attachments."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            await _live(mgr, "s1", tmp_path)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                for ch in range(channel.MAX_CHANNELS):
                    await ws.send_json({"type": "attach", "ch": ch, "session": "s1"})
                    await _until(ws, "attached", ch=ch)
                await ws.send_json({
                    "type": "attach", "ch": channel.MAX_CHANNELS, "session": "s1",
                })
                refused = await _until(ws, "attach_error", ch=channel.MAX_CHANNELS)
                assert str(channel.MAX_CHANNELS) in refused["error"]
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_carrier_going_takes_every_channel_with_it(home, tmp_path):
    """The attachments must not outlive the socket they were riding: a
    viewer left registered on a session nobody is watching is a leak that
    grows with every reconnect."""
    _harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            session = await _live(mgr, "s1", tmp_path)
            before = session.viewers()
            ws = await client.ws_connect("/api/control/ws", headers=BEARER)
            await _until(ws, "init")
            await ws.send_json({"type": "attach", "ch": 1, "session": "s1"})
            await _until(ws, "init", ch=1)
            assert session.viewers() == before + 1
            await ws.close()
            for _ in range(100):
                if session.viewers() == before:
                    break
                await asyncio.sleep(0.05)
            assert session.viewers() == before, "a viewer outlived its socket"
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("data", [b"", b"\x00"])
def test_a_frame_too_short_to_carry_a_header_is_dropped(data):
    """Guessing would write one terminal's keystrokes into another's PTY."""
    assert channel.unpack(data) == (None, b"")
