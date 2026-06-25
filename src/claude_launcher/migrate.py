"""Migrate skills and MCP servers from a global or local path into a profile.

Seeding copies the global ``settings.json`` (so its ``mcpServers`` come along),
but **skills live in a separate ``skills/`` directory** and **project/local MCP
servers live outside ``settings.json``**, so they are not seeded. This module
pulls those into a profile's ``CLAUDE_CONFIG_DIR`` from a source path that is
either a Claude config dir (e.g. ``~/.claude`` or another profile) or a project
directory (with ``.claude/`` and/or ``.mcp.json``).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from . import config, settings
from .profile import Profile


class MigrateError(Exception):
    """Raised for an unusable migration source."""


@dataclass
class MigrateResult:
    skills: List[str] = field(default_factory=list)
    mcp_servers: List[str] = field(default_factory=list)
    plugins: bool = False


def _skill_source_dirs(source: Path) -> List[Path]:
    """Skill directories to merge from, for a config dir or a project dir."""
    return [d for d in (source / "skills", source / ".claude" / "skills") if d.is_dir()]


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _collect_mcp_servers(source: Path) -> Dict[str, dict]:
    """Gather MCP server definitions from the usual files under ``source``."""
    servers: Dict[str, dict] = {}
    candidates = [
        source / "settings.json",
        source / "settings.local.json",
        source / ".claude" / "settings.json",
        source / ".claude" / "settings.local.json",
        source / ".claude.json",
    ]
    for path in candidates:
        if path.is_file():
            block = _load_json(path).get("mcpServers")
            if isinstance(block, dict):
                servers.update(block)
    # Project-root .mcp.json: either {"mcpServers": {...}} or a bare mapping.
    mcp_json = source / ".mcp.json"
    if mcp_json.is_file():
        data = _load_json(mcp_json)
        block = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
        if isinstance(block, dict):
            servers.update(block)
    return servers


def _copy_skills(source: Path, profile: Profile, dry_run: bool) -> List[str]:
    copied: List[str] = []
    dest_root = profile.config_dir / "skills"
    for skills_dir in _skill_source_dirs(source):
        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            copied.append(child.name)
            if not dry_run:
                dest_root.mkdir(parents=True, exist_ok=True)
                shutil.copytree(child, dest_root / child.name, dirs_exist_ok=True)
    return copied


def _copy_plugins(source: Path, profile: Profile, dry_run: bool) -> bool:
    src = source / "plugins"
    if not src.is_dir():
        return False
    if not dry_run:
        shutil.copytree(src, profile.config_dir / "plugins", dirs_exist_ok=True)
    return True


def migrate(
    profile: Profile,
    source: Path,
    *,
    skills: bool = True,
    mcp: bool = True,
    plugins: bool = False,
    dry_run: bool = False,
) -> MigrateResult:
    """Copy the selected pieces from ``source`` into ``profile``."""
    if not source.exists():
        raise MigrateError(f"migration source does not exist: {source}")
    result = MigrateResult()
    if skills:
        result.skills = _copy_skills(source, profile, dry_run)
    if mcp:
        servers = _collect_mcp_servers(source)
        result.mcp_servers = sorted(servers)
        if servers and not dry_run:
            settings.merge_mcp_servers(profile, servers)
    if plugins:
        result.plugins = _copy_plugins(source, profile, dry_run)
    return result


def default_source() -> Path:
    """The global Claude config dir, used when no source path is given."""
    return config.default_config_dir()
