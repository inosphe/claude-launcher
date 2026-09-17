"""Settings persist relay secrets, apply the pool live, and keep other uplinks."""
import asyncio
import json
import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import relay_uplink
from claude_launcher.daemon.relay_settings import RelaySettings

REAL_RUN = relay_uplink.RelayUplink.run


@pytest.fixture(autouse=True)
def isolated_relays(monkeypatch):
    for key in os.environ:
        if key.startswith("CLAUNCH_RELAY_"):
            monkeypatch.delenv(key)

    async def run(up):
        up.connected = True
        try:
            await asyncio.Event().wait()
        finally:
            up.connected = False

    monkeypatch.setattr(relay_uplink.RelayUplink, "run", run)


def payload(ident="work", **changes):
    return dict(id=ident, url=f"wss://{ident}.example", name="my-pc",
                token="private-token", verify_tls=True) | changes


def test_live_add_preserves_existing_and_edit_keeps_secret():
    async def run():
        changes = []
        service = RelaySettings(9876, changes.append)
        await service.start()
        assert not changes
        try:
            await service.save(payload())
            await asyncio.sleep(0)
            first = service.pool.uplinks[0]
            task = service.tasks["work"]
            result = await service.save(payload("home"))
            await asyncio.sleep(0)
            assert service.pool.uplinks[0] is first
            assert service.tasks["work"] is task and not task.done()
            assert len(service.pool.uplinks) == 2
            assert "private-token" not in json.dumps(result)
            assert service.pool.state()["connected_count"] == 2
            await service.save(payload("home", url="wss://new.example", token=""))
            assert store.relays_config()[1]["token"] == "private-token"
            assert service.pool.uplinks[1].url == "wss://new.example"
            assert service.pool.uplinks[0] is first
            assert all(pool is service.pool for pool in changes)
        finally:
            await service.close()
        assert not service.tasks
    asyncio.run(run())


@pytest.mark.parametrize("changes", [
    {"url": "https://example.com"}, {"url": "wss://u:password@example.com"},
    {"url": "wss://example.com:bad"}, {"url": "wss://example.com/#fragment"},
    {"id": "bad id"}, {"token": 7}, {"verify_tls": "false"},
    {"name": "한" * 86}, {"token": ""},
    {"url": "wss://bad host"},
])
def test_invalid_settings_do_not_persist_or_start(changes):
    async def run():
        service = RelaySettings(9876, lambda pool: None)
        with pytest.raises(ValueError):
            await service.save(payload(**changes))
        assert store.relays_config() == []
        assert service.pool.uplinks == []
    asyncio.run(run())


def test_save_registers_with_two_websocket_servers_without_reconnecting_first(monkeypatch):
    from aiohttp import web
    from claude_launcher.daemon import relay_wire as wire

    monkeypatch.setattr(relay_uplink.RelayUplink, "run", REAL_RUN)

    async def run():
        registrations = []

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            message = await ws.receive()
            room, frame = wire.FrameDecoder().feed(message.data)[0]
            assert frame[0] == wire.REGISTER
            registrations.append(frame)
            await ws.send_bytes(wire._frame(room, 3, bytes([wire.REGISTER_OK])))
            async for _ in ws:
                pass
            return ws

        app1, app2 = web.Application(), web.Application()
        app1.router.add_get("/", websocket)
        app2.router.add_get("/", websocket)
        async with TestServer(app1) as server1, TestServer(app2) as server2:
            service = RelaySettings(9876, lambda pool: None)
            try:
                for ident, server in (("one", server1), ("two", server2)):
                    await service.save(payload(ident, url=str(server.make_url("/")).replace("http:", "ws:")))
                    async def connected():
                        while not all(up.connected for up in service.pool.uplinks):
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(connected(), timeout=5)
                assert len(registrations) == 2
                assert service.state()["relay"]["connected_count"] == 2
            finally:
                await service.close()
    asyncio.run(run())


def test_legacy_migration_and_environment_token(monkeypatch):
    store.set_relay_field("url", "wss://old.example")
    store.set_relay_field("token", "old-secret")
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN_HOME", "env-secret")

    async def run():
        service = RelaySettings(9876, lambda pool: None)
        await service.start()
        old = service.pool.uplinks[0]
        try:
            data = await service.save(payload("home", token=""))
            assert service.pool.uplinks[0] is old
            assert service.pool.uplinks[1].token == "env-secret"
            assert "relay" not in store.load()["daemon"]
            assert [row["id"] for row in store.relays_config()] == ["relay1", "home"]
            assert "secret" not in json.dumps(data)
        finally:
            await service.close()
    asyncio.run(run())


def test_bare_env_transition_cannot_disable_existing_relay(monkeypatch):
    store.add_relay("work", url="wss://work.example")
    monkeypatch.setenv("CLAUNCH_RELAY_TOKEN", "bare-secret")

    async def run():
        service = RelaySettings(9876, lambda pool: None)
        await service.start()
        try:
            with pytest.raises(ValueError, match="disable an existing"):
                await service.save(payload("home"))
            assert len(store.relays_config()) == len(service.pool.uplinks) == 1
        finally:
            await service.close()
    asyncio.run(run())


def test_routes_require_auth_and_do_not_return_tokens():
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        app = build_app(mgr, "auth", started_at=time.monotonic(), mesh=MeshManager(mgr))
        service = RelaySettings(9876, lambda pool: None)
        app["relay_settings"] = service
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/relays")).status == 401
            assert (await client.post("/api/relays", json=payload())).status == 401
            headers = {"Authorization": "Bearer auth"}
            try:
                resp = await client.post("/api/relays", json=payload(), headers=headers)
                assert resp.status == 200
                assert "private-token" not in await resp.text()
                resp = await client.get("/api/relays", headers=headers)
                assert resp.status == 200
                assert (await resp.json())["relays"][0]["token_set"] is True
                resp = await client.post("/api/relays", json=[], headers=headers)
                assert resp.status == 400
                assert len(store.relays_config()) == 1
            finally:
                await service.close()
    asyncio.run(run())
