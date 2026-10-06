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


def test_a_browser_copy_joins_the_history_without_touching_the_host(home):
    # The web terminal posts what an OSC 52 write put on the browser's
    # clipboard (claunch-fqw4k): it lands first in the history the menu reads,
    # and the host clipboard it never came from is not re-recorded over it.
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "secret", started_at=time.monotonic())
        history = app["clipboard"]
        history.reader = lambda: "on the host"
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/clipboard", json={"text": "x"})).status == 401
            headers = {"Authorization": "Bearer secret"}
            await history.sample()
            response = await client.post("/api/clipboard", json={"text": "한글 <copy>\nline two"},
                                         headers=headers)
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["item"]["text"] == "한글 <copy>\nline two"
            items = (await (await client.get("/api/clipboard", headers=headers)).json())["items"]
            assert [item["text"] for item in items] == ["한글 <copy>\nline two", "on the host"]
            # The same text again moves to the front instead of repeating.
            await client.post("/api/clipboard", json={"text": "on the host"}, headers=headers)
            items = (await (await client.get("/api/clipboard", headers=headers)).json())["items"]
            assert [item["text"] for item in items] == ["on the host", "한글 <copy>\nline two"]
            for body in ({"text": ""}, {"text": 3}, {}, ["text"]):
                assert (await client.post("/api/clipboard", json=body, headers=headers)).status == 400
            assert (await client.post("/api/clipboard", data="not json", headers=headers)).status == 400
            too_long = {"text": "x" * (clipboard.MAX_TEXT + 1)}
            assert (await client.post("/api/clipboard", json=too_long, headers=headers)).status == 413
            assert len((await (await client.get("/api/clipboard", headers=headers)).json())["items"]) == 2
        await mgr.shutdown_all()
    asyncio.run(run())


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
