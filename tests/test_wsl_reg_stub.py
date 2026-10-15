"""daemon.wsl_hide_reg_exe (claude_launcher/wsl_reg_stub.py) and its endpoint.

A1 off by default; only ``True`` turns it on
A2 argv is unchanged when off, off WSL, or without reg.exe
A3 on WSL with the setting on, argv goes through the shim and keeps the command
A4 the stub file holds the ``exit 1`` script and is executable
B1 GET/PUT /api/wsl-reg-stub round-trips the setting; a non-bool is a 400
C1 (WSL only) inside the shim reg.exe is the stub and the uid is kept;
   outside it reg.exe is untouched
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store, wsl_reg_stub
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


def test_off_by_default_and_only_true_turns_it_on():
    assert store.DAEMON_DEFAULTS["wsl_hide_reg_exe"] is False
    assert wsl_reg_stub.enabled({"wsl_hide_reg_exe": True}) is True
    for value in (False, None, "true", 1):
        assert wsl_reg_stub.enabled({"wsl_hide_reg_exe": value}) is False
    assert wsl_reg_stub.enabled({}) is False


def test_argv_unchanged_when_off_or_not_applicable(home, monkeypatch):
    argv = ["claude", "--resume", "x"]
    on = {"wsl_hide_reg_exe": True}
    monkeypatch.setattr(wsl_reg_stub, "is_wsl", lambda: True)
    monkeypatch.setattr(wsl_reg_stub.os.path, "exists", lambda p: True)
    assert wsl_reg_stub.wrap_argv(argv, {}) == argv

    monkeypatch.setattr(wsl_reg_stub, "is_wsl", lambda: False)
    assert wsl_reg_stub.wrap_argv(argv, on) == argv

    monkeypatch.setattr(wsl_reg_stub, "is_wsl", lambda: True)
    monkeypatch.setattr(wsl_reg_stub.os.path, "exists", lambda p: False)
    assert wsl_reg_stub.wrap_argv(argv, on) == argv


def test_argv_goes_through_the_shim_when_on(home, monkeypatch):
    if not hasattr(os, "unshare"):
        pytest.skip("os.unshare needs Python 3.12+")
    monkeypatch.setattr(wsl_reg_stub, "is_wsl", lambda: True)
    monkeypatch.setattr(wsl_reg_stub.os.path, "exists", lambda p: True)
    argv = ["claude", "--resume", "x"]
    wrapped = wsl_reg_stub.wrap_argv(argv, {"wsl_hide_reg_exe": True})
    assert wrapped[:3] == [sys.executable, "-m", "claude_launcher.wsl_reg_stub"]
    assert wrapped[3] == "--stub"
    assert wrapped[5] == "--"
    assert wrapped[6:] == argv
    stub = wrapped[4]
    assert open(stub, encoding="utf-8").read() == wsl_reg_stub.STUB_TEXT
    assert os.access(stub, os.X_OK)


def test_shim_rejects_a_malformed_command_line(capsys):
    assert wsl_reg_stub.main(["claude"]) == 2
    assert "usage" in capsys.readouterr().err


def test_endpoint_round_trips_the_setting(home):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/wsl-reg-stub", headers=BEARER)
            assert resp.status == 200
            body = await resp.json()
            assert body["enabled"] is False
            assert body["wsl"] is wsl_reg_stub.is_wsl()

            resp = await client.put("/api/wsl-reg-stub", json={"enabled": True}, headers=BEARER)
            assert resp.status == 200
            assert (await resp.json())["enabled"] is True
            assert store.daemon_config()["wsl_hide_reg_exe"] is True

            resp = await client.put("/api/wsl-reg-stub", json={"enabled": "yes"}, headers=BEARER)
            assert resp.status == 400
            assert store.daemon_config()["wsl_hide_reg_exe"] is True

            resp = await client.put("/api/wsl-reg-stub", json={"enabled": False}, headers=BEARER)
            assert (await resp.json())["enabled"] is False
            assert "wsl_hide_reg_exe" not in (store.load().get("daemon") or {})
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


@pytest.mark.skipif(not (wsl_reg_stub.is_wsl() and os.path.exists(wsl_reg_stub.REG_EXE)
                         and hasattr(os, "unshare")),
                    reason="needs WSL with Windows reg.exe and Python 3.12+")
def test_shim_hides_reg_exe_only_inside(home):
    stub = str(wsl_reg_stub.stub_path())
    probe = (
        f"head -c 2 {wsl_reg_stub.REG_EXE}; echo; "
        f"{wsl_reg_stub.REG_EXE} query 'HKLM\\SOFTWARE\\Policies\\ClaudeCode' /v Settings"
        " >/dev/null 2>&1; echo reg=$?; id -u"
    )
    out = subprocess.run(
        [sys.executable, "-m", "claude_launcher.wsl_reg_stub", "--stub", stub, "--",
         "sh", "-c", probe],
        capture_output=True, text=True, timeout=60,
    )
    assert "could not hide" not in out.stderr, out.stderr
    lines = out.stdout.split()
    assert lines[0] == "#!"            # the stub, not a Windows binary
    assert lines[1] == "reg=1"
    assert lines[2] == str(os.getuid())
    with open(wsl_reg_stub.REG_EXE, "rb") as f:
        assert f.read(2) == b"MZ"       # untouched outside the namespace
