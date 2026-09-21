"""Profile model and on-disk registry.

A *profile* is a named storage root. Claude Code uses it directly as
``CLAUDE_CONFIG_DIR``; other harnesses use namespaced children selected by
their own home environment variable. This module only owns the roots.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProfileError(Exception):
    """Raised for invalid profile names or missing/duplicate profiles."""


@dataclass(frozen=True)
class Profile:
    """A named, isolated harness storage root."""

    name: str
    config_dir: Path
    # An execution-only harness choice parsed from ``NAME:HARNESS``.  It is
    # deliberately not part of ``config_dir``: every selector for a profile
    # shares the profile's one storage root and launcher-managed token.
    harness_override: Optional[str] = None

    def exists(self) -> bool:
        return self.config_dir.is_dir()

    @property
    def selector(self) -> str:
        """The stable user-facing selector for this resolved profile."""
        if self.harness_override:
            return f"{self.name}:{self.harness_override}"
        return self.name


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


def split_selector(value: str) -> Tuple[str, Optional[str]]:
    """Split ``PROFILE[:HARNESS]`` without allowing it into a disk path."""
    value = str(value or "").strip()
    if ":" not in value:
        return _validate_name(value), None
    parts = value.split(":")
    if len(parts) != 2:
        raise ProfileError(
            f"invalid profile selector {value!r}: use PROFILE or PROFILE:HARNESS"
        )
    profile_name = _validate_name(parts[0])
    harness_name = _validate_name(parts[1])
    return profile_name, harness_name


def resolve_selector(value: str) -> Profile:
    """Resolve ``PROFILE[:HARNESS]`` while keeping storage at ``PROFILE``."""
    name, harness_override = split_selector(value)
    return Profile(
        name=name,
        config_dir=config.profiles_dir() / name,
        harness_override=harness_override,
    )


def create(name: str) -> Profile:
    """Create a profile directory and register it in the store."""
    from . import store

    profile = resolve(name)
    if profile.exists():
        # The directory can exist without the store entry this call would add:
        # the write below is what a Windows sharing conflict refuses, and it
        # runs *after* the mkdir. Point at the path that finishes that setup
        # rather than at `prune`, which deletes the directory instead.
        raise ProfileError(
            f"profile {profile.name!r} already exists "
            f"(re-run its setup with 'claunch create {profile.name} --reinit')"
        )
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


def require_selector(value: str) -> Profile:
    """Return an existing profile selected as ``PROFILE[:HARNESS]``."""
    profile = resolve_selector(value)
    if not profile.exists():
        raise ProfileError(
            f"profile {profile.name!r} does not exist "
            f"(create it with 'claunch create {profile.name}')"
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
