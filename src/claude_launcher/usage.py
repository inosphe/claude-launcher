"""Query subscription usage for a profile using its OAuth login token.

Claude Code's OAuth token can read the same usage endpoint the CLI uses
(``/api/oauth/usage``), which reports per-window utilization (e.g. the 5-hour and
7-day rolling limits). This module turns a profile's token into that report.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from . import config, credentials, lineage
from .profile import Profile

_OAUTH_BETA = "oauth-2025-04-20"


class UsageError(Exception):
    """Raised when usage cannot be fetched from the API."""


@dataclass(frozen=True)
class UsageWindow:
    """A single rate-limit window (5-hour, 7-day, per-model, ...)."""

    name: str
    utilization: float
    resets_at: Optional[str]
    used_dollars: Optional[float]
    limit_dollars: Optional[float]


@dataclass(frozen=True)
class UsageReport:
    windows: List[UsageWindow]
    raw: dict


def _request(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _OAUTH_BETA,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
            "User-Agent": "claude-launcher",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise UsageError(
                f"authorization failed ({exc.code}); the token may be expired — re-run 'claunch login'. {detail}"
            ) from exc
        raise UsageError(f"usage request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError(f"could not reach usage endpoint: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"unexpected (non-JSON) usage response: {exc}") from exc


def _parse_windows(payload: dict) -> List[UsageWindow]:
    windows: List[UsageWindow] = []
    for name, value in payload.items():
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        windows.append(
            UsageWindow(
                name=name,
                utilization=float(value.get("utilization") or 0.0),
                resets_at=value.get("resets_at"),
                used_dollars=value.get("used_dollars"),
                limit_dollars=value.get("limit_dollars"),
            )
        )
    return windows


def fetch(profile: Profile) -> UsageReport:
    """Fetch and parse the usage report for ``profile`` (token may be inherited)."""
    token = lineage.lookup_token(profile)
    if not token:
        raise credentials.CredentialsError(
            f"no token for profile {profile.name!r}; run 'claunch login {profile.name}' first"
        )
    payload = _request(config.usage_url(), token)
    return UsageReport(windows=_parse_windows(payload), raw=payload)
