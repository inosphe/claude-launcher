"""The terminal attach bridge (``claunch attach`` / ``new-session --attach``).

The raw-terminal layer needs a real console, so tests drive the async bridge
directly with the stdin/stdout seams monkeypatched — the WebSocket protocol,
detach handling and keep-running-after-detach semantics are exercised against
a real PTY child through the daemon app.
"""

from __future__ import annotations

import asyncio
import threading
import time

from claude_launcher import attach as attach_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

from test_daemon_e2e import _register_py_harness, _wait_screen


def test_split_detach():
    assert attach_mod.split_detach(b"abc") == (b"abc", False)
    assert attach_mod.split_detach(b"ab\x1dcd") == (b"ab", True)
    assert attach_mod.split_detach(b"\x1d") == (b"", True)
    assert attach_mod.split_detach(b"") == (b"", False)


def test_strip_focus_events():
    assert attach_mod.strip_focus_events(b"abc") == (b"abc", False)
    assert attach_mod.strip_focus_events(b"\x1b[Iabc") == (b"abc", True)
    assert attach_mod.strip_focus_events(b"a\x1b[Ob") == (b"ab", False)
    assert attach_mod.strip_focus_events(b"\x1b[O\x1b[I") == (b"", True)
    # a bare ESC (real keypress) must pass through untouched
    assert attach_mod.strip_focus_events(b"\x1b") == (b"\x1b", False)


def test_ws_url():
    assert (
        attach_mod.ws_url("http://127.0.0.1:8377", "s0")
        == "ws://127.0.0.1:8377/api/sessions/s0/ws"
    )
    assert (
        attach_mod.ws_url("https://host:1/", "a b")
        == "wss://host:1/api/sessions/a b/ws"
    )


def test_attach_bridge_roundtrip_and_detach(home, tmp_path, monkeypatch):
    """Typed bytes reach the PTY, output streams back, Ctrl+] detaches — and
    the session must survive the detach (that is the point of attach)."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    chunks = []
    ready = threading.Event()
    echoed = threading.Event()
    repainted = threading.Event()

    def fake_write(text):
        chunks.append(text)
        joined = "".join(chunks)
        if "READY" in joined:
            ready.set()
        if "echo:hello" in joined:
            echoed.set()
        if joined.count("\x1b[2J\x1b[H") >= 2:  # initial seed + requested one
            repainted.set()

    reads = {"n": 0}

    def fake_read():
        reads["n"] += 1
        if reads["n"] == 1:
            assert ready.wait(15), "repaint never showed READY"
            return b"hello\r"
        if reads["n"] == 2:
            assert echoed.wait(15), "echo never came back over the socket"
            return b"\x1b[I"  # focus regained -> resize re-assert + repaint
        if reads["n"] == 3:
            assert repainted.wait(15), "focus-in never triggered a repaint"
            return b"\x1d"  # detach
        return b""

    monkeypatch.setattr(attach_mod, "_write_text", fake_write)
    monkeypatch.setattr(attach_mod, "_read_stdin", fake_read)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = mgr.create(SessionDef(name="att1", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            base = str(client.make_url("")).rstrip("/")
            outcome = await asyncio.wait_for(
                attach_mod._attach_async(base, "sekrit", "att1"), timeout=30
            )
            assert outcome["reason"] == "detach"
            assert echoed.is_set()

            # detach must leave the session running and responsive
            assert not session.exited
            await session.send_keys(["again", "Enter"])
            await _wait_screen(session, "echo:again")
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_attach_reports_session_exit(home, tmp_path, monkeypatch):
    """When the child dies while attached, the bridge surfaces the exit."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    ready = threading.Event()

    def fake_write(text):
        if "READY" in text:
            ready.set()

    reads = {"n": 0}

    def fake_read():
        reads["n"] += 1
        if reads["n"] == 1:
            assert ready.wait(15)
            return b"quit\r"
        time.sleep(30)  # never type again; the exit frame must end the attach
        return b""

    monkeypatch.setattr(attach_mod, "_write_text", fake_write)
    monkeypatch.setattr(attach_mod, "_read_stdin", fake_read)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = mgr.create(SessionDef(name="att2", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            base = str(client.make_url("")).rstrip("/")
            outcome = await asyncio.wait_for(
                attach_mod._attach_async(base, "sekrit", "att2"), timeout=30
            )
            assert outcome["reason"] == "exit"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
