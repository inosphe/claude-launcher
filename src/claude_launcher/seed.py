"""Seed a new profile from Claude Code's existing global configuration.

A profile is a fresh, empty ``CLAUDE_CONFIG_DIR``, so Claude Code treats every
new profile as a first run and replays onboarding/landing. To avoid that, we copy
the user's global config into the profile — minus account- and project-specific
data, so profiles stay isolated (each still logs in with its own setup-token).

Carried over: onboarding flags (``hasCompletedOnboarding`` etc.), UI prefs,
migration markers, and the global ``settings.json``.
Stripped: ``oauthAccount``, ``projects`` and any cached API-key responses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from . import config
from .profile import Profile

CONFIG_FILENAME = ".claude.json"
SETTINGS_FILENAME = "settings.json"

#: Keys that would leak identity/history across profiles — never copied.
_EXCLUDED_KEYS = frozenset(
    {"oauthAccount", "projects", "customApiKeyResponses"}
)


def _source_config_file(source_dir: Path) -> Optional[Path]:
    """Locate the global ``.claude.json`` (config dir first, then HOME)."""
    candidates = [source_dir / CONFIG_FILENAME, Path.home() / CONFIG_FILENAME]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _seed_config(source_dir: Path, profile: Profile) -> bool:
    src = _source_config_file(source_dir)
    if src is None:
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in _EXCLUDED_KEYS}
    dest = profile.config_dir / CONFIG_FILENAME
    dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def _seed_settings(source_dir: Path, profile: Profile) -> bool:
    src = source_dir / SETTINGS_FILENAME
    if not src.is_file():
        return False
    dest = profile.config_dir / SETTINGS_FILENAME
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def seed_profile(profile: Profile, source_dir: Optional[Path] = None) -> List[str]:
    """Seed ``profile`` from the global config; return the files copied."""
    source_dir = source_dir or config.seed_source_dir()
    copied: List[str] = []
    if _seed_config(source_dir, profile):
        copied.append(CONFIG_FILENAME)
    if _seed_settings(source_dir, profile):
        copied.append(SETTINGS_FILENAME)
    return copied
