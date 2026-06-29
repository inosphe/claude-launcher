"""Central paths and environment knobs for claude-launcher.

Everything that resolves a filesystem location or reads an environment override
lives here, so the rest of the package never touches ``os.environ`` directly.
"""

from __future__ import annotations

import os
from pathlib import Path

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


def seed_source_dir() -> Path:
    """Config dir a freshly created profile copies its global settings from."""
    override = os.environ.get(LAUNCHER_SEED_ENV)
    return Path(override).expanduser() if override else default_config_dir()


def sync_file() -> Path:
    """YAML file that ``export``/``import`` read and write (default ``~/.claunch.yaml``)."""
    override = os.environ.get(LAUNCHER_SYNC_ENV)
    return Path(override).expanduser() if override else Path.home() / ".claunch.yaml"

