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

from claude_launcher.daemon import vm_channel
from test_vm_channel import World
from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}
OTHER = {actors.HEADER: "agent:s2"}


@contextlib.asynccontextmanager
async def _api(tmp_path):
    fake = Fake()
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub)
    async with TestClient(TestServer(app)) as client:
        client.fake, client.reg, client.hub, client.world = fake, reg, hub, world
        yield client


def scenario(fn):
    """Run an async test body against a fresh app (the suite drives aiohttp
    with asyncio.run, as test_webdebug does)."""
    def test(tmp_path, monkeypatch):
        # Screenshots land under the daemon directory: keep it in the test's.
        monkeypatch.setattr(vm_api.paths, "daemon_dir", lambda: tmp_path)

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
    assert await api.hub.channels[vm_id].wait_up(2)


@scenario
async def test_a_session_cannot_register_start_or_grant(api):
    for path, body in (("/api/vm", {"id": "x"}), ("/api/vm/a/start", {}),
                       ("/api/vm/a/grant", {"session": "s1", "scopes": ["control"]}),
                       ("/api/vm/a/human", {"action": "driving"}),
                       ("/api/vm/a/revoke_controller", {})):
        resp = await api.post(path, json=body, headers=AGENT)
        assert resp.status == 403, path
        assert (await resp.json())["code"] == "vm_user_only"


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
    assert resp.status == 403 and (await resp.json())["code"] == "vm_no_grant"


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
    assert resp.status == 409 and body["code"] == "vm_slots_full"
    assert sorted(body["holders"]) == ["a", "b"]


@scenario
async def test_stop_that_times_out_is_a_gateway_timeout_and_force_is_separate(api):
    await _up(api)
    api.fake.shutdown_stops_vm = False
    resp = await api.post("/api/vm/a/stop")
    assert resp.status == 504 and (await resp.json())["code"] == "vm_stop_timeout"
    assert (await (await api.get("/api/vm/a")).json())["state"] == "stopping"
    resp = await api.post("/api/vm/a/force_stop")
    assert resp.status == 200 and (await resp.json())["state"] == "stopped"


@scenario
async def test_the_user_taking_the_screen_blocks_a_session(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    await api.post("/api/vm/a/human", json={"action": "driving"})
    resp = await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)
    assert resp.status == 423 and (await resp.json())["code"] == "vm_busy"
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


@scenario
async def test_start_opens_the_guest_channel_and_stop_closes_it_first(api):
    await _up(api)
    got = await (await api.get("/api/vm/a")).json()
    assert got["channel"] == "up" and got["cua_ready"] and got["ready"]
    assert len(api.world.opened) == 1
    assert (await api.post("/api/vm/a/stop")).status == 200
    got = await (await api.get("/api/vm/a")).json()
    assert got["state"] == "stopped" and got["channel"] == "idle" and not got["cua_ready"]
    assert "a" not in api.hub.channels


def _png_result():
    import base64
    return {"content": [{"type": "text", "text": "desktop"},
                        {"type": "image", "mimeType": "image/png",
                         "data": base64.b64encode(b"PNGDATA").decode()}],
            "isError": False}


@scenario
async def test_a_look_needs_view_a_click_needs_control_and_the_epoch(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    look = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                          headers=AGENT)
    assert look.status == 200
    click = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click"},
                           headers=AGENT)
    assert click.status == 403 and (await click.json())["code"] == "vm_no_grant"

    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    epoch = (await (await api.post("/api/vm/a/acquire", json={"session": "s1"},
                                   headers=AGENT)).json())["epoch"]
    first = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                    "epoch": epoch}, headers=AGENT)
    assert first.status == 409 and (await first.json())["code"] == "vm_observe_first"
    await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                   headers=AGENT)
    ok = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                 "epoch": epoch}, headers=AGENT)
    assert ok.status == 200
    stale = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click",
                                                    "epoch": epoch - 1}, headers=AGENT)
    assert stale.status == 409 and (await stale.json())["code"] == "vm_stale"


@scenario
async def test_screen_images_become_files_not_base64(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    chan = api.hub.channels["a"]

    async def fake_call(name, arguments=None, timeout=30):
        return _png_result()

    chan.call = fake_call
    body = await (await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                                 headers=AGENT)).json()
    image = [b for b in body["result"]["content"] if b["type"] == "image"][0]
    assert "data" not in image
    from pathlib import Path
    assert Path(image["path"]).read_bytes() == b"PNGDATA"


@scenario
async def test_a_down_channel_answers_unavailable_with_retry_after(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    await api.hub.channels["a"].stop()
    api.reg.set_channel("a", "down", "ssh closed")
    resp = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                          headers=AGENT)
    body = await resp.json()
    assert resp.status == 503 and body["code"] == "vm_unavailable" and body["retry_after"] == 5


@scenario
async def test_exec_needs_its_own_grant_and_control(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    epoch = (await (await api.post("/api/vm/a/acquire", json={"session": "s1"},
                                   headers=AGENT)).json())["epoch"]
    refused = await api.post("/api/vm/a/exec", json={"session": "s1", "command": "id",
                                                      "epoch": epoch}, headers=AGENT)
    assert refused.status == 403 and (await refused.json())["code"] == "vm_no_grant"
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["exec"]})
    await api.post("/api/vm/a/call", json={"session": "s1", "tool": "get_desktop_state"},
                   headers=AGENT)
    done = await api.post("/api/vm/a/exec", json={"session": "s1", "command": "id",
                                                   "epoch": epoch}, headers=AGENT)
    out = await done.json()
    assert done.status == 200 and out["rc"] == 0 and out["stdout"] == "ok\n"
    assert api.fake.execs[-1][-3:] == ["/bin/sh", "-c", "id"]


@scenario
async def test_tools_lists_which_are_look_only(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["view"]})
    body = await (await api.get("/api/vm/a/tools?session=s1", headers=AGENT)).json()
    assert body["tools"] == [{"name": "get_screen_size", "view_only": True, "description": ""}]
