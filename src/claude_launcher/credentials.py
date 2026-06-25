"""Token storage and lookup for a profile.

``claude setup-token`` prints a long-lived OAuth token but does **not** write it
into the config dir — Claude Code consumes it via the ``CLAUDE_CODE_OAUTH_TOKEN``
environment variable. So the launcher captures that token at login and stores it
inside the profile (``<CLAUDE_CONFIG_DIR>/.launcher-token``, ``0600``).

This module is the single source of truth for "what token does this profile
use", checking the launcher-stored token first and falling back to a
``.credentials.json`` written by an interactive ``/login`` if present.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Optional

from .profile import Profile

#: Launcher-managed token captured from ``claude setup-token``.
TOKEN_FILENAME = ".launcher-token"
#: Credentials file an interactive ``/login`` writes (fallback source).
CREDENTIALS_FILENAME = ".credentials.json"


class CredentialsError(Exception):
    """Raised when a profile has no usable OAuth token."""


def _token_path(profile: Profile):
    return profile.config_dir / TOKEN_FILENAME


def save_token(profile: Profile, token: str) -> None:
    """Persist a setup-token for ``profile`` with owner-only permissions."""
    token = token.strip()
    if not token:
        raise CredentialsError("refusing to store an empty token")
    path = _token_path(profile)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (best effort on Windows)
    except OSError:
        pass


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


def has_token(profile: Profile) -> bool:
    """Whether the profile has any usable token (stored or from login)."""
    return stored_token(profile) is not None or _credentials_access_token(profile) is not None


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
