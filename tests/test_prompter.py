"""The prompter proxy (daemon/prompter.py) and its three endpoints.

``PUT/GET /api/prompter`` stores or clears the server's base URL in
``daemon.prompter_url``; ``GET /api/prompter/prompts`` fetches that server's
``/api/snippets?kind=prompt`` for the footer. Off and unreachable both answer
200 with an empty list — the footer hides the row either way.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.prompt_packs import PackError
from claude_launcher.daemon import prompter
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

SNIPPETS = {
    "kind": "prompt",
    "count": 3,
    "snippets": [
        {"id": 1, "kind": "prompt", "name": "align", "title": "", "body": "align body\n",
         "tags": "", "position": 0, "created_at": "", "updated_at": "", "archived": 0},
        {"id": 2, "kind": "prompt", "name": "commit", "title": "Commit", "body": "commit body",
         "tags": "", "position": 1, "created_at": "", "updated_at": "", "archived": 0},
        {"id": 3, "kind": "prompt", "name": "empty", "title": "", "body": "  ",
         "tags": "", "position": 2, "created_at": "", "updated_at": "", "archived": 0},
    ],
}


async def _serve(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _fake_prompter(hits):
    async def snippets(request):
        hits.append(dict(request.query))
        return aioweb.json_response(SNIPPETS)

    app = aioweb.Application()
    app.router.add_get("/api/snippets", snippets)
    return await _serve(app)


def _daemon_app():
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
    return mgr, app


def setup_function():
    prompter._cache.clear()


def test_selected_prompts_use_configured_server_and_deduplicate(home):
    async def run():
        hits = []
        server = await _fake_prompter(hits)
        try:
            url = str(server.make_url("")).rstrip("/")
            prompter.set_url(url)
            recs = await prompter.selected_records({"url": url, "ids": [2, 1, 2]})
            assert [r["id"] for r in recs] == ["prompter:2", "prompter:1"]
            assert [r["text"] for r in recs] == ["commit body", "align body"]
            assert all(r["url"] == url and r["source"] == "prompter" for r in recs)
            assert hits == [{"kind": "prompt"}]
            with pytest.raises(PackError, match="unavailable"):
                await prompter.selected_records({"url": url, "ids": [3, 99]})
            with pytest.raises(PackError, match="server changed"):
                await prompter.selected_records({"url": "https://old.example", "ids": [1]})
        finally:
            await server.close()
    asyncio.run(run())


@pytest.mark.parametrize("selection", [[], "1", {}, {"ids": "1"}, {"ids": [True]},
                                       {"ids": [-1]}, {"ids": ["1"]}])
def test_selected_prompts_reject_malformed_selection(selection):
    with pytest.raises(PackError):
        asyncio.run(prompter.selected_records(selection))


def test_selected_prompts_fail_closed_only_when_selected(home, monkeypatch):
    prompter.set_url("https://p.example")
    calls = []

    async def fail(url):
        calls.append(url)
        raise RuntimeError("offline")

    monkeypatch.setattr(prompter, "_fetch", fail)

    async def run():
        assert await prompter.selected_records(None) == []
        assert await prompter.selected_records({"ids": []}) == []
        assert calls == []
        with pytest.raises(PackError, match="offline"):
            await prompter.selected_records({"url": "https://p.example", "ids": [1]})
        assert calls == ["https://p.example"]
    asyncio.run(run())


def test_normalize_url_accepts_http_and_blank():
    assert prompter.normalize_url(" https://p.example/ ") == "https://p.example"
    assert prompter.normalize_url("") is None
    assert prompter.normalize_url(None) is None
    for bad in ("ftp://x", "p.example", "https://", 5):
        try:
            prompter.normalize_url(bad)
        except prompter.PrompterURLError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_unset_url_answers_disabled_with_no_prompts(home):
    async def run():
        mgr, app = _daemon_app()
        client = await _serve(app)
        try:
            resp = await client.get("/api/prompter/prompts", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["enabled"] is False and body["connected"] is False
            assert body["prompts"] == []
            resp = await client.get("/api/prompter", headers=BEARER)
            assert (await resp.json()) == {"url": None}
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_set_url_fetches_prompts_and_clear_turns_it_off(home):
    async def run():
        hits = []
        fake = await _fake_prompter(hits)
        mgr, app = _daemon_app()
        client = await _serve(app)
        base = str(fake.make_url("")).rstrip("/")
        try:
            resp = await client.put("/api/prompter", json={"url": base + "/"}, headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["url"] == base
            assert store.daemon_config()["prompter_url"] == base
            status = body["status"]
            assert status["connected"] is True
            # Blank bodies are dropped; body text is stripped.
            assert [p["name"] for p in status["prompts"]] == ["align", "commit"]
            assert status["prompts"][0]["body"] == "align body"
            assert hits == [{"kind": "prompt"}]

            # Served from the short cache: no second request upstream.
            resp = await client.get("/api/prompter/prompts", headers=BEARER)
            assert (await resp.json())["connected"] is True
            assert len(hits) == 1
            resp = await client.get("/api/prompter/prompts?refresh=1", headers=BEARER)
            assert (await resp.json())["connected"] is True
            assert len(hits) == 2

            resp = await client.put("/api/prompter", json={"url": ""}, headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["url"] is None
            assert store.daemon_config()["prompter_url"] is None
            resp = await client.get("/api/prompter/prompts", headers=BEARER)
            assert (await resp.json())["enabled"] is False
        finally:
            await mgr.shutdown_all()
            await client.close()
            await fake.close()

    asyncio.run(run())


def test_unreachable_server_answers_not_connected(home):
    async def run():
        # Bind a server, note its address, then close it: nothing listens there.
        fake = await _fake_prompter([])
        base = str(fake.make_url("")).rstrip("/")
        await fake.close()
        mgr, app = _daemon_app()
        client = await _serve(app)
        try:
            resp = await client.put("/api/prompter", json={"url": base}, headers=BEARER)
            assert resp.status == 200
            status = (await resp.json())["status"]
            assert status["enabled"] is True
            assert status["connected"] is False
            assert status["prompts"] == []
            assert status["error"]
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_invalid_url_is_refused_and_not_stored(home):
    async def run():
        mgr, app = _daemon_app()
        client = await _serve(app)
        try:
            resp = await client.put("/api/prompter", json={"url": "not a url"}, headers=BEARER)
            assert resp.status == 400
            assert store.daemon_config()["prompter_url"] is None
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())
