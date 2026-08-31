"""The terminal attach bridge (``claunch attach`` / ``new-session --attach``).

The raw-terminal layer needs a real console, so tests drive the async bridge
directly with the stdin/stdout seams monkeypatched — the WebSocket protocol,
detach handling and keep-running-after-detach semantics are exercised against
a real PTY child through the daemon app.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time

from claude_launcher import attach as attach_mod
from claude_launcher import herdr as herdr_mod
from claude_launcher.daemon.api import build_app, notify_shutdown
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


def test_windows_stdin_reads_vt_utf8_bytes_without_unicode_roundtrip(monkeypatch):
    """Fast IME commits must reach the PTY byte-for-byte.

    In VT input mode ReadFile exposes UTF-8 directly. A ReadConsoleW roundtrip
    can lose a commit while the console input buffer is being drained.
    """
    class FakeKernel32:
        def GetStdHandle(self, value):
            assert value == -10
            return object()

        def ReadFile(self, handle, buf, size, count, overlapped):
            payload = "한글入力🙂".encode("utf-8")
            ctypes.memmove(buf, payload, len(payload))
            count._obj.value = len(payload)
            return 1

    monkeypatch.setattr(attach_mod.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {
        "kernel32": FakeKernel32(),
    })(), raising=False)

    assert attach_mod._read_stdin_windows() == "한글入力🙂".encode("utf-8")


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


def test_attach_reports_daemon_shutdown(home, tmp_path, monkeypatch):
    """A daemon stop/restart announces itself before killing sessions, so the
    bridge ends with reason 'shutdown' (reattach advice), not 'exit'."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    def fake_read():
        time.sleep(30)  # never type; the shutdown frame must end the attach
        return b""

    monkeypatch.setattr(attach_mod, "_write_text", lambda text: None)
    monkeypatch.setattr(attach_mod, "_read_stdin", fake_read)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = mgr.create(SessionDef(name="att3", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")

            base = str(client.make_url("")).rstrip("/")
            task = asyncio.ensure_future(
                attach_mod._attach_async(base, "sekrit", "att3")
            )
            deadline = time.monotonic() + 15
            while not app["websockets"] and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert app["websockets"], "attach never connected"

            # The daemon's shutdown sequence: announce, then terminate. The
            # attach must surface the announcement, not the ensuing exit.
            await notify_shutdown(app)
            await mgr.shutdown_all()
            outcome = await asyncio.wait_for(task, timeout=30)
            assert outcome["reason"] == "shutdown"
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_herdr_agent_state_mapping():
    """The daemon's status in Herdr's vocabulary; nothing guessed."""
    assert attach_mod._herdr_agent_state("busy") == "working"
    assert attach_mod._herdr_agent_state("idle") == "idle"
    assert attach_mod._herdr_agent_state("starting") == "unknown"
    assert attach_mod._herdr_agent_state("exited") == "unknown"
    assert attach_mod._herdr_agent_state(None) == "unknown"
    assert attach_mod._herdr_agent_state("") == "unknown"


def test_attach_reports_agent_to_herdr_and_releases_on_detach(
    home, tmp_path, monkeypatch
):
    """Inside Herdr, the attach tells it the pane mirrors claude — reported on
    entry with the daemon's status, released on the way out like the label."""
    class FakeStream:
        def isatty(self):
            return True

        def write(self, text):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdin", FakeStream())
    monkeypatch.setattr(sys, "stdout", FakeStream())

    calls = []

    class FakeHerdr:
        MIRROR_AGENT_LABEL = herdr_mod.MIRROR_AGENT_LABEL

        def rename_pane(self, label):
            calls.append(("rename_pane", label))
            return True

        def report_agent(self, agent, *, state, message):
            calls.append(("report_agent", agent, state, message))
            return True

        def clear_pane_label(self):
            calls.append(("clear_pane_label",))
            return True

        def release_agent(self, agent, *, pane=None):
            calls.append(("release_agent", agent))
            return True

    monkeypatch.setattr(attach_mod, "herdr", FakeHerdr())
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoopRawTerminal)

    async def fake_attach(base_url, token, name):
        return {"reason": "detach"}

    monkeypatch.setattr(attach_mod, "_attach_async", fake_attach)

    class FakeClient:
        base_url = "http://daemon"
        token = "t"

        def get(self, url):
            return {"status": "busy", "cwd": str(tmp_path), "role": "worker"}

    code = attach_mod.attach(FakeClient(), "att-herdr")
    assert code == 0
    assert calls[0][0] == "rename_pane"
    assert (
        "report_agent", herdr_mod.MIRROR_AGENT_LABEL, "working", "att-herdr"
    ) in calls
    assert calls.count(("release_agent", herdr_mod.MIRROR_AGENT_LABEL)) == 1


def test_attach_reports_unknown_when_daemon_status_is_absent(
    home, tmp_path, monkeypatch
):
    """A status the daemon does not name must not be guessed at."""
    class FakeStream:
        def isatty(self):
            return True

        def write(self, text):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdin", FakeStream())
    monkeypatch.setattr(sys, "stdout", FakeStream())

    states = []

    class FakeHerdr:
        MIRROR_AGENT_LABEL = herdr_mod.MIRROR_AGENT_LABEL

        def rename_pane(self, label):
            return True

        def report_agent(self, agent, *, state, message):
            states.append(state)
            return True

        def clear_pane_label(self):
            return True

        def release_agent(self, agent, *, pane=None):
            return True

    monkeypatch.setattr(attach_mod, "herdr", FakeHerdr())
    monkeypatch.setattr(attach_mod, "_RawTerminal", _NoopRawTerminal)

    async def fake_attach(base_url, token, name):
        return {"reason": "detach"}

    monkeypatch.setattr(attach_mod, "_attach_async", fake_attach)

    class FakeClient:
        base_url = "http://daemon"
        token = "t"

        def get(self, url):
            return {"cwd": str(tmp_path)}  # no "status" key

    code = attach_mod.attach(FakeClient(), "att-nostatus")
    assert code == 0
    assert states == ["unknown"]


class _NoopRawTerminal:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
