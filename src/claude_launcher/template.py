"""Default settings template applied to every new profile.

The template lives at ``<launcher home>/template.json`` and currently carries the
default ``env`` block. ``create`` applies it to new profiles; it can also be
re-applied to existing ones (``claunch env <name> --apply-template``). Edit the
file to change the defaults for future profiles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from . import config, settings
from .profile import Profile

TEMPLATE_FILENAME = "template.json"

#: Built-in defaults used until a template file is written.
DEFAULT_ENV: Dict[str, str] = {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
}


def template_path() -> Path:
    return config.launcher_home() / TEMPLATE_FILENAME


def default_template() -> dict:
    return {"env": dict(DEFAULT_ENV)}


def load() -> dict:
    """Return the template (from disk if present, else the built-in default)."""
    path = template_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return default_template()


def ensure_file() -> Path:
    """Write the default template file if it does not exist yet."""
    path = template_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default_template(), indent=2) + "\n", encoding="utf-8")
    return path


def save(data: dict) -> Path:
    """Persist a full template dict to the template file."""
    path = template_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def set_env(env_map: dict) -> Path:
    """Update only the template's ``env`` block, keeping other keys."""
    data = load()
    data["env"] = {str(k): str(v) for k, v in env_map.items()}
    return save(data)


def env() -> Dict[str, str]:
    block = load().get("env")
    return {str(k): str(v) for k, v in block.items()} if isinstance(block, dict) else {}


def apply_to(profile: Profile) -> Dict[str, str]:
    """Merge the template's env defaults into ``profile`` and return the result."""
    template_env = env()
    if template_env:
        return settings.set_env(profile, template_env)
    return settings.get_env(profile)
