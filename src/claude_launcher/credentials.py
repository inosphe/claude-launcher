"""Read OAuth credentials that ``claude setup-token`` writes into a profile.

Claude Code stores its login under ``<CLAUDE_CONFIG_DIR>/.credentials.json`` with
a ``claudeAiOauth`` object. This module is the single reader of that file; it does
not perform any network or refresh logic.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import List, Optional

from .profile import Profile

CREDENTIALS_FILENAME = ".credentials.json"


class CredentialsError(Exception):
    """Raised when a profile has no readable OAuth credentials."""


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    refresh_token: Optional[str]
    expires_at_ms: Optional[int]
    subscription_type: Optional[str]
    scopes: List[str]

    @property
    def is_expired(self) -> bool:
        if self.expires_at_ms is None:
            return False
        return self.expires_at_ms <= int(time.time() * 1000)

    @property
    def expires_at_epoch(self) -> Optional[float]:
        return None if self.expires_at_ms is None else self.expires_at_ms / 1000.0


def load(profile: Profile) -> OAuthToken:
    """Load and parse the OAuth token for ``profile``."""
    path = profile.config_dir / CREDENTIALS_FILENAME
    if not path.is_file():
        raise CredentialsError(
            f"no credentials for profile {profile.name!r}; run 'claunch login {profile.name}' first"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsError(f"could not read credentials: {exc}") from exc

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise CredentialsError(
            f"credentials for profile {profile.name!r} are missing an OAuth access token"
        )

    return OAuthToken(
        access_token=oauth["accessToken"],
        refresh_token=oauth.get("refreshToken"),
        expires_at_ms=oauth.get("expiresAt"),
        subscription_type=oauth.get("subscriptionType"),
        scopes=list(oauth.get("scopes") or []),
    )
