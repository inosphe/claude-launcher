"""The provider/model price table behind the dashboard's cost estimates.

The dashboard's session card (under the context gauge) and the detail panel
turn a context reading into an estimated price when the person has entered
rates for the reading's model. The table lives in the config file
(``daemon.model_prices`` / ``daemon.model_price_currency``), read live like
the reminder/ping defaults, and these checks exercise the GET/PUT pair that
carries it between the Settings page and the file. Rows are keyed by
provider and then model (``claude_launcher.model_prices``); the GET also maps
each profile to its provider, which is how the dashboard picks the tier.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import model_prices, store
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


def _table(doc):
    """The GET answer without the profile map, which depends on the home."""
    return {k: v for k, v in doc.items() if k not in ("profiles", "any")}


async def _put(client, payload):
    resp = await client.put("/api/model-prices", headers=BEARER, json=payload)
    return resp


def test_get_defaults(mgr):
    async def scenario(client):
        got = await _get(client)
        assert _table(got) == {"prices": {}, "currency": "$"}
        assert got["any"] == "*"
        assert isinstance(got["profiles"], dict)

    asyncio.run(_with_client(mgr, scenario))


def test_put_roundtrip(mgr):
    payload = {
        "currency": "₩",
        "prices": {
            "*": {"claude-opus-5": {"input": 15, "cache_read": 1.5,
                                    "cache_write": 18.75, "output": 75}},
            "deepseek": {"deepseek-v4p1-flash-1017": {"input": 0.14, "cache_read": 0.014,
                                                      "cache_write": 1.1}},
        },
    }

    async def scenario(client):
        resp = await _put(client, payload)
        assert resp.status == 200
        got = await resp.json()
        assert got["currency"] == "₩"
        assert got["prices"]["*"]["claude-opus-5"]["output"] == 75
        assert got["prices"]["deepseek"]["deepseek-v4p1-flash-1017"]["input"] == 0.14
        # Read back independently — the file is the source of truth.
        assert (await _get(client)) == got
        # And the config file carries it under the daemon block.
        doc = store.load()
        assert doc["daemon"]["model_prices"]["*"]["claude-opus-5"]["input"] == 15
        assert doc["daemon"]["model_price_currency"] == "₩"

    asyncio.run(_with_client(mgr, scenario))


def test_empty_prices_clear(mgr):
    async def scenario(client):
        await _put(client, {"prices": {"*": {"m": {"input": 1}}}})
        resp = await _put(client, {"prices": {}})
        assert _table(await resp.json()) == {"prices": {}, "currency": "$"}
        assert "model_prices" not in store.daemon_config()

    asyncio.run(_with_client(mgr, scenario))


def test_missing_component_means_zero_rate(mgr):
    async def scenario(client):
        resp = await _put(client, {"prices": {"p": {"m": {"input": 2}}}})
        assert (await resp.json())["prices"] == {"p": {"m": {"input": 2.0}}}

    asyncio.run(_with_client(mgr, scenario))


def test_unset_cache_write_stays_unset(mgr):
    # cache_write is optional: the dashboard prices an unset one at the
    # row's input rate (app.js cacheWriteRate), so a null must not be
    # stored as 0 — 0 is a real rate that turns that fallback off.
    async def scenario(client):
        resp = await _put(client, {"prices": {"*": {
            "m": {"input": 0.3, "cache_write": None},
            "z": {"input": 0.3, "cache_write": 0},
        }}})
        got = (await resp.json())["prices"]["*"]
        assert got["m"] == {"input": 0.3}
        assert got["z"] == {"input": 0.3, "cache_write": 0.0}

    asyncio.run(_with_client(mgr, scenario))


@pytest.mark.parametrize("bad", [
    {"prices": []},
    {"prices": {"": {"input": 1}}},
    {"prices": {"m": "expensive"}},
    {"prices": {"m": {"input": "cheap"}}},
    {"prices": {"m": {"input": -1}}},
    {"prices": {"m": {"input": float("inf")}}},
    {"prices": {"m": {"input": float("nan")}}},
    {"prices": {"p": {"": {"input": 1}}}},
    {"prices": {"p": {"m": "expensive"}}},
    {"prices": {"p": {"m": {"input": -1}}}},
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


def test_legacy_rows_keyed_by_model_put_under_any(mgr):
    # An older client still sends model -> rates; it lands under "*".
    async def scenario(client):
        resp = await _put(client, {"prices": {"m": {"input": 2}}})
        assert (await resp.json())["prices"] == {"*": {"m": {"input": 2.0}}}

    asyncio.run(_with_client(mgr, scenario))


def test_legacy_table_in_the_file_reads_as_any(mgr):
    # A table saved before the provider tier keeps pricing until resaved.
    store.set_daemon_field("model_prices", {"m": {"input": 1.0},
                                            "deepseek": {"m": {"input": 2.0}}})

    async def scenario(client):
        got = await _get(client)
        assert got["prices"] == {"*": {"m": {"input": 1.0}},
                                 "deepseek": {"m": {"input": 2.0}}}

    asyncio.run(_with_client(mgr, scenario))


def test_get_maps_each_profile_to_its_provider(mgr, monkeypatch):
    monkeypatch.setattr(model_prices, "profile_providers",
                        lambda doc=None: {"ds4": "deepseek", "sr": "default"})

    async def scenario(client):
        assert (await _get(client))["profiles"] == {"ds4": "deepseek",
                                                    "sr": "default"}

    asyncio.run(_with_client(mgr, scenario))


def test_rate_for_prefers_the_provider_row_then_any():
    table = {"*": {"m": {"input": 1.0}}, "deepseek": {"m": {"input": 2.0}}}
    assert model_prices.rate_for(table, "deepseek", "m") == {"input": 2.0}
    assert model_prices.rate_for(table, "default", "m") == {"input": 1.0}
    assert model_prices.rate_for(table, "", "m") == {"input": 1.0}
    assert model_prices.rate_for(table, "deepseek", "other") is None
    assert model_prices.rate_for({"m": {"input": 3.0}}, "deepseek", "m") == {"input": 3.0}
    assert model_prices.rate_for(None, "deepseek", "m") is None


def test_rate_for_skips_a_row_with_no_rates():
    # Only a hand-edited config holds one (clean drops it); it must not hide
    # the "*" row or price the model at 0.
    table = {"*": {"m": {"input": 1.0}}, "deepseek": {"m": {}}}
    assert model_prices.rate_for(table, "deepseek", "m") == {"input": 1.0}
    assert model_prices.rate_for({"*": {"m": {}}}, "deepseek", "m") is None


def test_clean_drops_a_model_whose_rates_are_all_unset():
    got = model_prices.clean({
        "deepseek": {"m": {"cache_write": None}, "n": {"input": 1}},
        "*": {"m": {}},
    })
    assert got == {"deepseek": {"n": {"input": 1.0}}}


def test_normalize_block_row_wins_over_legacy_row_for_the_same_model():
    # Both orders: the outcome must not depend on which key the file lists first.
    legacy, block = {"input": 1.0}, {"input": 2.0}
    assert model_prices.normalize({"m": legacy, "*": {"m": block}}) == {
        "*": {"m": block}}
    assert model_prices.normalize({"*": {"m": block}, "m": legacy}) == {
        "*": {"m": block}}
    assert model_prices.normalize({"m": legacy, "*": {"n": block}}) == {
        "*": {"m": legacy, "n": block}}


def test_profile_providers_follow_the_profile_selection(home):
    from claude_launcher import profile as profile_mod

    profile_mod.create("ds4")
    profile_mod.create("plain")
    doc = store.load()
    doc.setdefault("providers", {})["deepseek"] = {
        "endpoints": {"anthropic": "https://example.invalid/anthropic"}}
    doc.setdefault("profiles", {})["ds4"] = {"provider": "deepseek"}
    doc["profiles"]["plain"] = {}
    store.save(doc)
    got = model_prices.profile_providers()
    assert got["ds4"] == "deepseek"
    assert got["plain"] != "deepseek"
