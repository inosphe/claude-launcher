"""GET/PUT /api/delegate-setting: daemon.delegate_profile / delegate_model
from the Settings page (claunch-yl0to).

D1 GET answers the built-in defaults (ds4 / small), the profiles on this
   launcher home and the claunch model presets
D2 PUT writes either field; null restores the default
D3 an unknown profile or a model outside the presets is a 400 and the file
   is left as it was
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import profile, store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


def _with_client(body):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await body(client)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_get_answers_the_defaults_profiles_and_presets(home):
    profile.create("cheap")

    async def body(client):
        resp = await client.get("/api/delegate-setting", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["profile"] == "ds4" and data["model"] == "small"
        assert data["defaults"] == {"profile": "ds4", "model": "small"}
        assert "cheap" in data["profiles"]
        assert data["presets"] == ["default", "small", "large", "xlarge", "subagent"]

    _with_client(body)


def test_put_saves_a_field_and_null_restores_the_default(home):
    profile.create("cheap")

    async def body(client):
        resp = await client.put("/api/delegate-setting", json={"profile": "cheap"}, headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["profile"] == "cheap" and data["model"] == "small"
        assert store.daemon_config()["delegate_profile"] == "cheap"

        resp = await client.put("/api/delegate-setting", json={"model": "large"}, headers=BEARER)
        assert (await resp.json())["model"] == "large"

        resp = await client.put("/api/delegate-setting", json={"profile": None}, headers=BEARER)
        assert (await resp.json())["profile"] == "ds4"
        assert "delegate_profile" not in (store.load().get("daemon") or {})

    _with_client(body)


def test_an_unknown_profile_or_model_is_refused(home):
    async def body(client):
        for payload in ({"profile": "nope"}, {"model": "haiku"}, {"model": ""}, {}):
            resp = await client.put("/api/delegate-setting", json=payload, headers=BEARER)
            assert resp.status == 400, payload
        assert "daemon" not in store.load()

    _with_client(body)
