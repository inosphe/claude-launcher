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
from pathlib import Path
from typing import Dict, Iterable, List, Mapping

from . import store
from .profile import Profile

SETTINGS_FILENAME = "settings.json"

#: The values ``permissions.defaultMode`` accepts, in the words the installed
#: Claude Code itself rejects a wrong one with ("Valid modes: \"acceptEdits\"
#: (ask before file changes), \"plan\" (analysis only), \"bypassPermissions\"
#: (auto-accept all), or \"default\" (standard behavior)"). ``manual`` is
#: accepted as an alias for ``default``, and ``auto`` is the classifier-backed
#: mode this project's own sessions run in -- all three appear here because all
#: three are accepted on the wire, and a validator that named fewer would
#: refuse a value the harness honours.
#:
#: This list is a gate, not documentation: it is what stops a typo
#: (``permisions.defaultMode``, ``accept-edits``) from being converged into
#: every profile as a key Claude Code then ignores. Keep it beside the file it
#: describes rather than at a caller, so the CLI, the daemon and the web form
#: cannot drift apart on which values exist.
PERMISSION_MODES = (
    "default",
    "manual",
    "acceptEdits",
    "plan",
    "auto",
    "bypassPermissions",
)


def is_permission_mode(value: object) -> bool:
    """Whether ``value`` is one of :data:`PERMISSION_MODES`."""
    return str(value) in PERMISSION_MODES


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


def _merge_permission_rules(path: Path, key: str, rules: Iterable[str]) -> bool:
    """Append ``rules`` to ``permissions[key]``; True when the file changed.

    Shared by :func:`merge_permission_deny` and :func:`merge_permission_allow`
    so the two cannot drift: both create the file (and parents) when missing,
    both append only what is absent, and both leave a file whose
    ``permissions`` or rule list is some other shape exactly as they found it.
    That last half is the load-bearing one. A rule list is the user's, and a
    guard is not worth clobbering whatever they meant.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    perms = doc.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return False
    present = perms.setdefault(key, [])
    if not isinstance(present, list):
        return False
    missing = [rule for rule in rules if rule not in present]
    if not missing:
        return False
    present.extend(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return True


def merge_permission_deny(path: Path, rules: Iterable[str]) -> bool:
    """Append ``rules`` to a Claude Code settings file's ``permissions.deny``.

    Creates the file (and parents) when missing; returns True when the file
    changed. Only ever appends what is absent — the user's own permission
    edits (allow lists included) survive a reinstall untouched. A settings
    file whose ``permissions``/``deny`` is some other shape is left alone
    rather than repaired: it is the user's file, and a guard is not worth
    clobbering whatever they meant.
    """
    return _merge_permission_rules(path, "deny", rules)


def merge_permission_allow(path: Path, rules: Iterable[str]) -> bool:
    """Append ``rules`` to a Claude Code settings file's ``permissions.allow``.

    The other half of :func:`merge_permission_deny`, and the same shape on
    purpose: a **union**, never a replacement. An allow list is something a
    person grows, so an entry already there stays and a rule already there is
    not duplicated.

    This is why the claunch MCP rule is planted here rather than declared as a
    shared settings key: ``claunch apply`` converges a declared value through
    :func:`dotted_set`, which assigns the leaf — a declared
    ``permissions.allow`` would replace the whole list and silently drop
    whatever the person had added. :func:`merge_permission_deny` has always
    promised the opposite for the neighbouring key; this keeps the promise for
    this one too.
    """
    return _merge_permission_rules(path, "allow", rules)


def dotted_get(doc: Mapping, key: str):
    """The value at a dotted ``key`` (``permissions.defaultMode``), or ``None``.

    Absent is reported as ``None`` at every step: a missing intermediate
    mapping, a missing leaf, and a leaf that happens to hold JSON ``null``
    are one answer here, which is what a caller comparing against a declared
    value wants. ``None`` is never a value claunch declares, so the three
    cannot be told apart and do not need to be.
    """
    node = doc
    for part in _dotted_parts(key):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def dotted_set(doc: dict, key: str, value) -> bool:
    """Write ``value`` at a dotted ``key`` inside ``doc``, merging the path.

    Returns True when ``doc`` holds the value afterwards, False when the
    write was refused. Sibling keys under a shared parent survive — writing
    ``permissions.defaultMode`` is the reason this exists at all, because the
    plain assignment it replaces would drop ``permissions.deny`` (the gate
    guard) on the floor.

    A step that exists but is not a mapping is refused rather than replaced,
    the same call :func:`merge_permission_deny` makes: the settings file is
    the user's, and one key claunch converges is not worth overwriting
    whatever shape they wrote.
    """
    parts = _dotted_parts(key)
    if not parts:
        return False
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if nxt is None:
            nxt = {}
            node[part] = nxt
        if not isinstance(nxt, dict):
            return False
        node = nxt
    last = parts[-1]
    if node.get(last) == value:
        return True
    node[last] = value
    return True


def _dotted_parts(key: str) -> List[str]:
    """``"permissions.defaultMode"`` -> ``["permissions", "defaultMode"]``."""
    return [part for part in str(key).split(".") if part]


CLAUDE_JSON = ".claude.json"


def merge_mcp_servers(
    profile: Profile,
    servers: Mapping[str, dict],
    remove: Iterable[str] = (),
) -> Dict[str, dict]:
    """Merge MCP servers into the profile's user-scope config (``.claude.json``).

    Claude Code silently ignores ``mcpServers`` in ``settings.json``; the
    user-scope location inside ``CLAUDE_CONFIG_DIR`` is ``.claude.json``.
    Same-named entries that earlier launcher versions wrote into
    ``settings.json`` are dropped so no dead config lingers (other entries
    there are left untouched — they are not ours).

    ``remove`` names servers a *previous* launcher version registered and this
    one supersedes. Without it an upgrade would leave the old entries running
    alongside the new one, and the agent would see every tool twice — the
    superseded copies are not stale config, they are a working server that has
    to be switched off deliberately.
    """
    existing = merge_mcp_servers_into(profile.config_dir / CLAUDE_JSON, servers, remove)
    _drop_stale_settings_servers(profile, [*servers, *remove])
    return existing


def merge_mcp_servers_into(
    path: Path,
    servers: Mapping[str, dict],
    remove: Iterable[str] = (),
) -> Dict[str, dict]:
    """Merge MCP servers into one ``.claude.json``-shaped file.

    The path-level half of :func:`merge_mcp_servers`, shared with the global
    (user-scope) install, whose target is not a profile at all — the default
    setup keeps it at ``~/.claude.json``, a sibling of ``~/.claude``.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    existing = doc.get("mcpServers")
    if not isinstance(existing, dict):
        existing = {}
    for name in remove:
        existing.pop(name, None)
    existing.update(servers)
    doc["mcpServers"] = existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return existing


def _drop_stale_settings_servers(profile: Profile, names: Iterable[str]) -> None:
    data = load(profile)
    stale = data.get("mcpServers")
    if not isinstance(stale, dict):
        return
    hit = False
    for name in names:
        if name in stale:
            del stale[name]
            hit = True
    if not hit:
        return
    if not stale:
        del data["mcpServers"]
    save(profile, data)
