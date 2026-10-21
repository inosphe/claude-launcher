"""GET /api/test-output-dir: daemon.test_output_dir from the Settings page,
read-only (claunch-pyv3d.1).

A1 GET answers the saved setting ("" when unset) and the root it resolves
   to now -- the built-in per-platform default when empty
A2 a set value shows up in ``configured`` and ``resolved``; a relative
   value anchors at the launcher home
A3 the route is read-only: PUT is a 405 and the config file is untouched
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import config, pytest_basetemp, store
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


def test_get_answers_the_default_when_unset(home):
    async def body(client):
        resp = await client.get("/api/test-output-dir", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["configured"] == ""
        assert data["resolved"] == str(pytest_basetemp.root())

    _with_client(body)


def test_get_answers_a_set_absolute_value(home):
    store.set_daemon_field("test_output_dir", "/tmp/claunch-test-output-dir")

    async def body(client):
        resp = await client.get("/api/test-output-dir", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["configured"] == "/tmp/claunch-test-output-dir"
        assert data["resolved"] == "/tmp/claunch-test-output-dir"

    _with_client(body)


def test_get_resolves_a_relative_value_at_the_launcher_home(home):
    store.set_daemon_field("test_output_dir", "my-test-output")

    async def body(client):
        resp = await client.get("/api/test-output-dir", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["configured"] == "my-test-output"
        assert data["resolved"] == str(config.launcher_home() / "my-test-output")

    _with_client(body)


def test_put_is_refused_and_leaves_the_file(home):
    async def body(client):
        resp = await client.put(
            "/api/test-output-dir", json={"configured": "/elsewhere"}, headers=BEARER
        )
        assert resp.status == 405
        assert store.daemon_config().get("test_output_dir") in (None, "")

    _with_client(body)
