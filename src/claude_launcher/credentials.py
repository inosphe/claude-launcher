"""The single launcher-managed token and native Claude login for a profile.

``claude setup-token`` prints a long-lived OAuth token but does **not** write it
into the config dir — Claude Code consumes it via the ``CLAUDE_CODE_OAUTH_TOKEN``
environment variable. So the launcher captures that token at login and stores it
inside the profile (``<CLAUDE_CONFIG_DIR>/.launcher-token``, ``0600``).

Harness declarations decide how the one launcher token is projected at
process launch. Claude token lookup also falls back to a ``.credentials.json``
written by an interactive ``/login``; that native OAuth file is not projected
into other harnesses.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Optional

from . import atomic
from .profile import Profile

#: The one launcher-managed profile token (setup-token or provider/API token).
TOKEN_FILENAME = ".launcher-token"
#: Transitional filename written by the short-lived ``set-key`` design. It is
#: never a runtime source; bootstrap only moves it when no canonical token
#: exists, preserving a safe upgrade without keeping two credential stores.
LEGACY_API_KEY_FILENAME = ".launcher-api-key"
#: Credentials file an interactive ``/login`` writes (fallback source).
CREDENTIALS_FILENAME = ".credentials.json"


class CredentialsError(Exception):
    """Raised when profile credential storage cannot satisfy a request."""


def _token_path(profile: Profile):
    return profile.config_dir / TOKEN_FILENAME


def _save_secret(path, value: str, what: str) -> None:
    value = value.strip()
    if not value:
        raise CredentialsError(f"refusing to store an empty {what}")
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (best effort on Windows)
    except OSError:
        pass


def save_token(profile: Profile, token: str) -> None:
    """Persist a setup-token for ``profile`` with owner-only permissions."""
    _save_secret(_token_path(profile), token, "token")


def migrate_legacy_api_key(profile: Profile) -> bool:
    """Move an unambiguous old ``set-key`` file to the single token path.

    If both files exist, the canonical token wins and the old file is left in
    place for manual review; silently discarding either different secret would
    be an unsafe migration.
    """
    target = _token_path(profile)
    legacy = profile.config_dir / LEGACY_API_KEY_FILENAME
    if target.exists() or not legacy.is_file():
        return False
    atomic.replace(legacy, target)
    return True


def stored_token(profile: Profile) -> Optional[str]:
    """Return the launcher-stored setup-token, or ``None`` if absent."""
    path = _token_path(profile)
    if not path.is_file():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def _credentials_access_token(profile: Profile) -> Optional[str]:
    """Read an access token from a ``/login``-written ``.credentials.json``."""
    path = profile.config_dir / CREDENTIALS_FILENAME
    if not path.is_file():
        return None
    try:
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(oauth, dict):
        return oauth.get("accessToken")
    return None


def access_token(profile: Profile) -> str:
    """Best available OAuth token for the profile (stored first, then login)."""
    token = stored_token(profile) or _credentials_access_token(profile)
    if not token:
        raise CredentialsError(
            f"no token for profile {profile.name!r}; run 'claunch login {profile.name}' first"
        )
    return token


def credentials_token(profile: Profile) -> Optional[str]:
    """The access token from a ``/login`` ``.credentials.json`` (or ``None``)."""
    return _credentials_access_token(profile)


def scopes(profile: Profile) -> Optional[list]:
    """OAuth scopes recorded in ``.credentials.json`` (``None`` for setup-tokens)."""
    path = profile.config_dir / CREDENTIALS_FILENAME
    if not path.is_file():
        return None
    try:
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, json.JSONDecodeError):
        return None
    return oauth.get("scopes") if isinstance(oauth, dict) else None


def own_token(profile: Profile) -> Optional[str]:
    """The profile's own token (stored setup-token first, then login creds)."""
    return stored_token(profile) or _credentials_access_token(profile)


def has_own_credentials(profile: Profile) -> bool:
    """Whether the profile has a ``/login``-written ``.credentials.json`` token."""
    return _credentials_access_token(profile) is not None


def has_token(profile: Profile) -> bool:
    """Whether the profile has any usable token (stored or from login)."""
    return own_token(profile) is not None


def token_state(profile: Profile) -> str:
    """Coarse login state for display: ``"ok"``, ``"expired"`` or ``"none"``.

    A launcher-stored setup-token has no expiry metadata, so it always reads as
    ``"ok"``. A ``/login`` ``.credentials.json`` is checked against its
    ``expiresAt`` timestamp.
    """
    if stored_token(profile) is not None:
        return "ok"
    path = profile.config_dir / CREDENTIALS_FILENAME
    if not path.is_file():
        return "none"
    try:
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, json.JSONDecodeError):
        return "none"
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return "none"
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at <= int(time.time() * 1000):
        return "expired"
    return "ok"
