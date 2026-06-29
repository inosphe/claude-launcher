"""Bootstrap: migrate legacy scattered config, then materialize declared dirs."""

from __future__ import annotations

import json

from claude_launcher import bootstrap, config, store


def _make_profile_dir(home, name):
    d = config.profiles_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_migrates_legacy_settings_env(home):
    d = _make_profile_dir(home, "old")
    (d / "settings.json").write_text(
        json.dumps({"env": {"LEGACY": "1"}, "mcpServers": {"s": {"command": "x"}}}),
        encoding="utf-8",
    )
    bootstrap.run()
    # env absorbed into the store...
    assert store.profile_entry("old")["env"] == {"LEGACY": "1"}
    # ...stripped from settings.json, but mcpServers preserved.
    data = json.loads((d / "settings.json").read_text(encoding="utf-8"))
    assert "env" not in data
    assert data["mcpServers"] == {"s": {"command": "x"}}


def test_migrates_legacy_parent_meta(home):
    _make_profile_dir(home, "base")
    child = _make_profile_dir(home, "child")
    (child / ".launcher.json").write_text(
        json.dumps({"parent": "base"}), encoding="utf-8"
    )
    bootstrap.run()
    assert store.profile_entry("child")["parent"] == "base"
    assert not (child / ".launcher.json").exists()


def test_migrates_legacy_template_json(home):
    (home / "template.json").write_text(
        json.dumps({"env": {"TPL": "x"}}), encoding="utf-8"
    )
    bootstrap.run()
    assert store.template_env() == {"TPL": "x"}
    assert not (home / "template.json").exists()


def test_migration_is_idempotent(home):
    d = _make_profile_dir(home, "old")
    (d / "settings.json").write_text(
        json.dumps({"env": {"A": "1"}}), encoding="utf-8"
    )
    bootstrap.run()
    first = store.load()
    bootstrap.run()
    assert store.load() == first


def test_legacy_live_values_win_over_stale_yaml(home, config_file):
    # An old export snapshot with a now-stale env, plus newer live settings.json.
    store.save({"version": 1, "profiles": {"old": {"env": {"K": "STALE"}}}})
    d = _make_profile_dir(home, "old")
    (d / "settings.json").write_text(
        json.dumps({"env": {"K": "FRESH"}}), encoding="utf-8"
    )
    bootstrap.run()
    # The live value must win; the stale snapshot must not be kept.
    assert store.profile_entry("old")["env"] == {"K": "FRESH"}


def test_migration_preserves_provider_selection(home, config_file):
    store.save({"version": 1, "profiles": {"old": {"provider": "glm"}}})
    d = _make_profile_dir(home, "old")
    (d / "settings.json").write_text(
        json.dumps({"env": {"K": "v"}}), encoding="utf-8"
    )
    bootstrap.run()
    entry = store.profile_entry("old")
    assert entry["provider"] == "glm"
    assert entry["env"] == {"K": "v"}


def test_token_only_profile_is_registered(home):
    # A legitimate profile with a token but no env/parent must still land in the
    # store, so prune does not treat it as an orphan.
    d = _make_profile_dir(home, "tok")
    (d / ".launcher-token").write_text("sk-ant-oat01-X\n", encoding="utf-8")
    bootstrap.run()
    assert "tok" in store.profiles()


def test_migration_runs_once_via_marker(home):
    d = _make_profile_dir(home, "p")
    (d / "settings.json").write_text(json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    bootstrap.run()
    # Simulate the user later removing the entry by hand; the marker stops the
    # migration from re-registering it (which would defeat prune).
    store.remove_profile("p")
    bootstrap.run()
    assert "p" not in store.profiles()


def test_reconcile_materializes_declared_profile(home, config_file):
    # A config naming a profile with no directory (e.g. copied from elsewhere).
    store.save({"version": 1, "profiles": {"copied": {"env": {"A": "1"}}}})
    assert not (config.profiles_dir() / "copied").is_dir()
    bootstrap.run()
    assert (config.profiles_dir() / "copied").is_dir()
    # Config survives materialization.
    assert store.profile_entry("copied")["env"] == {"A": "1"}


def test_clean_install_noop(home, config_file):
    # No legacy files, no declared profiles: bootstrap does nothing surprising.
    bootstrap.run()
    assert store.profiles() == {}
