"""Host text history is bounded, authenticated and never sends terminal input."""
import asyncio
import sys
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import clipboard
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager


def test_history_deduplicates_bounds_and_recovers():
    async def run():
        current = ["한글\ntext"]
        history = clipboard.History(lambda: current[0])
        await history.sample()
        await history.sample()
        assert len(history.items) == 1
        assert history.items[0]["text"] == current[0]
        for i in range(40):
            current[0] = str(i)
            await history.sample()
        assert len(history.items) == 30
        current[0] = "20"
        await history.sample()
        assert history.items[0]["text"] == "20"
        assert len(history.items) == 30
        current[0] = "x" * (clipboard.MAX_TEXT + 1)
        await history.sample()
        assert history.error
        assert history.items[0]["text"] == "20"
        current[0] = ""
        await history.sample()
        assert history.error is None
        assert len(history.items) == 30
        def busy():
            raise OSError("busy")
        history.reader = busy
        await history.sample()
        assert history.error == "busy"
        assert len(history.items) == 30
    asyncio.run(run())


def test_authenticated_history_delete_and_clear(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "secret", started_at=time.monotonic())
        history = app["clipboard"]
        history.reader = lambda: "<script>한글</script>\nsecond line"
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/clipboard")).status == 401
            assert (await client.delete("/api/clipboard")).status == 401
            headers = {"Authorization": "Bearer secret"}
            response = await client.get("/api/clipboard", headers=headers)
            assert response.headers["Cache-Control"] == "no-store"
            doc = await response.json()
            assert doc["items"][0]["text"] == history.reader()
            item_id = doc["items"][0]["id"]
            assert (await client.delete(f"/api/clipboard/{item_id}", headers=headers)).status == 200
            # Deletion must not re-add the unchanged system clipboard.
            assert (await (await client.get("/api/clipboard", headers=headers)).json())["items"] == []
            history.reader = lambda: "next"
            await history.sample()
            assert (await client.delete("/api/clipboard", headers=headers)).status == 200
            assert (await (await client.get("/api/clipboard", headers=headers)).json())["items"] == []
        await mgr.shutdown_all()
    asyncio.run(run())


def test_windows_reads_are_one_at_a_time_across_apps(monkeypatch):
    # Two apps in one process each sample on their own thread. OpenClipboard
    # with no window belongs to the process, so a second open succeeds while
    # the first still reads, and its close frees that data: 0xc0000374
    # (claunch-lxwwf). A fake user32/kernel32 counts the readers inside.
    pytest.importorskip("ctypes.wintypes")
    import threading

    state = {"inside": 0, "most": 0}
    guard = threading.Lock()
    text = "x".encode("utf-16-le") + b"\0\0"
    buf = __import__("ctypes").create_string_buffer(text, len(text))

    class Fn:
        def __init__(self, call):
            self.call = call
            self.argtypes = self.restype = None

        def __call__(self, *args):
            return self.call(*args)

    def open_clipboard(_hwnd):
        with guard:
            state["inside"] += 1
            state["most"] = max(state["most"], state["inside"])
        time.sleep(0.01)
        return True

    def close_clipboard():
        with guard:
            state["inside"] -= 1
        return True

    calls = {
        "OpenClipboard": open_clipboard,
        "CloseClipboard": close_clipboard,
        "GetClipboardData": lambda _fmt: 1,
        "GlobalSize": lambda _h: len(text),
        "GlobalLock": lambda _h: __import__("ctypes").addressof(buf),
        "GlobalUnlock": lambda _h: True,
    }

    class Dll:
        def __init__(self, *_args, **_kw):
            for name, call in calls.items():
                setattr(self, name, Fn(call))

    monkeypatch.setattr(clipboard.ctypes, "WinDLL", Dll, raising=False)
    got = []
    threads = [threading.Thread(target=lambda: got.append(clipboard._read_windows()))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == ["x"] * 8
    assert state["most"] == 1


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows reads in-process, outside this guard (claunch-lxwwf)")
def test_a_served_app_reads_no_clipboard_under_the_suite(home, monkeypatch):
    # The history an app samples every second while it is served reaches the
    # Linux route through read_text; conftest has made that route read nothing.
    monkeypatch.setattr(clipboard, "read_command", lambda argv: pytest.fail("ran a tool"))
    monkeypatch.setattr(clipboard, "_wsl_bridge",
                        lambda: pytest.fail("started powershell.exe"))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "secret", started_at=time.monotonic())
        history = app["clipboard"]
        await history.sample()
        assert history.items == [] and history.error is None
        await mgr.shutdown_all()
    asyncio.run(run())
