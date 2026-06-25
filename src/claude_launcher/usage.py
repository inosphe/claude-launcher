"""Query subscription usage for a profile using its OAuth login token.

There are two ways to read the rolling 5-hour / 7-day limits:

* ``/api/oauth/usage`` — a free, read-only endpoint, but it requires the
  ``user:profile`` scope. Interactive ``/login`` tokens have it.
* The ``anthropic-ratelimit-unified-*`` response headers on a normal API call —
  available to any inference-capable token.

``claude setup-token`` tokens (what the launcher uses) do **not** carry
``user:profile``, so the endpoint 403s for them. We therefore try the endpoint
first and, on a scope error, fall back to reading the rate-limit headers from a
minimal ``/v1/messages`` call (1 output token).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from . import config, credentials, lineage
from .profile import Profile

_OAUTH_BETA = "oauth-2025-04-20"
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


class UsageError(Exception):
    """Raised when usage cannot be fetched from the API."""


class _ScopeError(Exception):
    """Internal: the OAuth usage endpoint rejected the token's scope."""


@dataclass(frozen=True)
class UsageWindow:
    """A single rate-limit window (5-hour, 7-day, per-model, ...)."""

    name: str
    utilization: float
    resets_at: Optional[str]
    used_dollars: Optional[float] = None
    limit_dollars: Optional[float] = None
    status: Optional[str] = None


@dataclass(frozen=True)
class UsageReport:
    windows: List[UsageWindow]
    raw: dict
    source: str = "oauth-usage"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _OAUTH_BETA,
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-launcher",
    }


def _oauth_usage(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={**_headers(token), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 429:
            raise UsageError("rate limited by the API; try again later") from exc
        if exc.code == 403 and "scope" in detail.lower():
            raise _ScopeError() from exc
        if exc.code in (401, 403):
            raise UsageError(
                f"authorization failed ({exc.code}); the token may be expired — re-run 'claunch login'"
            ) from exc
        raise UsageError(f"usage request failed ({exc.code}): {detail[:200]}") from exc
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


def _ratelimit_headers(token: str) -> dict:
    """Minimal messages call; return the unified rate-limit headers (lower-cased)."""
    body = json.dumps(
        {
            "model": config.usage_model(),
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _MESSAGES_URL,
        data=body,
        headers={**_headers(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise UsageError("rate limited by the API; try again later") from exc
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise UsageError(f"could not read usage headers ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError(f"could not reach the API: {exc}") from exc


def _epoch_to_iso(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _windows_from_headers(headers: dict) -> List[UsageWindow]:
    windows: List[UsageWindow] = []
    for name, prefix in (("five_hour", "5h"), ("seven_day", "7d")):
        util = headers.get(f"anthropic-ratelimit-unified-{prefix}-utilization")
        if util is None:
            continue
        windows.append(
            UsageWindow(
                name=name,
                utilization=float(util) * 100.0,
                resets_at=_epoch_to_iso(headers.get(f"anthropic-ratelimit-unified-{prefix}-reset")),
                status=headers.get(f"anthropic-ratelimit-unified-{prefix}-status"),
            )
        )
    return windows


def fetch(profile: Profile) -> UsageReport:
    """Fetch a usage report for ``profile`` (token may be inherited from a parent)."""
    token, profile_scoped = lineage.resolve_token(profile)
    if not token:
        raise credentials.CredentialsError(
            f"no token for profile {profile.name!r}; run 'claunch login {profile.name}' first"
        )
    # The free OAuth usage endpoint needs the user:profile scope; setup-tokens
    # don't have it, so for them we go straight to the rate-limit headers and
    # avoid a wasted (rate-limit-consuming) 403.
    if profile_scoped:
        try:
            payload = _oauth_usage(config.usage_url(), token)
            return UsageReport(windows=_parse_windows(payload), raw=payload, source="oauth-usage")
        except _ScopeError:
            pass
    headers = _ratelimit_headers(token)
    windows = _windows_from_headers(headers)
    return UsageReport(windows=windows, raw=headers, source="ratelimit-headers")
