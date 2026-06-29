"""The store is the single source of truth: read/write of ~/.claunch.yaml."""

from __future__ import annotations

import pytest
import yaml

from claude_launcher import store


def test_load_defaults_when_absent(config_file):
    assert not config_file.exists()
    doc = store.load()
    assert doc["version"] == store.VERSION
    # Reading must not create the file.
    assert not config_file.exists()


def test_save_then_load_roundtrip(config_file):
    store.save({"profiles": {"a": {"env": {"X": "1"}}}})
    assert config_file.exists()
    doc = store.load()
    assert doc["version"] == store.VERSION
    assert doc["profiles"]["a"]["env"] == {"X": "1"}


def test_set_profile_field_sets_and_clears(home):
    store.set_profile_field("work", "parent", "base")
    assert store.profile_entry("work") == {"parent": "base"}
    # Empty/None clears the field but keeps the entry.
    store.set_profile_field("work", "parent", None)
    assert store.profile_entry("work") == {}


def test_ensure_profile_registers_empty_entry(home):
    store.ensure_profile("solo")
    assert "solo" in store.profiles()
    assert store.profile_entry("solo") == {}
    # Idempotent: does not clobber existing config.
    store.set_profile_field("solo", "env", {"A": "1"})
    store.ensure_profile("solo")
    assert store.profile_entry("solo")["env"] == {"A": "1"}


def test_remove_profile(home):
    store.ensure_profile("gone")
    store.remove_profile("gone")
    assert "gone" not in store.profiles()


def test_template_env_helpers(home):
    assert store.template_env() == {}
    store.set_template_env({"K": "v"})
    assert store.template_env() == {"K": "v"}


def test_malformed_file_raises_not_overwrites(config_file):
    config_file.write_text("{ this: is: not: valid", encoding="utf-8")
    original = config_file.read_text(encoding="utf-8")
    # A present-but-unparseable file must raise, never be silently treated as
    # empty (which the next write would clobber).
    with pytest.raises(store.StoreError):
        store.load()
    assert config_file.read_text(encoding="utf-8") == original


def test_non_mapping_top_level_raises(config_file):
    config_file.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load()


def test_empty_file_is_ok(config_file):
    config_file.write_text("", encoding="utf-8")
    assert store.load()["version"] == store.VERSION


def test_save_is_stable_yaml(config_file):
    store.save({"profiles": {"b": {}, "a": {}}})
    text = config_file.read_text(encoding="utf-8")
    # sort_keys=True keeps a deterministic order.
    assert text.index("a:") < text.index("b:")
    assert yaml.safe_load(text)["version"] == store.VERSION
