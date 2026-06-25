"""Read and edit a profile's ``settings.json`` (Claude Code's native settings).

Claude Code applies the ``"env"`` map in ``settings.json`` to its session, so this
module is how the launcher stores per-profile environment variables. It only
touches ``<CLAUDE_CONFIG_DIR>/settings.json`` and performs no process or network
work.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, Mapping

from .profile import Profile

SETTINGS_FILENAME = "settings.json"


def _path(profile: Profile):
    return profile.config_dir / SETTINGS_FILENAME


def load(profile: Profile) -> dict:
    """Return the parsed settings, or ``{}`` if missing/unreadable."""
    path = _path(profile)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(profile: Profile, data: dict) -> None:
    _path(profile).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def get_env(profile: Profile) -> Dict[str, str]:
    env = load(profile).get("env")
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def set_env(profile: Profile, updates: Mapping[str, str]) -> Dict[str, str]:
    """Merge ``updates`` into the profile's ``env`` and persist."""
    data = load(profile)
    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
    env.update({str(k): str(v) for k, v in updates.items()})
    data["env"] = env
    save(profile, data)
    return get_env(profile)


def merge_mcp_servers(profile: Profile, servers: Mapping[str, dict]) -> Dict[str, dict]:
    """Merge MCP server definitions into the profile's ``settings.json``."""
    data = load(profile)
    existing = data.get("mcpServers")
    if not isinstance(existing, dict):
        existing = {}
    existing.update(servers)
    data["mcpServers"] = existing
    save(profile, data)
    return existing


def replace_env(profile: Profile, env: Mapping[str, str]) -> Dict[str, str]:
    """Set the profile's ``env`` to exactly ``env`` (authoritative sync)."""
    data = load(profile)
    data["env"] = {str(k): str(v) for k, v in env.items()}
    save(profile, data)
    return get_env(profile)


def unset_env(profile: Profile, keys: Iterable[str]) -> Dict[str, str]:
    """Remove ``keys`` from the profile's ``env`` and persist."""
    data = load(profile)
    env = data.get("env")
    if isinstance(env, dict):
        for key in keys:
            env.pop(key, None)
        data["env"] = env
        save(profile, data)
    return get_env(profile)
