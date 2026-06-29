"""Profile inheritance: a profile may declare a parent and inherit from it.

A child profile (e.g. ``company_work``) names a parent (``company``) in the
central config file (``~/.claunch.yaml``, see :mod:`store`). At launch the child
inherits the parent's environment variables (child keys win) and, when it has no
token of its own, the parent's login token — so you can log in once on a parent
and share it across several working profiles.

This module owns chain resolution and reads/writes the ``parent`` field via the
store. It performs no subprocess or network work.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import credentials, profile as profile_mod, settings, store
from .profile import Profile


class LineageError(Exception):
    """Raised for missing parents or parent cycles."""


def get_parent(profile: Profile) -> Optional[str]:
    parent = store.profile_entry(profile.name).get("parent")
    return str(parent) if parent else None


def _parent_profile(profile: Profile) -> Optional[Profile]:
    name = get_parent(profile)
    if not name:
        return None
    parent = profile_mod.resolve(name)
    return parent if parent.exists() else None


def chain(profile: Profile) -> List[Profile]:
    """Profiles from the root ancestor down to ``profile`` (self last)."""
    items: List[Profile] = []
    seen = set()
    current: Optional[Profile] = profile
    while current is not None:
        if current.name in seen:
            raise LineageError(f"parent cycle detected at {current.name!r}")
        seen.add(current.name)
        items.append(current)
        current = _parent_profile(current)
    items.reverse()
    return items


def _ancestors_nearest_first(profile: Profile) -> List[Profile]:
    return list(reversed(chain(profile)[:-1]))


def descendants(profile: Profile) -> List[Profile]:
    """All profiles that have ``profile`` somewhere in their parent chain."""
    out: List[Profile] = []
    for candidate in profile_mod.list_all():
        if candidate.name == profile.name:
            continue
        try:
            if any(a.name == profile.name for a in chain(candidate)):
                out.append(candidate)
        except LineageError:
            continue
    return out


def set_parent(profile: Profile, parent_name: str) -> None:
    """Point ``profile`` at ``parent_name`` (must exist, no cycles)."""
    parent = profile_mod.require(parent_name)
    if parent.name == profile.name:
        raise LineageError("a profile cannot be its own parent")
    if profile.name in {p.name for p in chain(parent)}:
        raise LineageError(
            f"setting parent {parent_name!r} would create a cycle"
        )
    store.set_profile_field(profile.name, "parent", parent.name)


def clear_parent(profile: Profile) -> None:
    store.set_profile_field(profile.name, "parent", None)


def effective_env(profile: Profile) -> Dict[str, str]:
    """Env vars merged from root ancestor down to the profile (child wins)."""
    env: Dict[str, str] = {}
    for p in chain(profile):
        env.update(settings.get_env(p))
    return env


def injectable_token(profile: Profile) -> Optional[str]:
    """Token to inject as ``CLAUDE_CODE_OAUTH_TOKEN`` for ``run``.

    Uses the profile's own stored setup-token; if it only has native
    ``.credentials.json``, returns ``None`` so Claude Code reads (and refreshes)
    those itself; otherwise inherits the nearest ancestor's token.
    """
    own_stored = credentials.stored_token(profile)
    if own_stored:
        return own_stored
    if credentials.has_own_credentials(profile):
        return None
    for ancestor in _ancestors_nearest_first(profile):
        token = credentials.own_token(ancestor)
        if token:
            return token
    return None


def resolve_token(profile: Profile):
    """Return ``(token, has_profile_scope)`` for the profile (own first, then up).

    Setup-tokens carry no scope metadata and lack ``user:profile``, so they are
    reported as unscoped; ``.credentials.json`` tokens are scoped per their file.
    """
    for p in [profile, *_ancestors_nearest_first(profile)]:
        stored = credentials.stored_token(p)
        if stored:
            return stored, False
        creds = credentials.credentials_token(p)
        if creds:
            return creds, "user:profile" in (credentials.scopes(p) or [])
    return None, False


def lookup_token(profile: Profile) -> Optional[str]:
    """Any usable token for the profile (own first, then inherited)."""
    return resolve_token(profile)[0]


def login_state(profile: Profile) -> str:
    """Display state: ``"ok"``, ``"expired"``, ``"inherited"`` or ``"none"``."""
    own = credentials.token_state(profile)
    if own != "none":
        return own
    for ancestor in _ancestors_nearest_first(profile):
        if credentials.token_state(ancestor) in ("ok", "expired"):
            return "inherited"
    return "none"
