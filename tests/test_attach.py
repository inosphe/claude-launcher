"""The terminal attach bridge (``claunch attach`` / ``new-session --attach``).

The raw-terminal layer needs a real console, so tests drive the async bridge
directly with the stdin/stdout seams monkeypatched — the WebSocket protocol,
detach handling and keep-running-after-detach semantics are exercised against
a real PTY child through the daemon app.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
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

        def GetConsoleCP(self):
            return 65001

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


def test_windows_stdin_transcodes_console_code_page_to_utf8(monkeypatch):
    """ReadFile hands back the console *input code page* (949 on a Korean
    Windows), not UTF-8 — forwarding those bytes verbatim painted every
    Hangul syllable as two replacement characters in the session. The bridge
    transcodes to the PTY's UTF-8, and a syllable split across two reads
    still comes out whole."""
    payloads = ["웹 데몬".encode("cp949")]
    split = "어\x1b[A".encode("cp949")
    payloads += [split[:1], split[1:]]

    class FakeKernel32:
        def GetStdHandle(self, value):
            return object()

        def GetConsoleCP(self):
            return 949

        def ReadFile(self, handle, buf, size, count, overlapped):
            payload = payloads.pop(0)
            ctypes.memmove(buf, payload, len(payload))
            count._obj.value = len(payload)
            return 1

    monkeypatch.setattr(attach_mod.sys, "platform", "win32")
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {
        "kernel32": FakeKernel32(),
    })(), raising=False)

    assert attach_mod._read_stdin_windows() == "웹 데몬".encode("utf-8")
    # The lead byte alone is held back rather than mangled, and the read
    # goes around again for the rest instead of returning b"": that value
    # is this function's EOF, and the caller detaches on it.
    assert attach_mod._read_stdin_windows() == "어\x1b[A".encode("utf-8")
    assert payloads == []


def _fake_console(monkeypatch, payloads, codepage):
    """A console whose ReadFile hands back ``payloads`` one call at a time,
    then reports end of input the way conhost does (ok, zero bytes)."""
    queue = list(payloads)

    class FakeKernel32:
        calls = 0

        def GetStdHandle(self, value):
            return object()

        def GetConsoleCP(self):
            return codepage

        def ReadFile(self, handle, buf, size, count, overlapped):
            FakeKernel32.calls += 1
            if not queue:
                count._obj.value = 0
                return 1
            payload = queue.pop(0)
            ctypes.memmove(buf, payload, len(payload))
            count._obj.value = len(payload)
            return 1

    monkeypatch.setattr(attach_mod.sys, "platform", "win32")
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {
        "kernel32": FakeKernel32(),
    })(), raising=False)
    return FakeKernel32


def test_a_held_fragment_is_not_reported_as_end_of_input(monkeypatch):
    """b"" out of this function means one thing to its callers: stdin is
    closed. ``pump_stdin`` puts None on the queue and returns, ``send_pump``
    closes the socket and records a detach. A transcode holding the leading
    bytes of a split character must therefore not produce b"" -- the
    keyboard is still there. The read goes around again for the rest.

    This is reachable on every Korean console since the transcode stopped
    passing code page 65001 through untouched: before that, a UTF-8 console
    never held anything back."""
    # The chunk that decodes to nothing at all is the one that used to
    # come back as b"": one lead byte, the other two still to come.
    raw = "한글".encode("utf-8")
    _fake_console(monkeypatch, [raw[:1], raw[1:]], attach_mod._CP_UTF8)
    assert attach_mod._read_stdin_windows() == raw


def test_a_closed_console_is_still_end_of_input(monkeypatch):
    """The other side of the same rule: a real EOF must still read as one,
    or the attach never ends."""
    _fake_console(monkeypatch, [], attach_mod._CP_UTF8)
    assert attach_mod._read_stdin_windows() == b""


def test_the_read_does_not_spin_when_there_is_something_to_return(monkeypatch):
    """One ReadFile per call in the ordinary case: the loop exists for the
    held fragment alone."""
    k32 = _fake_console(monkeypatch, ["ls\r".encode("utf-8")], attach_mod._CP_UTF8)
    k32.calls = 0
    assert attach_mod._read_stdin_windows() == b"ls\r"
    assert k32.calls == 1


def test_console_input_unknown_code_page_passes_through(monkeypatch):
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    raw = bytes([0xFF, 0xFE])
    assert attach_mod._console_input_to_utf8(raw, 424242) == raw


def test_console_input_utf8_code_page_repairs_a_split_syllable(monkeypatch):
    """A UTF-8 console passed its bytes through untouched, so ReadFile's chunk
    boundary reached the daemon wherever it fell, which is not a character
    boundary. The daemon then held the lead bytes of a split syllable while it
    waited for the rest, and whatever was written to that PTY in between -- a
    mesh delivery, another viewer -- was read as the end of that character
    (``claunch-pty-shared-decoder-across-writers-o3cy4``). The boundary is
    repaired here, where the code page is known."""
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    raw = "한글".encode("utf-8")
    first = attach_mod._console_input_to_utf8(raw[:4], attach_mod._CP_UTF8)
    second = attach_mod._console_input_to_utf8(raw[4:], attach_mod._CP_UTF8)
    assert first == "한".encode("utf-8")  # the lead byte of 글 is held back
    assert second == "글".encode("utf-8")
    assert first + second == raw


def test_console_input_utf8_code_page_leaves_a_whole_chunk_alone(monkeypatch):
    """The ordinary case: a chunk that ends on a character boundary comes out
    as the bytes that went in, VT sequences included."""
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    raw = "한글".encode("utf-8") + b"\x1b[A\r"
    assert attach_mod._console_input_to_utf8(raw, attach_mod._CP_UTF8) == raw


def test_console_input_changing_code_page_starts_a_fresh_decoder(monkeypatch):
    """_RawTerminal moves the console from 949 to 65001 for the attach. The
    decoder is kept per code page, so a byte left over from the old one is
    dropped rather than fed to the new one."""
    monkeypatch.setattr(attach_mod, "_console_decoder", None)
    attach_mod._console_input_to_utf8("한".encode("cp949")[:1], 949)
    out = attach_mod._console_input_to_utf8("글".encode("utf-8"), attach_mod._CP_UTF8)
    assert out == "글".encode("utf-8")


def test_raw_terminal_switches_console_input_to_utf8_and_restores(monkeypatch):
    """On a console whose input code page is not UTF-8, the attach moves it
    to 65001 (so emoji and other out-of-code-page characters survive
    ReadFile) and puts the original back on exit — but only on consoles new
    enough that ReadFile under 65001 works at all."""
    calls = []

    class FakeKernel32:
        cp = 949

        def GetStdHandle(self, value):
            return value

        def GetConsoleMode(self, handle, out):
            out._obj.value = 0x1F7
            return 1

        def SetConsoleMode(self, handle, mode):
            return 1

        def GetConsoleCP(self):
            return self.cp

        def SetConsoleCP(self, cp):
            calls.append(cp)
            self.cp = cp
            return 1

    k32 = FakeKernel32()
    monkeypatch.setattr(attach_mod.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {"kernel32": k32})(),
                        raising=False)
    monkeypatch.setattr(attach_mod, "_utf8_console_input_supported", lambda: True)

    with attach_mod._RawTerminal() as term:
        assert k32.cp == 65001
        assert term.original_codepage == 949
        assert term.codepage_switched
    assert calls == [65001, 949]
    assert k32.cp == 949

    # Legacy conhost: the code page is left alone (transcode covers it).
    monkeypatch.setattr(attach_mod, "_utf8_console_input_supported", lambda: False)
    calls.clear()
    with attach_mod._RawTerminal() as term:
        assert not term.codepage_switched
    assert calls == []

    # Already UTF-8: nothing to switch, nothing to say.
    k32.cp = 65001
    monkeypatch.setattr(attach_mod, "_utf8_console_input_supported", lambda: True)
    with attach_mod._RawTerminal() as term:
        assert not term.codepage_switched
    assert calls == []


def test_codepage_note_only_when_console_was_not_utf8():
    assert attach_mod.codepage_note(None, False) is None
    assert attach_mod.codepage_note(65001, False) is None
    switched = attach_mod.codepage_note(949, True)
    assert "949" in switched and "switched" in switched and "chcp 65001" in switched
    kept = attach_mod.codepage_note(949, False)
    assert "transcoded" in kept and "'?'" in kept


def test_attach_prints_codepage_note_after_detach(monkeypatch, capsys):
    """The note lands after detach: anything printed before raw mode is
    repainted over by the session, so it is the one readable moment."""
    class Term:
        original_codepage = 949
        codepage_switched = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Client:
        base_url = "http://h"
        token = "t"

        def get(self, path):
            return {"status": "idle", "cwd": "", "role": ""}

    monkeypatch.setattr(attach_mod.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(attach_mod.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(attach_mod, "_RawTerminal", Term)
    monkeypatch.setattr(attach_mod, "_write_text", lambda text: None)
    monkeypatch.setattr(attach_mod.herdr, "rename_pane", lambda label: False)

    async def detached(*a, **k):
        return {"reason": "detach"}

    monkeypatch.setattr(attach_mod, "_attach_async", detached)
    assert attach_mod.attach(Client(), "s1") == 0
    err = capsys.readouterr().err
    assert "console input code page was 949" in err


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


def test_attach_shows_its_console_notice_over_the_session(home, tmp_path, monkeypatch):
    """What attach learns about the local console (its code page) is handed
    to the daemon as a viewer-local notice and comes back drawn over row 1
    of the mirrored screen -- the one place a line survives the session's
    repaint. Nothing of it reaches the PTY."""
    _register_py_harness()
    from aiohttp.test_utils import TestClient, TestServer

    chunks = []
    drawn = threading.Event()

    def fake_write(text):
        chunks.append(text)
        if "code page was 949" in "".join(chunks):
            drawn.set()

    reads = {"n": 0}

    def fake_read():
        reads["n"] += 1
        if reads["n"] == 1:
            assert drawn.wait(15), "the notice never appeared in the byte stream"
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
            session = mgr.create(SessionDef(name="att4", harness="py", cwd=str(tmp_path)))
            await _wait_screen(session, "READY")
            base = str(client.make_url("")).rstrip("/")
            outcome = await asyncio.wait_for(
                attach_mod._attach_async(
                    base, "sekrit", "att4", notice=attach_mod.codepage_note(949, True)
                ),
                timeout=30,
            )
            assert outcome["reason"] == "detach"
            joined = "".join(chunks)
            # Drawn on row 1 inside a cursor save/restore, in the warn style.
            assert "\x1b7\x1b[1;1H\x1b[0;1;30;43m" in joined
            assert "code page was 949" in joined
            # ...and never typed into the session.
            await session.screen_synced()
            assert "code page" not in "\n".join(session.capture())
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

    async def fake_attach(base_url, token, name, notice=None):
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

    async def fake_attach(base_url, token, name, notice=None):
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


def test_split_focus_events_reports_both_directions():
    assert attach_mod.split_focus_events(b"\x1b[Iabc") == (b"abc", True, False)
    assert attach_mod.split_focus_events(b"a\x1b[Ob") == (b"ab", False, True)
    assert attach_mod.split_focus_events(b"\x1b[O\x1b[I") == (b"", True, True)
    assert attach_mod.split_focus_events(b"abc") == (b"abc", False, False)


def test_focus_in_sends_resize_focus_and_repaint_focus_out_unfocuses():
    """An attached terminal must be a *focused* viewer to the daemon (normal
    child priority, foreground rendering) — the web terminal says so with a
    ``focus`` frame, and so must attach (claunch-wpd0)."""
    size = os.terminal_size((120, 40))
    frames = [json.loads(f) for f in attach_mod.focus_control_frames(True, False, size)]
    assert frames == [
        {"type": "resize", "cols": 120, "rows": 40},
        {"type": "focus", "focused": True},
        {"type": "repaint"},
    ]
    frames = [json.loads(f) for f in attach_mod.focus_control_frames(False, True, size)]
    assert frames == [{"type": "focus", "focused": False}]
    assert attach_mod.focus_control_frames(False, False, size) == []
