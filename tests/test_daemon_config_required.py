"""The daemon does not start in a home without a config file
(``config.missing_config_error``), and nothing stops a running daemon to
start one that would refuse.

A daemon that came up on built-in defaults ran with no relays, on loopback,
without the PTY host and without its profiles (2026-10-25: a reproduction
script replaced the config file with its fixture, and a restart picked it up).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import config, daemon_client
from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import paths, restart_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}


def test_the_message_names_the_missing_file(home):
    msg = config.missing_config_error()
    assert str(home / ".claunch.yaml") in msg and "does not start" in msg
    (home / ".claunch.yaml").write_text("version: 2\n", encoding="utf-8")
    assert config.missing_config_error() is None


def test_the_daemon_refuses_to_start_without_a_config_file(home, monkeypatch, capsys):
    served = []

    async def fake_serve(*args, **kwargs):
        served.append(args)
        return 0

    monkeypatch.setattr(daemon_main, "_serve", fake_serve)
    assert daemon_main.main([]) == daemon_main.NO_CONFIG_EXIT
    err = capsys.readouterr().err
    assert "error: config file" in err and str(home / ".claunch.yaml") in err
    assert served == []
    assert not (home / ".claunch.yaml").exists()
    assert not paths.daemon_dir().exists()


def test_start_refuses_before_spawning(home, monkeypatch):
    spawned = []
    monkeypatch.setattr(daemon_client, "spawn_daemon", lambda *a, **k: spawned.append(1))
    with pytest.raises(daemon_client.DaemonClientError, match="not found"):
        daemon_client.start()
    assert spawned == []


def test_restart_refuses_before_stopping_the_running_daemon(home, monkeypatch):
    stopped = []
    monkeypatch.setattr(daemon_client, "stop", lambda **k: stopped.append(k))
    with pytest.raises(daemon_client.DaemonClientError, match="not found"):
        daemon_client.restart()
    assert stopped == []


def _with_app(home, check):
    """Run ``check`` against a served app whose home then has no config file
    (building the app writes one, so it is removed after the start)."""
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        (home / ".claunch.yaml").unlink(missing_ok=True)
        try:
            await check(app, client)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_the_restart_api_refuses(home):
    async def check(app, client):
        resp = await client.post("/api/daemon/restart", headers=AUTH, json={})
        assert resp.status == 409
        assert "not found" in (await resp.json())["error"]
        assert app["restart_requested"] is False
        assert not app["shutdown_event"].is_set()

    _with_app(home, check)


def test_the_gate_neither_approves_nor_times_out_into_a_restart(home):
    async def check(app, client):
        gate = app["restart_gate"]
        gate.submit(session="s1", pty_host="full")
        resp = await client.post(
            "/api/daemon/restart-request/approve", headers=AUTH, json={}
        )
        assert resp.status == 409
        assert "not found" in (await resp.json())["error"]
        assert gate.get()["status"] == "pending"

        gate._on_timeout(gate.get()["id"])
        record = gate.get()
        assert record["status"] == "rejected" and "not found" in record["refused"]
        assert app["restart_requested"] is False
        assert not app["shutdown_event"].is_set()

    _with_app(home, check)


def test_gate_approval_raises_config_missing(home):
    async def check(app, client):
        gate = app["restart_gate"]
        gate.submit(session="s1", pty_host="full")
        with pytest.raises(restart_gate.ConfigMissing):
            gate.approve()

    _with_app(home, check)
