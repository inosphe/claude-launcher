"""Model tier presets: storage, validation, resolution."""

from __future__ import annotations

import pytest

from claude_launcher import store
from claude_launcher.daemon import model_tiers
from claude_launcher.profile import create as create_profile


def _profiles(*names: str, provider: str = "ds") -> None:
    from claude_launcher import providers

    def _mutate(doc: dict) -> None:
        doc.setdefault("providers", {})[provider] = {
            "api_key": "k",
            "endpoints": {"anthropic": "https://ds.example.com"},
            "models": {"default": "ds4", "small": "ds4", "large": "ds4-pro"},
        }

    store.update(_mutate)
    for name in names:
        prof = create_profile(name)
        store.set_profile_field(prof.name, "provider", provider)


def _block(*tiers, active=None) -> dict:
    return {"version": 1, "tiers": list(tiers),
            "active": active or {}}


def _tier(tier_id: str, *entries) -> dict:
    return {"id": tier_id, "entries": list(entries)}


def _entry(profile: str, model=None, harness=None) -> dict:
    out = {"profile": profile}
    if model:
        out["model"] = model
    if harness:
        out["harness"] = harness
    return out


# --- cleaning -----------------------------------------------------------------

def test_a_missing_block_reads_as_empty(home):
    assert model_tiers.load() == {"version": 1, "tiers": [], "active": {}}


def test_clean_drops_what_makes_no_sense(home):
    block = model_tiers._clean({
        "version": 9,
        "tiers": [
            {"id": "ok", "entries": [{"profile": "a", "model": "small"},
                                      {"model": "no-profile"},
                                      "garbage"]},
            {"id": "Bad Id!"},
            {"id": "ok"},  # duplicate id dropped
            "garbage",
        ],
        "active": {"ok": 99, "missing": 0, "ok-list": "x"},
    })
    assert block["tiers"] == [
        {"id": "ok", "entries": [{"profile": "a", "model": "small"}]}
    ]
    assert block["active"] == {}


# --- validation ---------------------------------------------------------------

def test_a_valid_block_round_trips(home):
    _profiles("a", "b")
    block = _block(_tier("economy", _entry("a", model="small"),
                         _entry("b", model="large")))
    saved = model_tiers.save(block)
    assert saved == model_tiers.load()
    assert model_tiers.resolve("economy") == ("a", "claude", "small")


def test_validate_requires_at_least_one_tier(home):
    with pytest.raises(model_tiers.TierError, match="at least one tier"):
        model_tiers.save({"tiers": []})


def test_validate_caps_the_tier_count(home):
    _profiles("a")
    tiers = [_tier(f"t{i}", _entry("a")) for i in range(model_tiers.MAX_TIERS + 1)]
    with pytest.raises(model_tiers.TierError, match="at most"):
        model_tiers.save(_block(*tiers))


def test_an_unknown_profile_is_refused_with_the_field(home):
    _profiles("a")
    with pytest.raises(model_tiers.TierError, match="entry 1"):
        model_tiers.save(_block(_tier("t", _entry("ghost"))))


def test_a_denied_harness_is_refused(home):
    _profiles("a")
    with pytest.raises(model_tiers.TierError, match="harness 'ghost-harness'"):
        model_tiers.save(
            _block(_tier("t", _entry("a", harness="ghost-harness")))
        )


def test_an_unknown_model_is_refused_and_names_the_choices(home):
    _profiles("a")
    with pytest.raises(model_tiers.TierError, match="model 'mystery'"):
        model_tiers.save(_block(_tier("t", _entry("a", model="mystery"))))


def test_duplicate_entries_in_one_tier_are_refused(home):
    _profiles("a")
    tier = _tier("t", _entry("a", model="small"),
                 _entry("a", model="small", harness="claude"))
    with pytest.raises(model_tiers.TierError, match="duplicate"):
        model_tiers.save(_block(tier))


def test_defaults_make_an_entry_valid(home):
    _profiles("a")
    saved = model_tiers.save(_block(_tier("t", _entry("a"))))
    assert saved["tiers"][0]["entries"] == [{"profile": "a"}]


# --- resolve ------------------------------------------------------------------

def test_resolve_applies_the_active_pointer(home):
    _profiles("a", "b")
    model_tiers.save(_block(_tier("t", _entry("a", model="small"),
                                  _entry("b", model="large")),
                            active={"t": 1}))
    assert model_tiers.resolve("t") == ("b", "claude", "large")
    # The live switch: move the pointer, the next call follows.
    model_tiers.save(_block(_tier("t", _entry("a", model="small"),
                                  _entry("b", model="large")),
                            active={"t": 0}))
    assert model_tiers.resolve("t") == ("a", "claude", "small")


def test_resolve_defaults_model_and_harness(home):
    _profiles("a")
    model_tiers.save(_block(_tier("t", _entry("a"))))
    assert model_tiers.resolve("t") == ("a", "claude", "default")


def test_resolve_of_an_unknown_or_empty_tier_is_none(home):
    _profiles("a")
    model_tiers.save(_block(_tier("t", _entry("a"))))
    assert model_tiers.resolve("nope") is None


def test_resolve_survives_a_deleted_profile(home):
    _profiles("a")
    model_tiers.save(_block(_tier("t", _entry("a"))))
    # The profile dir disappears under the stored block.
    import shutil
    from claude_launcher import config
    shutil.rmtree(config.profiles_dir() / "a")
    assert model_tiers.resolve("t") is None
    assert model_tiers.tier_ids() == ["t"]


# --- storage ------------------------------------------------------------------

def test_save_replaces_the_whole_block(home):
    _profiles("a")
    model_tiers.save(_block(_tier("one", _entry("a"))))
    model_tiers.save(_block(_tier("two", _entry("a"))))
    assert model_tiers.tier_ids() == ["two"]
    doc = store.load()
    assert "model_tiers" in doc
