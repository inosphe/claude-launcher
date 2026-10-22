"""GET/PUT /api/sweep-batch-setting: daemon.delayed_sweep_batch_size /
delayed_sweep_batch_settle from the Settings page.

D1 GET answers the effective values and the built-in defaults (5 / 600)
D2 PUT writes either field; null restores the default
D3 a negative, fractional-size, boolean, string or too-large value is a 400
   and the file is left as it was
D4 the batch policy the sweep clock reads follows the saved values
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import delayed_sweep
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


def test_get_answers_the_effective_values_and_defaults(home):
    async def body(client):
        resp = await client.get("/api/sweep-batch-setting", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["size"] == 5 and data["settle"] == 600.0
        assert data["defaults"] == {"size": 5, "settle": 600.0}

    _with_client(body)


def test_put_saves_a_field_and_null_restores_the_default(home):
    async def body(client):
        resp = await client.put("/api/sweep-batch-setting", json={"settle": 900}, headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["settle"] == 900.0 and data["size"] == 5
        assert store.daemon_config()["delayed_sweep_batch_settle"] == 900.0
        assert delayed_sweep.batch_policy() == (5, 900.0)

        resp = await client.put("/api/sweep-batch-setting", json={"size": 0}, headers=BEARER)
        assert (await resp.json())["size"] == 0
        assert delayed_sweep.batch_policy() is None

        resp = await client.put(
            "/api/sweep-batch-setting", json={"size": None, "settle": None}, headers=BEARER)
        data = await resp.json()
        assert data["size"] == 5 and data["settle"] == 600.0
        assert "daemon" not in store.load()

    _with_client(body)


def test_a_bad_value_is_refused_and_the_file_is_unchanged(home):
    async def body(client):
        for payload in (
            {"size": -1}, {"size": 2.5}, {"size": True}, {"size": "5"}, {"size": 101},
            {"settle": -1}, {"settle": "600"}, {"settle": 86401}, {},
        ):
            resp = await client.put("/api/sweep-batch-setting", json=payload, headers=BEARER)
            assert resp.status == 400, payload
        assert "daemon" not in store.load()

    _with_client(body)
