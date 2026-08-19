"""Central paths and environment knobs for claude-launcher.

Everything that resolves a filesystem location or reads an environment override
lives here, so the rest of the package never touches ``os.environ`` directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: Environment variable Claude Code reads to locate its config/credentials.
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

#: Override the base directory that holds all profiles.
LAUNCHER_HOME_ENV = "CLAUDE_LAUNCHER_HOME"

#: Override the ``claude`` executable name/path.
LAUNCHER_BIN_ENV = "CLAUDE_LAUNCHER_BIN"

#: Override the endpoint used by ``claunch usage``.
LAUNCHER_USAGE_URL_ENV = "CLAUDE_LAUNCHER_USAGE_URL"

#: Override the config dir new profiles are seeded from.
LAUNCHER_SEED_ENV = "CLAUDE_LAUNCHER_SEED"

#: Override the YAML file used by ``claunch export`` / ``import`` (also holds
#: provider definitions and selection).
LAUNCHER_SYNC_ENV = "CLAUDE_LAUNCHER_SYNC_FILE"

#: Server side: directory holding the sync server's documents and user list.
SYNC_SERVER_DIR_ENV = "CLAUNCH_SYNC_SERVER_DIR"

#: Client side: overrides for the ``sync:`` block in the config file. The token
#: especially is a secret, so an env var is the preferred place for it.
SYNC_URL_ENV = "CLAUNCH_SYNC_URL"
SYNC_TOKEN_ENV = "CLAUNCH_SYNC_TOKEN"
SYNC_NAMESPACE_ENV = "CLAUNCH_SYNC_NAMESPACE"

_DEFAULT_HOME = Path.home() / ".claude-launcher"


def launcher_home() -> Path:
    """Base directory holding launcher state (profiles, metadata)."""
    override = os.environ.get(LAUNCHER_HOME_ENV)
    return Path(override).expanduser() if override else _DEFAULT_HOME


def profiles_dir() -> Path:
    """Directory under which each profile gets its own ``CLAUDE_CONFIG_DIR``."""
    return launcher_home() / "profiles"


def claude_bin() -> str:
    """Name or path of the ``claude`` executable to invoke."""
    return os.environ.get(LAUNCHER_BIN_ENV, "claude")


def usage_url() -> str:
    """Endpoint queried for subscription usage / rate-limit info."""
    return os.environ.get(
        LAUNCHER_USAGE_URL_ENV,
        "https://api.anthropic.com/api/oauth/usage",
    )


def usage_model() -> str:
    """Model for the minimal call that reads rate-limit headers (setup-token fallback)."""
    return os.environ.get("CLAUDE_LAUNCHER_USAGE_MODEL", "claude-haiku-4-5-20251001")


def default_config_dir() -> Path:
    """Claude Code's default config dir (the source for seeding new profiles)."""
    override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claude"


def user_claude_json() -> Path:
    """Claude Code's user-scope config file (where ``mcpServers`` live).

    With ``CLAUDE_CONFIG_DIR`` set it sits inside that directory; without it,
    Claude Code reads ``~/.claude.json`` — a *sibling* of ``~/.claude``, not a
    file inside it. Writing ``~/.claude/.claude.json`` in the default setup
    would be silently ignored, which is why this is not simply
    ``default_config_dir() / ".claude.json"``.
    """
    override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def seed_source_dir() -> Path:
    """Config dir a freshly created profile copies its global settings from."""
    override = os.environ.get(LAUNCHER_SEED_ENV)
    return Path(override).expanduser() if override else default_config_dir()


def sync_file() -> Path:
    """YAML file that ``export``/``import`` read and write (default ``~/.claunch.yaml``)."""
    override = os.environ.get(LAUNCHER_SYNC_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claunch.yaml"


def sync_base_file() -> Path:
    """Snapshot of the last state this machine agreed on with the sync server.

    Kept out of the config file itself: it is per-machine bookkeeping (the base
    of the three-way merge), not configuration.
    """
    return launcher_home() / "sync-base.yaml"


def sync_server_dir() -> Path:
    """Where ``claunch sync-server`` keeps its documents and user list."""
    override = os.environ.get(SYNC_SERVER_DIR_ENV)
    return Path(override).expanduser() if override else launcher_home() / "sync-server"


def sync_url() -> Optional[str]:
    """Env override for the sync server URL (``None`` if unset)."""
    return os.environ.get(SYNC_URL_ENV) or None


def sync_token() -> Optional[str]:
    """Env override for the sync auth token (``None`` if unset)."""
    return os.environ.get(SYNC_TOKEN_ENV) or None


def sync_namespace() -> Optional[str]:
    """Env override for the synced document's namespace (``None`` if unset)."""
    return os.environ.get(SYNC_NAMESPACE_ENV) or None

