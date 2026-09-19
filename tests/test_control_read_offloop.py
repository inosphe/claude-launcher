"""A read must not stop the socket from carrying anything else.

The control socket answers the page's reads (claunch-riq5) and, since
claunch-gh4f, also carries every terminal the page is looking at. Both ran
through one ``async for msg in ws`` loop, and a read was awaited inside it,
so for as long as a read took nothing else on the socket was read: no
keystroke reached a PTY, no resize or repaint got through, and the terminal
painted once and then sat there.

The duration is not small. Measured against the live daemon on 2026-09-20,
``/api/mesh`` answered 1.7MB in 3.3s and ``/api/sessions`` 2.7MB in 0.7s,
against a dashboard tick every 5s.

These pin the fix: a read runs on a task of its own, the loop keeps reading
while it does, and there is a ceiling on how many one socket will serve at
once.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import api as api_mod
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

#: Long enough that a read holding the loop would be unmistakable, short
#: enough that the test does not spend it when the fix works.
SLOW = 3.0


def _harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _slow_reads(monkeypatch, seconds=SLOW):
    async def one(template, path):
        await asyncio.sleep(seconds)
        return {"slow": path}

    monkeypatch.setattr(api_mod, "_batch_one", one)


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
    """The next text frame of type ``want`` (on channel ``ch`` if given)."""

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


def test_a_keystroke_does_not_wait_behind_a_read(home, tmp_path, monkeypatch):
    """The failure this is for: typing into a terminal and nothing arriving
    while the dashboard's tick is being answered on the same socket."""
    _harness()
    _slow_reads(monkeypatch)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            await _live(mgr, "s1", tmp_path)
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "attach", "ch": 1, "session": "s1"})
                await _until(ws, "attached", ch=1)
                await _until(ws, "init", ch=1)

                started = asyncio.get_running_loop().time()
                await ws.send_json({"type": "read", "id": 7, "paths": ["/api/health"]})

                # Re-sent while waiting: a keystroke that lands while the PTY
                # is still starting is dropped there, and the wait would then
                # be measuring that race rather than the blocking.
                async def echo():
                    while True:
                        await ws.send_bytes(channel.pack(1, b"hello\r"))
                        deadline = asyncio.get_running_loop().time() + 0.5
                        while asyncio.get_running_loop().time() < deadline:
                            try:
                                msg = await asyncio.wait_for(ws.receive(), 0.5)
                            except asyncio.TimeoutError:
                                break
                            if msg.type is not WSMsgType.BINARY:
                                continue
                            _, payload = channel.unpack(msg.data)
                            if b"echo:hello" in payload:
                                return asyncio.get_running_loop().time()

                at = await asyncio.wait_for(echo(), SLOW * 3)
                assert at - started < SLOW, (
                    "the keystroke waited for the read to finish"
                )
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_read_is_still_answered(home, tmp_path, monkeypatch):
    """Off the loop, not dropped: the answer still comes back under the id
    that asked for it."""
    _slow_reads(monkeypatch, seconds=0.2)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "read", "id": 3, "paths": ["/api/health"]})
                answer = await _until(ws, "read_result")
                assert answer["id"] == 3
                assert answer["answers"]["/api/health"] == {"slow": "/api/health"}
        finally:
            await client.close()

    asyncio.run(run())


def test_reads_are_answered_as_they_finish(home, tmp_path, monkeypatch):
    """Concurrent, so a slow read cannot hold a fast one behind it. The id
    is what pairs an answer with its question, which is why it exists."""

    async def one(template, path):
        await asyncio.sleep(1.0 if path == "/api/health" else 0.05)
        return {"path": path}

    monkeypatch.setattr(api_mod, "_batch_one", one)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                await ws.send_json({"type": "read", "id": 1, "paths": ["/api/health"]})
                await ws.send_json({"type": "read", "id": 2, "paths": ["/api/daemon"]})
                first = await _until(ws, "read_result")
                assert first["id"] == 2, "the quick read waited for the slow one"
                second = await _until(ws, "read_result", timeout=5.0)
                assert second["id"] == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_one_socket_will_not_hold_unbounded_reads(home, tmp_path, monkeypatch):
    """A ceiling, answered rather than queued: a client that asks past it
    learns so on the read it asked, and the socket keeps working."""
    _slow_reads(monkeypatch)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await _until(ws, "init")
                for i in range(api_mod.CONTROL_READS_IN_FLIGHT):
                    await ws.send_json({
                        "type": "read", "id": i, "paths": ["/api/health"],
                    })
                # The loop has to have taken all of them before the next one
                # can be the one over the line.
                await asyncio.sleep(0.2)
                over = api_mod.CONTROL_READS_IN_FLIGHT
                await ws.send_json({
                    "type": "read", "id": over, "paths": ["/api/health"],
                })
                refused = await _until(ws, "read_result", timeout=SLOW / 2)
                assert refused["id"] == over
                assert "too many reads in flight" in refused["errors"][""]
        finally:
            await client.close()

    asyncio.run(run())
