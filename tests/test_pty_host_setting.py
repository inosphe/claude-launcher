"""GET/PUT /api/pty-host-setting: daemon.pty_host from the Settings page
(claunch-xnnct.5).

A1 GET answers the saved setting (off by default), whether the platform
   has a PTY host, and host_code.info() as the daemon runs it
A2 PUT {enabled} writes daemon.pty_host to the config file and answers the
   same shape; the running daemon is unchanged (host.configured is still
   the value it started with) until the next restart
A3 a non-bool ``enabled`` is a 400 and the file is left as it was
"""

from __future__ import annotations

import asyncio
import sys
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import host_code, pty_backend
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


def test_get_answers_the_setting_and_the_running_host(home, monkeypatch):
    monkeypatch.setattr(host_code, "_configured", False)
    monkeypatch.setattr(pty_backend, "current_host", lambda: None)

    async def body(client):
        resp = await client.get("/api/pty-host-setting", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is False
        assert data["supported"] is (sys.platform != "win32")
        assert data["host"]["state"] == "off"
        assert data["host"]["configured"] is False

    _with_client(body)


def test_put_saves_the_setting_and_leaves_the_running_daemon(home, monkeypatch):
    monkeypatch.setattr(host_code, "_configured", False)
    monkeypatch.setattr(pty_backend, "current_host", lambda: None)

    async def body(client):
        resp = await client.put("/api/pty-host-setting", json={"enabled": True}, headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert data["enabled"] is True
        assert store.daemon_config()["pty_host"] is True
        # The daemon decides at start: until a restart it runs as it started.
        assert data["host"]["configured"] is False
        assert data["host"]["state"] == "off"

        resp = await client.get("/api/pty-host-setting", headers=BEARER)
        assert (await resp.json())["enabled"] is True

        resp = await client.put("/api/pty-host-setting", json={"enabled": False}, headers=BEARER)
        assert resp.status == 200
        assert (await resp.json())["enabled"] is False
        assert store.daemon_config()["pty_host"] is False

    _with_client(body)


def test_put_refuses_a_non_bool(home, monkeypatch):
    monkeypatch.setattr(pty_backend, "current_host", lambda: None)

    async def body(client):
        for bad in ("yes", 1, None):
            resp = await client.put("/api/pty-host-setting", json={"enabled": bad}, headers=BEARER)
            assert resp.status == 400, bad
        resp = await client.put("/api/pty-host-setting", json={}, headers=BEARER)
        assert resp.status == 400
        assert store.daemon_config()["pty_host"] is False

    _with_client(body)
