"""Per-profile environment variables, plus the profile's native ``settings.json``.

A profile's launcher-managed ``env`` lives in the central config file
(``~/.claunch.yaml``, see :mod:`store`), not in the profile directory — the
launcher injects it into ``claude``'s process at launch. The ``get_env`` /
``set_env`` / ``replace_env`` / ``unset_env`` helpers here read and write that
central store.

``load`` / ``save`` touch the profile's own ``<CLAUDE_CONFIG_DIR>/settings.json``
(Claude Code's settings file). ``merge_mcp_servers`` is different: Claude Code
does NOT read ``mcpServers`` from ``settings.json`` — user-scope MCP servers
live in ``<CLAUDE_CONFIG_DIR>/.claude.json`` (what ``claude mcp add --scope
user`` writes), so that is where it merges.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, Mapping

from . import store
from .profile import Profile

SETTINGS_FILENAME = "settings.json"


def _path(profile: Profile):
    return profile.config_dir / SETTINGS_FILENAME


def load(profile: Profile) -> dict:
    """Return the profile's native ``settings.json``, or ``{}`` if missing."""
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
    """The profile's own launcher env (from the central store)."""
    env = store.profile_entry(profile.name).get("env")
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def set_env(profile: Profile, updates: Mapping[str, str]) -> Dict[str, str]:
    """Merge ``updates`` into the profile's env and persist to the store."""
    env = get_env(profile)
    env.update({str(k): str(v) for k, v in updates.items()})
    store.set_profile_field(profile.name, "env", env)
    return env


def replace_env(profile: Profile, env: Mapping[str, str]) -> Dict[str, str]:
    """Set the profile's env to exactly ``env`` (authoritative sync)."""
    new = {str(k): str(v) for k, v in env.items()}
    store.set_profile_field(profile.name, "env", new)
    return new


def unset_env(profile: Profile, keys: Iterable[str]) -> Dict[str, str]:
    """Remove ``keys`` from the profile's env and persist to the store."""
    env = get_env(profile)
    for key in keys:
        env.pop(key, None)
    store.set_profile_field(profile.name, "env", env)
    return env


CLAUDE_JSON = ".claude.json"


def merge_mcp_servers(profile: Profile, servers: Mapping[str, dict]) -> Dict[str, dict]:
    """Merge MCP servers into the profile's user-scope config (``.claude.json``).

    Claude Code silently ignores ``mcpServers`` in ``settings.json``; the
    user-scope location inside ``CLAUDE_CONFIG_DIR`` is ``.claude.json``.
    Same-named entries that earlier launcher versions wrote into
    ``settings.json`` are dropped so no dead config lingers (other entries
    there are left untouched — they are not ours).
    """
    path = profile.config_dir / CLAUDE_JSON
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    existing = doc.get("mcpServers")
    if not isinstance(existing, dict):
        existing = {}
    existing.update(servers)
    doc["mcpServers"] = existing
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    _drop_stale_settings_servers(profile, servers)
    return existing


def _drop_stale_settings_servers(profile: Profile, servers: Mapping[str, dict]) -> None:
    data = load(profile)
    stale = data.get("mcpServers")
    if not isinstance(stale, dict):
        return
    hit = False
    for name in servers:
        if name in stale:
            del stale[name]
            hit = True
    if not hit:
        return
    if not stale:
        del data["mcpServers"]
    save(profile, data)
