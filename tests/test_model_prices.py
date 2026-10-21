"""The per-model price table behind the dashboard's cost estimates.

The dashboard's session card (under the context gauge) and the detail panel
turn a context reading into an estimated price when the person has entered
rates for the reading's model. The table lives in the config file
(``daemon.model_prices`` / ``daemon.model_price_currency``), read live like
the reminder/ping defaults, and these checks exercise the GET/PUT pair that
carries it between the Settings page and the file.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import store
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}


@pytest.fixture
def mgr(home, tmp_path, monkeypatch):
    """A session manager against an isolated launcher home."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    (tmp_path / "proj").mkdir()
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _with_client(mgr, fn):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr)
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        return await fn(client)
    finally:
        await client.close()


async def _get(client):
    resp = await client.get("/api/model-prices", headers=BEARER)
    assert resp.status == 200
    return await resp.json()


async def _put(client, payload):
    resp = await client.put("/api/model-prices", headers=BEARER, json=payload)
    return resp


def test_get_defaults(mgr):
    async def scenario(client):
        assert (await _get(client)) == {"prices": {}, "currency": "$"}

    asyncio.run(_with_client(mgr, scenario))


def test_put_roundtrip(mgr):
    payload = {
        "currency": "₩",
        "prices": {
            "claude-opus-5": {"input": 15, "cache_read": 1.5,
                              "cache_write": 18.75, "output": 75},
            "deepseek-v4p1-flash-1017": {"input": 0.14, "cache_read": 0.014,
                                         "cache_write": 1.1},
        },
    }

    async def scenario(client):
        resp = await _put(client, payload)
        assert resp.status == 200
        got = await resp.json()
        assert got["currency"] == "₩"
        assert got["prices"]["claude-opus-5"]["output"] == 75
        # Read back independently — the file is the source of truth.
        assert (await _get(client)) == got
        # And the config file carries it under the daemon block.
        doc = store.load()
        assert doc["daemon"]["model_prices"]["claude-opus-5"]["input"] == 15
        assert doc["daemon"]["model_price_currency"] == "₩"

    asyncio.run(_with_client(mgr, scenario))


def test_empty_prices_clear(mgr):
    async def scenario(client):
        await _put(client, {"prices": {"m": {"input": 1}}})
        resp = await _put(client, {"prices": {}})
        assert (await resp.json()) == {"prices": {}, "currency": "$"}
        assert "model_prices" not in store.daemon_config()

    asyncio.run(_with_client(mgr, scenario))


def test_missing_component_means_zero_rate(mgr):
    async def scenario(client):
        resp = await _put(client, {"prices": {"m": {"input": 2}}})
        assert (await resp.json())["prices"] == {"m": {"input": 2.0}}

    asyncio.run(_with_client(mgr, scenario))


@pytest.mark.parametrize("bad", [
    {"prices": []},
    {"prices": {"": {"input": 1}}},
    {"prices": {"m": "expensive"}},
    {"prices": {"m": {"input": "cheap"}}},
    {"prices": {"m": {"input": -1}}},
    {"prices": {"m": {"input": float("inf")}}},
    {"prices": {"m": {"input": float("nan")}}},
    {"currency": "USDUSDUSD"},
])
def test_validation_rejects(mgr, bad):
    async def scenario(client):
        resp = await _put(client, bad)
        assert resp.status == 400, await resp.text()
        assert "error" in (await resp.json())

    asyncio.run(_with_client(mgr, scenario))


def test_partial_currency_only(mgr):
    async def scenario(client):
        resp = await _put(client, {"currency": "€"})
        got = await resp.json()
        assert got["currency"] == "€"
        assert got["prices"] == {}

    asyncio.run(_with_client(mgr, scenario))
