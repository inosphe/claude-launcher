"""Host text history is bounded, authenticated and never sends terminal input."""
import asyncio
import time

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
