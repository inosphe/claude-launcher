"""The VM feature switch (``daemon.vm_enabled``, default off).

E1 off by default: the VM routes answer vm_disabled, the dashboard's request
   poll says enabled false with nothing to announce, the page itself loads,
   and the hub's background work has not started
E2 the user turns it on: the routes answer and the hub starts, without a
   restart; turning it on from the CLI (the config file) works the same way
E3 a session cannot switch it, and the body must say true or false
E4 turning it off is refused while a VM is running, and works once it is
   stopped; the hub's background work stops with it
E5 the switch reports what the host offers tart
"""
from __future__ import annotations

import asyncio
import contextlib

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import vm_api, vm_channel, vm_tart
from claude_launcher.daemon.vmhub import Registry

from test_vm_api import AGENT
from test_vm_channel import World
from test_vm_tart import Fake


@contextlib.asynccontextmanager
async def _api(tmp_path):
    fake = Fake()
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub)
    async with TestClient(TestServer(app)) as client:
        client.app_ref = app
        yield client


def scenario(fn):
    def test(tmp_path, monkeypatch):
        monkeypatch.setattr(vm_api.paths, "daemon_dir", lambda: tmp_path)

        async def run():
            async with _api(tmp_path) as api:
                await fn(api)
        asyncio.run(run())
    test.__name__ = fn.__name__
    return test


def _hub_started(api) -> bool:
    return api.app_ref["vm_hub"]["tasks"] is not None


@scenario
async def test_e1_off_by_default(api):
    for method, path in (("GET", "/api/vm"), ("POST", "/api/vm"), ("GET", "/api/vm-presets"),
                         ("POST", "/api/vm/a/start"), ("GET", "/api/vm/a/frame")):
        resp = await api.request(method, path, json={})
        assert resp.status == 409, path
        assert (await resp.json())["code"] == "vm_disabled", path
    assert await (await api.get("/api/vm-requests")).json() == {"enabled": False, "requests": []}
    state = await (await api.get("/api/vm-enabled")).json()
    assert state["enabled"] is False
    assert not _hub_started(api)


@scenario
async def test_e2_the_user_turns_it_on(api):
    resp = await api.post("/api/vm-enabled", json={"enabled": True})
    assert resp.status == 200 and (await resp.json())["enabled"] is True
    assert store.daemon_config()["vm_enabled"] is True
    assert _hub_started(api)
    assert (await api.get("/api/vm")).status == 200
    assert (await (await api.get("/api/vm-requests")).json())["enabled"] is True


@scenario
async def test_e2_turned_on_in_the_config_file(api):
    store.set_daemon_field("vm_enabled", True)
    assert (await api.get("/api/vm")).status == 200
    assert _hub_started(api)


@scenario
async def test_e3_a_session_cannot_switch_it_and_the_body_is_checked(api):
    resp = await api.post("/api/vm-enabled", json={"enabled": True}, headers=AGENT)
    assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
    assert store.daemon_config()["vm_enabled"] is False
    for body in ({}, {"enabled": "yes"}, {"enabled": 1}):
        resp = await api.post("/api/vm-enabled", json=body)
        assert resp.status == 400, body
    assert store.daemon_config()["vm_enabled"] is False


@scenario
async def test_e4_off_is_refused_while_a_vm_runs(api):
    await api.post("/api/vm-enabled", json={"enabled": True})
    assert (await api.post("/api/vm", json={"id": "a", "ssh_key": "/keys/k"})).status == 201
    assert (await api.post("/api/vm/a/start")).status == 200
    resp = await api.post("/api/vm-enabled", json={"enabled": False})
    assert resp.status == 409
    got = await resp.json()
    assert got["code"] == "vm_busy_state" and "a" in got["error"]
    assert store.daemon_config()["vm_enabled"] is True

    assert (await api.post("/api/vm/a/stop")).status == 200
    resp = await api.post("/api/vm-enabled", json={"enabled": False})
    assert resp.status == 200 and (await resp.json())["enabled"] is False
    assert not _hub_started(api)
    assert (await api.get("/api/vm")).status == 409


@scenario
async def test_e5_the_switch_reports_the_host(api):
    host = (await (await api.get("/api/vm-enabled")).json())["host"]
    assert set(host) == {"macos", "apple_silicon", "tart", "ok"}
    assert host["ok"] == (host["apple_silicon"] and host["tart"])
