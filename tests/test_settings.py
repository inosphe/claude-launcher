"""Per-profile env now lives in the store; mcpServers stays in settings.json."""

from __future__ import annotations

import json

from claude_launcher import profile, settings, store


def test_env_roundtrip_via_store(home):
    p = profile.create("work")
    assert settings.get_env(p) == {}
    settings.set_env(p, {"A": "1", "B": "2"})
    assert settings.get_env(p) == {"A": "1", "B": "2"}
    # It is persisted in the central config file, not the profile dir.
    assert store.profile_entry("work")["env"] == {"A": "1", "B": "2"}
    assert not (p.config_dir / "settings.json").exists()


def test_set_env_merges(home):
    p = profile.create("work")
    settings.set_env(p, {"A": "1"})
    settings.set_env(p, {"B": "2"})
    assert settings.get_env(p) == {"A": "1", "B": "2"}


def test_replace_env_is_authoritative(home):
    p = profile.create("work")
    settings.set_env(p, {"A": "1"})
    settings.replace_env(p, {"C": "3"})
    assert settings.get_env(p) == {"C": "3"}


def test_unset_env(home):
    p = profile.create("work")
    settings.set_env(p, {"A": "1", "B": "2"})
    settings.unset_env(p, ["A"])
    assert settings.get_env(p) == {"B": "2"}


def test_values_coerced_to_str(home):
    p = profile.create("work")
    settings.set_env(p, {"N": 5})
    assert settings.get_env(p) == {"N": "5"}


def test_merge_mcp_servers_writes_native_settings(home):
    p = profile.create("work")
    settings.merge_mcp_servers(p, {"srv": {"command": "x"}})
    data = json.loads((p.config_dir / "settings.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"] == {"command": "x"}
    # mcp config must NOT leak into the env store.
    assert "mcpServers" not in store.profile_entry("work")
