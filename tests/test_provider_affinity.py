"""GET/PUT /api/provider-affinity: providers.<name>.session_affinity from the
Settings page (claunch-vjgwd.1.1).

A1 GET answers one row per configured provider, with the flag, whether the
    provider is fronted by a routing/metering shim, and a malformed flag
    surfaced as an error instead of raising
A2 PUT writes the flag (true writes the key, false drops it) and the rest of
    the provider entry survives
A3 an unknown provider or a non-boolean flag is a 400 and the file is left
    as it was
"""

from __future__ import annotations

import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
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


def _write_providers():
    store.update(lambda doc: doc.__setitem__("providers", {
        "fw": {"env": {"ANTHROPIC_BASE_URL": "https://api.fireworks.ai/"},
               "session_affinity": True},
        "plain": {"env": {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api/"}},
        "broken": {"env": {"ANTHROPIC_BASE_URL": "https://x/"},
                   "session_affinity": "yes"},
    }))


def _row(data, name):
    return next(r for r in data["providers"] if r["name"] == name)


def test_get_answers_one_row_per_provider(home):
    _write_providers()

    async def body(client):
        resp = await client.get("/api/provider-affinity", headers=BEARER)
        assert resp.status == 200
        data = await resp.json()
        assert [r["name"] for r in data["providers"]] == ["broken", "fw", "plain"]
        assert _row(data, "fw")["session_affinity"] is True
        assert _row(data, "plain")["session_affinity"] is False
        # metering is off in tests, so only a routing block fronts a provider
        assert _row(data, "fw")["fronted"] is False
        # a malformed flag reads back as off with the error surfaced
        assert _row(data, "broken")["session_affinity"] is False
        assert "must be a boolean" in _row(data, "broken")["error"]

    _with_client(body)


def test_put_writes_and_clears_the_flag(home):
    _write_providers()

    async def body(client):
        resp = await client.put("/api/provider-affinity",
                                json={"name": "plain", "session_affinity": True},
                                headers=BEARER)
        assert resp.status == 200
        assert _row(await resp.json(), "plain")["session_affinity"] is True
        entry = store.load()["providers"]["plain"]
        assert entry["session_affinity"] is True
        # the rest of the entry survived
        assert entry["env"]["ANTHROPIC_BASE_URL"]

        resp = await client.put("/api/provider-affinity",
                                json={"name": "plain", "session_affinity": False},
                                headers=BEARER)
        assert resp.status == 200
        assert _row(await resp.json(), "plain")["session_affinity"] is False
        # off drops the key rather than writing `false`
        assert "session_affinity" not in store.load()["providers"]["plain"]
        # and the provider that was on stays on
        assert store.load()["providers"]["fw"]["session_affinity"] is True

    _with_client(body)


def test_a_bad_request_leaves_the_file_alone(home):
    _write_providers()

    async def body(client):
        for payload in (
            {"name": "nope", "session_affinity": True},
            {"name": "fw", "session_affinity": "true"},
            {"name": "fw", "session_affinity": 1},
            {"name": "", "session_affinity": True},
            {"session_affinity": True},
        ):
            resp = await client.put("/api/provider-affinity", json=payload,
                                    headers=BEARER)
            assert resp.status == 400, payload
        doc = store.load()
        assert doc["providers"]["fw"]["session_affinity"] is True
        assert "session_affinity" not in doc["providers"]["plain"]

    _with_client(body)


def test_no_providers_configured_is_an_empty_list(home):
    async def body(client):
        resp = await client.get("/api/provider-affinity", headers=BEARER)
        assert resp.status == 200
        assert (await resp.json())["providers"] == []

    _with_client(body)
