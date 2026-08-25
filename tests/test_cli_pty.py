"""The dashboard's CLI tab: one raw, unmanaged shell over its own WebSocket.

Sessions are managed agents; this is neither — no name, no harness, no
restore, just a persistent shell on the daemon machine. The tests drive the
``/api/cli/ws`` endpoint against a real PTY child (an interactive Python
REPL, ``python -i``) through the daemon app, so the protocol is exercised
both ways against a genuine console.

The REPL is the sync point, but the *output line*, not the prompt, is what
tests wait on after typing: ConPTY makes readline re-render its prompt line
after each echoed keystroke, so ">>> " is on the wire mid-typing, before
the line has been processed — waiting for it would pass on a redraw. An
expression's printed result (``42``) cannot appear before the line is
executed, so the result is the wait; the prompt is only waited on when
nothing has been typed yet.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from aiohttp import WSMsgType, test_utils

from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.clipty import ShellPty, _config_argv, default_shell_argv
from claude_launcher.daemon.manager import SessionManager

# A quiet interactive REPL: -q drops the banner, -u flushes output eagerly,
# so ">>> " appears as soon as the child is ready to take input.
REPL = [sys.executable, "-i", "-u", "-q"]

BEARER = {"Authorization": "Bearer sekrit"}


async def _next_json(sock, timeout=30.0):
    """The next text frame, parsed — passing over any binary frames in the
    way. The two lanes are mixed on this socket (raw shell output — cursor
    redraws, terminal queries — interleaves with control frames), so "the
    next control message" has to skip whatever data is in front of it."""
    while True:
        msg = await asyncio.wait_for(sock.receive(), timeout)
        if msg.type == WSMsgType.BINARY:
            continue
        assert msg.type == WSMsgType.TEXT, f"expected a text frame, got {msg.type}"
        return json.loads(msg.data)


async def _stream_until(sock, needle: bytes, timeout=30.0):
    """Accumulate binary output until ``needle`` is inside it.

    Text frames (init, exit, ...) are skipped — callers that need them read
    them before waiting on output. Returns everything seen.
    """
    buf = bytearray()
    while needle not in buf:
        msg = await asyncio.wait_for(sock.receive(), timeout)
        if msg.type == WSMsgType.BINARY:
            buf += msg.data
    return bytes(buf)


async def _swallow(sock, seconds: float):
    """No *control* frame should arrive for a moment. Stray binary output
    is passed over — the point is that keystrokes stop reaching the shell,
    not that the pty goes byte-silent."""
    while True:
        try:
            msg = await asyncio.wait_for(sock.receive(), seconds)
        except asyncio.TimeoutError:
            return  # quiet: nothing at all arrived
        if msg.type == WSMsgType.BINARY:
            continue  # data noise; keep listening
        raise AssertionError(f"expected silence, got a {msg.type} frame")


def test_default_shell_argv():
    argv = default_shell_argv()
    assert argv and isinstance(argv[0], str) and argv[0]
    assert _config_argv(None) is None
    assert _config_argv("powershell") == ["powershell"]
    assert _config_argv(["pwsh", "-NoLogo"]) == ["pwsh", "-NoLogo"]


def test_cli_terminal_roundtrip_and_persistence(home, tmp_path):
    """Typed bytes reach the shell, output streams back, and the shell —
    and its output ring — survive the viewer leaving."""

    async def run():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), shell=shell)
        client = test_utils.TestClient(test_utils.TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/api/cli/ws", headers=BEARER)
            init = await _next_json(ws)
            assert init["type"] == "init"
            assert init["exited"] is False
            assert init["cols"] and init["rows"]

            await _stream_until(ws, b">>>")
            await ws.send_bytes(b"print(41 + 1)\r")
            await _stream_until(ws, b"42\r\n")   # the result, not a prompt redraw
            await ws.send_bytes(b"print(42 + 1)\r")
            await _stream_until(ws, b"43\r\n")
            pid = init["pid"]
            await ws.close()

            # A new viewer: the same shell is still running underneath, and
            # the ring replays what it printed while nobody was watching.
            ws2 = await client.ws_connect("/api/cli/ws", headers=BEARER)
            init2 = await _next_json(ws2)
            assert init2["type"] == "init" and init2["exited"] is False
            assert init2["pid"] == pid  # same child, not a fresh one
            await _stream_until(ws2, b"42\r\n")  # replayed from the ring
            await ws2.send_bytes(b"print(43 + 1)\r")
            await _stream_until(ws2, b"44\r\n")
            await ws2.close()
        finally:
            await client.close()
        assert shell.exited  # the app's shutdown killed the child

    asyncio.run(run())


def test_cli_resize_reaches_every_viewer(home, tmp_path):
    """One viewer's resize control is applied to the pty and announced to
    every other viewer."""

    async def run():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path), cols=80, rows=24)
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), shell=shell)
        client = test_utils.TestClient(test_utils.TestServer(app))
        await client.start_server()
        try:
            ws1 = await client.ws_connect("/api/cli/ws", headers=BEARER)
            await _next_json(ws1)
            ws2 = await client.ws_connect("/api/cli/ws", headers=BEARER)
            init2 = await _next_json(ws2)
            assert (init2["cols"], init2["rows"]) == (80, 24)
            await _stream_until(ws1, b">>>")

            await ws1.send_str(json.dumps({"type": "resize", "cols": 90, "rows": 25}))
            awaited = await _next_json(ws2)
            assert awaited["type"] == "resize"
            assert (awaited["cols"], awaited["rows"]) == (90, 25)
            assert (shell.cols, shell.rows) == (90, 25)
            await ws1.close()
            await ws2.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_cli_exit_then_restart(home, tmp_path):
    """An exited shell drops keystrokes and says so; the restart control
    revives it as a fresh child (new pid), while the socket stays up."""

    async def run():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), shell=shell)
        client = test_utils.TestClient(test_utils.TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/api/cli/ws", headers=BEARER)
            init = await _next_json(ws)
            await _stream_until(ws, b">>>")

            await ws.send_bytes(b"exit()\r")
            exit_frame = await _next_json(ws)
            assert exit_frame["type"] == "exit"
            assert shell.exited

            # Keystrokes after exit go nowhere — the daemon drops them and
            # the socket stays open for the restart control.
            await ws.send_bytes(b"print(1)\r")
            await _swallow(ws, 1.5)
            assert shell.exited

            # A viewer attaching to the dead shell is told it is dead.
            ws2 = await client.ws_connect("/api/cli/ws", headers=BEARER)
            init2 = await _next_json(ws2)
            assert init2["type"] == "init" and init2["exited"] is True
            await _stream_until(ws2, b">>>")   # the ring still replays
            await ws2.close()

            # The restart control brings a fresh child up under the same
            # socket, announced with a new pid.
            await ws.send_str(json.dumps({"type": "restart"}))
            revived = await _next_json(ws)
            assert revived["type"] == "init" and revived["exited"] is False
            assert revived["pid"] != init["pid"]
            await _stream_until(ws, b">>>")
            await ws.send_bytes(b"print(2 + 2)\r")
            await _stream_until(ws, b"4\r\n")
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_cli_shell_never_auto_revives(home, tmp_path):
    """start_once only ever starts a shell that never existed; an exited
    shell stays exited until a viewer asks for restart."""

    async def run():
        shell = ShellPty(argv=REPL, cwd=str(tmp_path))
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), shell=shell)
        client = test_utils.TestClient(test_utils.TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/api/cli/ws", headers=BEARER)
            await _next_json(ws)
            await _stream_until(ws, b">>>")
            await ws.send_bytes(b"exit()\r")
            assert (await _next_json(ws))["type"] == "exit"
            await ws.close()

            # A fresh viewer after the exit: still the dead shell.
            assert shell.start_once() is False
            ws2 = await client.ws_connect("/api/cli/ws", headers=BEARER)
            assert (await _next_json(ws2))["exited"] is True
            await ws2.close()

            # A brand-new daemon incarnation would start one fresh.
            shell2 = ShellPty(argv=REPL, cwd=str(tmp_path))
            assert shell2.start_once() is True
            assert not shell2.exited
            await shell2.shutdown()
        finally:
            await client.close()

    asyncio.run(run())
