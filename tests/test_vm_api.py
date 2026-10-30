"""/api/vm (vm-setup-8s6, step 3): who may call what, and the lifecycle
routes over a fake tart."""
from __future__ import annotations

import asyncio
import contextlib

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import actors, vm_api, vm_tart
from claude_launcher.daemon.vmhub import Registry

from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}
OTHER = {actors.HEADER: "agent:s2"}


@contextlib.asynccontextmanager
async def _api(tmp_path):
    fake = Fake()
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    app = web.Application()
    vm_api.install(app, reg, mgr)
    async with TestClient(TestServer(app)) as client:
        client.fake, client.reg = fake, reg
        yield client


def scenario(fn):
    """Run an async test body against a fresh app (the suite drives aiohttp
    with asyncio.run, as test_webdebug does)."""
    def test(tmp_path):
        async def run():
            async with _api(tmp_path) as api:
                await fn(api)
        asyncio.run(run())
    test.__name__ = fn.__name__
    return test


async def _register(api, vm_id="a"):
    resp = await api.post("/api/vm", json={"id": vm_id, "ssh_key": "/keys/k"})
    assert resp.status == 201, await resp.text()


async def _up(api, vm_id="a"):
    """Registered, started, the guest channel marked ready (step 4's job)."""
    await _register(api, vm_id)
    assert (await api.post(f"/api/vm/{vm_id}/start")).status == 200
    api.reg.set_ready(vm_id, cua=True)


@scenario
async def test_a_session_cannot_register_start_or_grant(api):
    for path, body in (("/api/vm", {"id": "x"}), ("/api/vm/a/start", {}),
                       ("/api/vm/a/grant", {"session": "s1", "scopes": ["control"]}),
                       ("/api/vm/a/human", {"action": "driving"}),
                       ("/api/vm/a/revoke_controller", {})):
        resp = await api.post(path, json=body, headers=AGENT)
        assert resp.status == 403, path
        assert (await resp.json())["error"] == "vm_user_only"


@scenario
async def test_the_user_registers_starts_and_grants(api):
    await _up(api)
    resp = await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    assert (await resp.json())["grants"] == ["view", "control"]
    got = await (await api.get("/api/vm/a")).json()
    assert got["state"] == "running" and got["ready"] and got["grants"]["s1"]


@scenario
async def test_a_session_acquires_as_itself_and_not_as_another(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    other = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=OTHER)
    assert other.status == 403
    ok = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert ok.status == 200 and (await ok.json())["epoch"] >= 1


@scenario
async def test_a_second_session_is_told_who_holds_it(api):
    await _up(api)
    for s in ("s1", "s2"):
        await api.post("/api/vm/a/grant", json={"session": s, "scopes": ["control"]})
    await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    resp = await api.post("/api/vm/a/acquire", json={"session": "s2"}, headers=OTHER)
    assert resp.status == 409
    assert (await resp.json())["held_by"] == "s1"


@scenario
async def test_acquire_without_a_grant_is_forbidden(api):
    await _up(api)
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 403 and (await resp.json())["error"] == "vm_no_grant"


@scenario
async def test_a_session_lists_only_what_it_may_see_and_only_its_own_grants(api):
    await _up(api)
    await _register(api, "b")
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["control"]})
    rows = (await (await api.get("/api/vm?session=s1")).json())["vms"]
    assert [r["id"] for r in rows] == ["a"]
    assert rows[0]["grants"] == {"s1": ["view"]}


@scenario
async def test_a_full_machine_answers_409_with_the_holders(api):
    await _register(api, "a")
    await _register(api, "b")
    await _register(api, "c")
    for v in "ab":
        assert (await api.post(f"/api/vm/{v}/start")).status == 200
    resp = await api.post("/api/vm/c/start")
    body = await resp.json()
    assert resp.status == 409 and body["error"] == "vm_slots_full"
    assert sorted(body["holders"]) == ["a", "b"]


@scenario
async def test_stop_that_times_out_is_a_gateway_timeout_and_force_is_separate(api):
    await _up(api)
    api.fake.shutdown_stops_vm = False
    resp = await api.post("/api/vm/a/stop")
    assert resp.status == 504 and (await resp.json())["error"] == "vm_stop_timeout"
    assert (await (await api.get("/api/vm/a")).json())["state"] == "stopping"
    resp = await api.post("/api/vm/a/force_stop")
    assert resp.status == 200 and (await resp.json())["state"] == "stopped"


@scenario
async def test_the_user_taking_the_screen_blocks_a_session(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    await api.post("/api/vm/a/human", json={"action": "driving"})
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 423 and (await resp.json())["error"] == "vm_busy"
    await api.post("/api/vm/a/human", json={"action": "idle"})
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 200


@scenario
async def test_unknown_vm_and_bad_bodies(api):
    assert (await api.get("/api/vm/nope")).status == 404
    resp = await api.post("/api/vm", data="not json")
    assert resp.status == 400
    resp = await api.post("/api/vm/a/human", json={"action": "dance"})
    assert resp.status in (400, 404)


def test_the_settings_have_defaults():
    cfg = store.DAEMON_DEFAULTS
    assert cfg["vm_slot_limit"] == 2 and cfg["vm_human_idle"] == 15
    assert cfg["vm_stop_wait"] == 120 and cfg["vm_graphics"] is True
