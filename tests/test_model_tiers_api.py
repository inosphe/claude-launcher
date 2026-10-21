"""The model tiers settings endpoints: GET/PUT /api/model-tiers."""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher import store
from claude_launcher.daemon import api, model_tiers
from claude_launcher.profile import create as create_profile


class _Request:
    def __init__(self, payload=None):
        self._payload = payload

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def get():
    resp = asyncio.run(api.h_model_tiers_get(_Request()))
    return resp.status, json.loads(resp.text)


def put(payload):
    resp = asyncio.run(api.h_model_tiers_put(_Request(payload)))
    return resp.status, json.loads(resp.text)


def _setup_profile(name: str = "a") -> None:
    def _mutate(doc: dict) -> None:
        doc.setdefault("providers", {})["ds"] = {
            "api_key": "k",
            "endpoints": {"anthropic": "https://ds.example.com"},
            "models": {"default": "ds4", "small": "ds4"},
        }

    store.update(_mutate)
    prof = create_profile(name)
    store.set_profile_field(prof.name, "provider", "ds")


_BLOCK = {"tiers": [{"id": "economy",
                     "entries": [{"profile": "a", "model": "small"}]}],
          "active": {}}


def test_get_returns_the_cleaned_block_and_catalogs(home):
    _setup_profile()
    assert get()[1]["block"]["tiers"] == []
    status, body = put(_BLOCK)
    assert status == 200
    assert body["block"]["tiers"][0]["id"] == "economy"
    assert "a" in body["profiles"]
    assert "claude" in body["harnesses"]["a"]
    assert "small" in body["models"]["a"]
    assert "small" in body["presets"]
    assert body["max_tiers"] == model_tiers.MAX_TIERS


def test_put_replaces_the_whole_block(home):
    _setup_profile()
    assert put(_BLOCK)[0] == 200
    other = {"tiers": [{"id": "heavy",
                        "entries": [{"profile": "a"}]}]}
    status, body = put(other)
    assert status == 200
    assert [t["id"] for t in body["block"]["tiers"]] == ["heavy"]


def test_put_refuses_an_invalid_combination_naming_the_field(home):
    _setup_profile()
    bad = {"tiers": [{"id": "t",
                      "entries": [{"profile": "ghost", "model": "small"}]}]}
    status, body = put(bad)
    assert status == 400
    assert "entry 1" in body["error"]


def test_put_refuses_a_body_without_tiers(home):
    _setup_profile()
    status, body = put({"active": {}})
    assert status == 400
    assert "tiers" in body["error"]


def test_put_validation_error_does_not_touch_the_stored_block(home):
    _setup_profile()
    assert put(_BLOCK)[0] == 200
    put({"tiers": [{"id": "t", "entries": [{"profile": "ghost"}]}]})
    status, body = get()
    assert [t["id"] for t in body["block"]["tiers"]] == ["economy"]
