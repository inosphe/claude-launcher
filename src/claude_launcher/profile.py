"""Profile model and on-disk registry.

A *profile* is just a named directory used as ``CLAUDE_CONFIG_DIR``. This module
owns creating, listing, resolving and deleting those directories. It performs no
subprocess work and knows nothing about ``claude`` itself.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List

from . import config

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProfileError(Exception):
    """Raised for invalid profile names or missing/duplicate profiles."""


@dataclass(frozen=True)
class Profile:
    """A named, isolated Claude Code configuration directory."""

    name: str
    config_dir: Path

    def exists(self) -> bool:
        return self.config_dir.is_dir()


def _validate_name(name: str) -> str:
    name = name.strip()
    if not name or not _NAME_RE.match(name):
        raise ProfileError(
            f"invalid profile name {name!r}: use letters, digits, '.', '_' or '-'"
        )
    return name


def resolve(name: str) -> Profile:
    """Return the :class:`Profile` for ``name`` without touching the disk."""
    name = _validate_name(name)
    return Profile(name=name, config_dir=config.profiles_dir() / name)


def create(name: str) -> Profile:
    """Create a profile directory and register it in the store."""
    from . import store

    profile = resolve(name)
    if profile.exists():
        raise ProfileError(f"profile {profile.name!r} already exists")
    profile.config_dir.mkdir(parents=True, exist_ok=False)
    store.ensure_profile(profile.name)
    return profile


def require(name: str) -> Profile:
    """Return an existing profile or raise."""
    profile = resolve(name)
    if not profile.exists():
        raise ProfileError(
            f"profile {profile.name!r} does not exist (create it with 'claunch create {profile.name}')"
        )
    return profile


def remove(name: str) -> Profile:
    """Delete a profile directory and its store entry."""
    from . import store

    profile = require(name)
    shutil.rmtree(profile.config_dir)
    store.remove_profile(profile.name)
    return profile


def list_all() -> List[Profile]:
    """Return all profiles, sorted by name."""
    root = config.profiles_dir()
    if not root.is_dir():
        return []
    profiles = [
        Profile(name=child.name, config_dir=child)
        for child in root.iterdir()
        if child.is_dir()
    ]
    return sorted(profiles, key=lambda p: p.name)
