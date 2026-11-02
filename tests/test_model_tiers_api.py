"""The model tiers settings endpoints: GET/PUT /api/model-tiers."""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher import lineage, profile as profile_mod, store
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


def _setup_codex_profile(name: str = "cx") -> None:
    """A codex profile whose provider declares no role models: the first
    profile the settings card's Add tier would seed (claunch-61vj3)."""
    from claude_launcher import lineage

    prof = create_profile(name)
    lineage.set_harness(prof, "codex")
    store.set_profile_field(name, "allowed_harnesses", ["codex"])


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


def test_put_accepts_the_block_add_tier_makes_for_a_codex_profile(home):
    """Add tier seeds ``entries: [{profile}]`` with no model; the PUT must
    land instead of answering 400 (claunch-61vj3)."""
    _setup_codex_profile()
    status, body = put({"tiers": [{"id": "misc",
                                   "entries": [{"profile": "cx"}]}]})
    assert status == 200
    assert body["block"]["tiers"][0]["entries"] == [{"profile": "cx"}]
    # The editor's Model select is built from this list.
    assert "default" in body["models"]["cx"]
    assert body["block"]["tiers"][0]["id"] == "misc"


def test_the_editor_model_choices_are_the_validation_list_per_entry_harness(home):
    """The card's Model select asks per (profile, entry harness): ``validate``
    reads the entry's own harness, so one list per profile would hide valid
    presets and offer invalid ones (claunch-61vj3 review)."""
    _setup_profile("a")
    store.set_profile_field("a", "allowed_harnesses", ["claude", "codex"])
    _setup_codex_profile("cx")
    status, body = get()
    assert status == 200
    doc = store.load()
    by = body["models_by_harness"]
    for name in ("a", "cx"):
        prof = profile_mod.require(name)
        effective = lineage.effective_harness(prof, doc)
        assert by[name][""] == model_tiers._model_choices(prof, effective, doc)
        for harness in body["harnesses"][name]:
            assert by[name][harness] == model_tiers._model_choices(prof, harness, doc)
    # The lists really differ for 'a' (claude default, codex also allowed).
    assert by["a"]["claude"] != by["a"]["codex"]


def test_put_validation_error_does_not_touch_the_stored_block(home):
    _setup_profile()
    assert put(_BLOCK)[0] == 200
    put({"tiers": [{"id": "t", "entries": [{"profile": "ghost"}]}]})
    status, body = get()
    assert [t["id"] for t in body["block"]["tiers"]] == ["economy"]
