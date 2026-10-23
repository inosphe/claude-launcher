"""Query supported subscription usage for a profile's selected harness.

Claude has two ways to read the rolling 5-hour / 7-day limits:

* ``/api/oauth/usage`` — a free, read-only endpoint, but it requires the
  ``user:profile`` scope. Interactive ``/login`` tokens have it.
* The ``anthropic-ratelimit-unified-*`` response headers on a normal API call —
  available to any inference-capable token.

``claude setup-token`` tokens (what the launcher uses) do **not** carry
``user:profile``, so the endpoint 403s for them. We therefore try the endpoint
first and, on a scope error, fall back to reading the rate-limit headers from a
minimal ``/v1/messages`` call (1 output token).

Codex exposes its account limits through the documented app-server JSONL RPC.
Kimi Code exposes managed-plan limits through its ``/usages`` endpoint; the
Kimi harness reaches it through the CLI's authenticated local web server while
a Claude-compatible Kimi provider can call it with that profile's API key.
A Fireworks provider has no plan limit; its reading is the account's spend
(claunch-f5w4x.5): the month to date from the ``monthly-spend-usd`` quota,
and, when the configuration records a credit balance, what is left of it.
An OpenCode Go provider (``opencode.ai/zen/go``) reports its plan windows --
the rolling 5 hours, the week and the month, each a percentage and a reset
time -- at ``/zen/go/v1/usage``, read with the profile's API key.
Harnesses without a supported equivalent are rejected explicitly.
"""

from __future__ import annotations

import json
import queue
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from . import config, credentials, harnesses, lineage, providers, runner, store
from .profile import Profile

_OAUTH_BETA = "oauth-2025-04-20"
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_KIMI_CODE_ORIGIN = "https://api.kimi.com"
_KIMI_CODE_USAGE_URL = f"{_KIMI_CODE_ORIGIN}/coding/v1/usages"
_KIMI_WEB_READY_RE = re.compile(r"^Kimi server:\s+(https?://\S+)$")
_FIREWORKS_HOSTS = {"api.fireworks.ai", "api.fireworks.ai:443"}
_FIREWORKS_API = "https://api.fireworks.ai/v1"
#: The quota value an account without a spend limit reports.
_FIREWORKS_NO_LIMIT = 9_999_999
_OPENCODE_GO_HOSTS = {"opencode.ai", "opencode.ai:443"}
_OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
#: OpenCode Go's window names, and the names a reading gives them.
_OPENCODE_GO_WINDOWS = (("rolling", "five_hour"), ("weekly", "weekly"), ("monthly", "monthly"))


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
    remaining_dollars: Optional[float] = None


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
    req = urllib.request.Request(
        url, headers={**_headers(token), "Accept": "application/json"}
    )
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
        raise UsageError(
            f"could not read usage headers ({exc.code}): {detail}"
        ) from exc
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
                resets_at=_epoch_to_iso(
                    headers.get(f"anthropic-ratelimit-unified-{prefix}-reset")
                ),
                status=headers.get(f"anthropic-ratelimit-unified-{prefix}-status"),
            )
        )
    return windows


def _fetch_claude(profile: Profile) -> UsageReport:
    """Claude usage (token may be inherited from a parent)."""
    provider = providers.resolve_name(profile)
    if not providers.uses_anthropic_oauth(provider):
        raise UsageError(
            "Claude harness usage reporting requires an Anthropic service "
            f"provider; profile {profile.selector!r} selects provider {provider!r} "
            f"(service {providers.service(provider)!r})"
        )
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
            return UsageReport(
                windows=_parse_windows(payload), raw=payload, source="oauth-usage"
            )
        except _ScopeError:
            pass
    headers = _ratelimit_headers(token)
    windows = _windows_from_headers(headers)
    return UsageReport(windows=windows, raw=headers, source="ratelimit-headers")


def _number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _kimi_window_name(window: object, fallback: str) -> str:
    if not isinstance(window, dict):
        return fallback
    duration_value = _number(window.get("duration"))
    if duration_value is None or duration_value <= 0:
        return fallback
    duration = int(duration_value)
    raw_unit = str(window.get("timeUnit") or window.get("unit") or "").lower()
    unit = {
        "time_unit_minute": "minute",
        "time_unit_hour": "hour",
        "time_unit_day": "day",
        "time_unit_week": "week",
        "minute": "minute",
        "hour": "hour",
        "day": "day",
        "week": "week",
    }.get(raw_unit)
    if unit is None:
        return fallback
    if unit == "week" and duration == 1:
        return "weekly"
    minutes = (
        duration
        * {
            "minute": 1,
            "hour": 60,
            "day": 1440,
            "week": 10080,
        }[unit]
    )
    known = {300: "five_hour", 10080: "seven_day"}.get(minutes)
    if known:
        return known
    return f"{duration}_{unit}"


def _kimi_usage_window(
    row: object,
    window: object,
    fallback_name: str,
) -> Optional[UsageWindow]:
    if not isinstance(row, dict):
        return None
    used = _number(row.get("used"))
    limit = _number(row.get("limit"))
    if used is None and limit is None:
        return None
    used = used or 0.0
    limit = limit or 0.0
    utilization = max(0.0, min(100.0, used / limit * 100.0)) if limit > 0 else 0.0
    reset = row.get("resetTime") or row.get("reset_at")
    return UsageWindow(
        name=_kimi_window_name(window, fallback_name),
        utilization=utilization,
        resets_at=str(reset) if reset else None,
    )


def _kimi_windows(payload: dict) -> List[UsageWindow]:
    """Normalize Kimi's public payload and local-server wire payload."""
    windows: List[UsageWindow] = []
    summary = payload.get("usage")
    if not isinstance(summary, dict):
        summary = payload.get("summary")
    if isinstance(summary, dict):
        item = _kimi_usage_window(
            summary,
            summary.get("window"),
            str(summary.get("name") or "weekly"),
        )
        if item is not None:
            windows.append(item)

    limits = payload.get("limits")
    if not isinstance(limits, list):
        return windows
    for index, raw in enumerate(limits, 1):
        if not isinstance(raw, dict):
            continue
        detail = raw.get("detail")
        if not isinstance(detail, dict):
            detail = raw
        fallback = str(raw.get("name") or detail.get("name") or f"limit_{index}")
        item = _kimi_usage_window(detail, raw.get("window"), fallback)
        if item is not None:
            windows.append(item)
    return windows


def _kimi_provider_context(profile: Profile) -> Optional[tuple[str, str]]:
    """Return the managed Kimi usage URL and effective bearer credential."""
    provider = providers.resolve_name(profile)
    if provider == providers.DEFAULT_PROVIDER:
        return None
    declared_env = providers.provider_env(provider)
    declared_env.update(lineage.effective_env(profile))
    raw_base = str(declared_env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(raw_base)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() not in {"api.kimi.com", "api.kimi.com:443"}
        or parsed.path.rstrip("/") not in {"/coding", "/coding/v1"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    env = runner.child_env(profile, with_token=True, base_env={})
    token = str(
        env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
    ).strip()
    return _KIMI_CODE_USAGE_URL, token


def _api_error_message(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text[:200]
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message[:200]
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:200]
    return text[:200]


def _kimi_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Python 3.13 enabled X509_STRICT by default. Kimi's current public chain
    # is trusted and hostname-validated, but its CA Basic Constraints extension
    # is rejected by that additional RFC-5280 conformance check. Python's 3.13
    # compatibility guidance is to clear this flag while retaining CERT_REQUIRED.
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        context.verify_flags &= ~strict
    return context


def _kimi_request(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            req, timeout=30, context=_kimi_ssl_context()
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _api_error_message(exc.read())
        if exc.code in (401, 403):
            raise UsageError(
                f"Kimi usage authorization failed ({exc.code}); "
                "check the profile login or token"
            ) from exc
        suffix = f": {detail}" if detail else ""
        raise UsageError(f"Kimi usage request failed ({exc.code}){suffix}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError(f"could not reach the Kimi usage endpoint: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"unexpected Kimi usage response: {exc}") from exc
    if not isinstance(payload, dict):
        raise UsageError("Kimi usage endpoint returned an invalid response")
    return payload


def _fetch_kimi_provider(profile: Profile, url: str, token: str) -> UsageReport:
    if not token:
        raise credentials.CredentialsError(
            f"no Kimi token for profile {profile.name!r}; "
            f"run 'claunch set-token {profile.name}' first"
        )
    payload = _kimi_request(url, token)
    return UsageReport(
        windows=_kimi_windows(payload),
        raw=payload,
        source="kimi-managed-usage",
    )


def _fireworks_context(profile: Profile) -> Optional[str]:
    """The bearer credential of a profile whose provider is Fireworks, or
    ``None`` when its provider is another backend."""
    provider = providers.resolve_name(profile)
    if provider == providers.DEFAULT_PROVIDER:
        return None
    declared_env = providers.provider_env(provider)
    declared_env.update(lineage.effective_env(profile))
    raw_base = str(declared_env.get("ANTHROPIC_BASE_URL") or "")
    try:
        parsed = urllib.parse.urlsplit(raw_base)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in _FIREWORKS_HOSTS:
        return None
    env = runner.child_env(profile, with_token=True, base_env={})
    return str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()


def _opencode_go_context(profile: Profile) -> Optional[str]:
    """The bearer credential of a profile whose provider is OpenCode Go, or
    ``None`` when its provider is another backend."""
    provider = providers.resolve_name(profile)
    if provider == providers.DEFAULT_PROVIDER:
        return None
    declared_env = providers.provider_env(provider)
    declared_env.update(lineage.effective_env(profile))
    raw_base = str(declared_env.get("ANTHROPIC_BASE_URL") or "")
    try:
        parsed = urllib.parse.urlsplit(raw_base)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() not in _OPENCODE_GO_HOSTS
        or parsed.path.rstrip("/") not in {"/zen/go", "/zen/go/v1"}
    ):
        return None
    # Not ``runner.child_env(with_token=True)``: OpenCode Go declares a
    # session header, so that call would start the routing shim that fronts
    # the endpoint for a harness -- a listing must not.
    stored = lineage.stored_auth_token(profile)
    return str(
        stored or declared_env.get("ANTHROPIC_AUTH_TOKEN") or declared_env.get("ANTHROPIC_API_KEY") or ""
    ).strip()


def _opencode_go_windows(payload: dict) -> List[UsageWindow]:
    """The plan windows of a ``/zen/go/v1/usage`` reply, in the order 5h,
    week, month. The endpoint is not in OpenCode's published docs, so a
    reply without any of them is an error, not an empty reading."""
    rows = payload.get("usage") if isinstance(payload, dict) else None
    windows: List[UsageWindow] = []
    if isinstance(rows, dict):
        for key, name in _OPENCODE_GO_WINDOWS:
            row = rows.get(key)
            if not isinstance(row, dict):
                continue
            pct = _number(row.get("percent"))
            if pct is None:
                continue
            reset = row.get("resetsAt")
            status = row.get("status")
            windows.append(UsageWindow(
                name=name,
                utilization=max(0.0, min(100.0, pct)),
                resets_at=str(reset) if reset else None,
                status=str(status) if status else None,
            ))
    if not windows:
        raise UsageError(f"unexpected OpenCode Go usage response: {json.dumps(payload)[:200]}")
    return windows


def _fetch_opencode_go(profile: Profile, token: str) -> UsageReport:
    if not token:
        raise credentials.CredentialsError(
            f"no OpenCode API key for profile {profile.name!r}; "
            f"run 'claunch set-token {profile.name}' first"
        )
    req = urllib.request.Request(_OPENCODE_GO_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "claude-launcher",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code in (401, 403):
            raise UsageError(
                f"OpenCode rejected the API key ({exc.code}); "
                f"run 'claunch set-token {profile.name}' with a current key"
            ) from exc
        raise UsageError(f"OpenCode Go usage request failed ({exc.code}): {detail[:200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError(f"could not reach the OpenCode Go usage endpoint: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"unexpected (non-JSON) OpenCode Go usage response: {exc}") from exc
    return UsageReport(windows=_opencode_go_windows(payload), raw=payload, source="opencode-go")


def credit_setting(profile: Profile) -> Optional[tuple[float, date]]:
    """The credit balance recorded for ``profile``'s account: ``(balance in
    USD, the day it was read)``, or ``None`` when none is recorded.

    The Fireworks API does not report a prepaid balance, so a person copies
    it from the console into ``credit: {balance_usd, as_of}`` on the provider
    entry or on a profile (the profile itself, then its ancestors, then the
    provider). Raises :class:`UsageError` for a malformed entry.
    """
    doc = store.load()
    provider = providers.resolve_name(profile, doc)
    layers = [providers.entries(doc).get(provider, {})] + [
        store.profile_entry(p.name, doc) for p in lineage.chain(profile, doc)
    ]
    raw = None
    for entry in layers:
        if isinstance(entry.get("credit"), dict):
            raw = entry["credit"]
    if raw is None:
        return None
    balance = _number(raw.get("balance_usd"))
    as_of = raw.get("as_of")
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    elif not isinstance(as_of, date):
        try:
            as_of = date.fromisoformat(str(as_of))
        except ValueError:
            as_of = None
    if balance is None or as_of is None:
        raise UsageError(
            "credit needs balance_usd (a number) and as_of (YYYY-MM-DD), "
            f"found {raw!r}"
        )
    return balance, as_of


def _fireworks_get(path: str, token: str, query: Optional[dict] = None) -> dict:
    url = f"{_FIREWORKS_API}/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _api_error_message(exc.read())
        if exc.code in (401, 403):
            raise UsageError(
                f"Fireworks billing authorization failed ({exc.code}); "
                "check the profile's API key"
            ) from exc
        suffix = f": {detail}" if detail else ""
        raise UsageError(f"Fireworks billing request failed ({exc.code}){suffix}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UsageError(f"could not reach the Fireworks API: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"unexpected Fireworks response: {exc}") from exc
    if not isinstance(payload, dict):
        raise UsageError("Fireworks API returned an invalid response")
    return payload


def _money(value) -> float:
    """A ``google.type.Money`` value (``units`` string, ``nanos``) in units."""
    if not isinstance(value, dict):
        return 0.0
    return (_number(value.get("units")) or 0.0) + (_number(value.get("nanos")) or 0.0) / 1e9


def _fireworks_spent_since(account: str, token: str, since: date, today: date) -> float:
    """Rated cost from the start of ``since`` through ``today`` (the summary's
    end date is exclusive)."""
    summary = _fireworks_get(
        f"{account}/billing/summary",
        token,
        {
            "startTime": f"{since.isoformat()}T00:00:00Z",
            "endTime": f"{(today + timedelta(days=1)).isoformat()}T00:00:00Z",
        },
    )
    return sum(_money(item.get("totalCost")) for item in summary.get("lineItems") or [])


def _fetch_fireworks(profile: Profile, token: str, now: Optional[datetime] = None) -> UsageReport:
    if not token:
        raise credentials.CredentialsError(
            f"no Fireworks API key for profile {profile.name!r}; "
            f"run 'claunch set-token {profile.name}' first"
        )
    credit = credit_setting(profile)
    now = now or datetime.now(timezone.utc)
    accounts = _fireworks_get("accounts", token).get("accounts") or []
    account = accounts[0].get("name") if accounts and isinstance(accounts[0], dict) else None
    if not account:
        raise UsageError("the Fireworks API key names no account")
    quota = _fireworks_get(f"{account}/quotas/monthly-spend-usd", token)
    spent = _number(quota.get("usage")) or 0.0
    limit = _number(quota.get("value"))
    if limit is not None and (limit <= 0 or limit >= _FIREWORKS_NO_LIMIT):
        limit = None
    month_end = (now.replace(day=28) + timedelta(days=4)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    windows = [
        UsageWindow(
            name="monthly_spend",
            utilization=spent / limit * 100 if limit else 0.0,
            resets_at=month_end.isoformat().replace("+00:00", "Z"),
            used_dollars=spent,
            limit_dollars=limit,
            remaining_dollars=limit - spent if limit else None,
        )
    ]
    raw = {"account": account, "quota": quota}
    if credit is not None:
        balance, as_of = credit
        used = _fireworks_spent_since(account, token, as_of, now.date())
        windows.append(
            UsageWindow(
                name="credit",
                utilization=used / balance * 100 if balance > 0 else 100.0,
                resets_at=None,
                used_dollars=used,
                limit_dollars=balance,
                remaining_dollars=balance - used,
            )
        )
        raw["credit"] = {"balance_usd": balance, "as_of": as_of.isoformat(), "spent": used}
    return UsageReport(windows=windows, raw=raw, source="fireworks-billing")


def _codex_window_name(minutes) -> str:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "unknown"
    return {300: "five_hour", 10080: "seven_day"}.get(value, f"{value}_minute")


def _codex_windows(payload: dict) -> List[UsageWindow]:
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        buckets = [
            (str(key), value) for key, value in by_id.items() if isinstance(value, dict)
        ]
    else:
        limits = payload.get("rateLimits")
        buckets = [("", limits)] if isinstance(limits, dict) else []
    windows: List[UsageWindow] = []
    qualify = len(buckets) > 1
    for bucket_id, limits in buckets:
        label = str(limits.get("limitName") or limits.get("limitId") or bucket_id)
        for slot in ("primary", "secondary"):
            value = limits.get(slot)
            if not isinstance(value, dict):
                continue
            name = _codex_window_name(value.get("windowDurationMins"))
            if qualify and label:
                name = f"{label}.{name}"
            reset = _epoch_to_iso(str(value.get("resetsAt") or ""))
            windows.append(
                UsageWindow(
                    name=name,
                    utilization=float(value.get("usedPercent") or 0.0),
                    resets_at=reset,
                    status=limits.get("rateLimitReachedType"),
                )
            )
    return windows


def _fetch_codex(profile: Profile, entry: harnesses.Harness) -> UsageReport:
    """Read ChatGPT Codex limits through the CLI's app-server RPC."""
    requests = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "claude_launcher",
                    "title": "claude-launcher",
                    "version": "1",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 2, "params": {}},
    ]
    # stdio JSONL is the documented default transport. There is no ``--stdio``
    # flag; the explicit spelling would be ``--listen stdio://``.
    cmd = [*entry.launch_command(), "app-server"]
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=runner.harness_child_env(profile, entry),
        )
    except FileNotFoundError as exc:
        raise UsageError(f"could not find Codex command {entry.program()!r}") from exc
    except OSError as exc:
        raise UsageError(f"could not start Codex app-server: {exc}") from exc

    assert process.stdin is not None and process.stdout is not None
    lines: queue.Queue = queue.Queue()

    def _read_stdout() -> None:
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_read_stdout, daemon=True).start()
    reply = None
    deadline = time.monotonic() + 30.0
    detail_lines = []
    try:
        # Keep stdin open. Closing it immediately after these writes lets the
        # app-server exit before the asynchronous rate-limit response is
        # emitted (subprocess.run therefore is not suitable here).
        for item in requests:
            process.stdin.write(json.dumps(item) + "\n")
        process.stdin.flush()
        while reply is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("Codex usage request timed out after 30 seconds")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise UsageError("Codex usage request timed out after 30 seconds")
            if line is None:
                break
            detail_lines.append(line.strip())
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id") == 2:
                reply = item
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if reply is None:
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read()
        detail = (stderr or "\n".join(detail_lines)).strip()[:300]
        raise UsageError(
            "Codex app-server returned no rate-limit response"
            + (f": {detail}" if detail else "")
        )
    if isinstance(reply.get("error"), dict):
        message = reply["error"].get("message") or str(reply["error"])
        raise UsageError(f"Codex usage request failed: {message}")
    payload = reply.get("result")
    if not isinstance(payload, dict):
        raise UsageError("Codex app-server returned an invalid rate-limit response")
    return UsageReport(
        windows=_codex_windows(payload), raw=payload, source="codex-app-server"
    )


def _kimi_server_location(value: str) -> Optional[tuple[str, str]]:
    match = _KIMI_WEB_READY_RE.match(value.strip())
    if match is None:
        return None
    try:
        parsed = urllib.parse.urlsplit(match.group(1))
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return None
    values = urllib.parse.parse_qs(parsed.fragment).get("token")
    if not values or not values[0]:
        return None
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return origin, values[0]


def _shutdown_kimi_server(origin: str, token: str) -> None:
    request = urllib.request.Request(
        f"{origin}/api/v1/shutdown",
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=3, context=_kimi_ssl_context()
        ) as response:
            response.read()
    except (OSError, urllib.error.URLError, TimeoutError):
        # Cleanup is best-effort; the process fallback below still stops it.
        pass


def _fetch_kimi(profile: Profile, entry: harnesses.Harness) -> UsageReport:
    """Use Kimi Code's authenticated local server to refresh OAuth safely."""
    cmd = [
        *entry.launch_command(),
        "web",
        "--no-open",
        "--port",
        "0",
        "--log-level",
        "error",
    ]
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=runner.harness_child_env(profile, entry),
        )
    except FileNotFoundError as exc:
        raise UsageError(f"could not find Kimi command {entry.program()!r}") from exc
    except OSError as exc:
        raise UsageError(f"could not start Kimi Code local server: {exc}") from exc

    assert process.stdout is not None
    lines: queue.Queue = queue.Queue()

    def _read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_read_stdout, daemon=True).start()
    location = None
    deadline = time.monotonic() + 30.0
    try:
        while location is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageError("Kimi Code local server timed out after 30 seconds")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise UsageError("Kimi Code local server timed out after 30 seconds")
            if line is None:
                break
            location = _kimi_server_location(line)
        if location is None:
            stderr = (
                process.stderr.read()
                if process.poll() is not None and process.stderr is not None
                else ""
            )
            detail = stderr.strip()[:300]
            raise UsageError(
                "Kimi Code local server did not report a listening address"
                + (f": {detail}" if detail else "")
            )
        origin, server_token = location
        envelope = _kimi_request(f"{origin}/api/v1/oauth/usage", server_token)
    finally:
        if location is not None:
            _shutdown_kimi_server(*location)
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    data = envelope.get("data")
    if envelope.get("code") != 0 or not isinstance(data, dict):
        raise UsageError("Kimi Code local server returned an invalid usage response")
    if data.get("kind") == "error":
        message = data.get("message") or "unknown Kimi Code usage error"
        raise UsageError(f"Kimi usage request failed: {message}")
    if data.get("kind") != "ok":
        raise UsageError("Kimi Code local server returned an invalid usage response")
    return UsageReport(windows=_kimi_windows(data), raw=data, source="kimi-web-server")


def _default_usage_has_credentials(profile: Profile) -> bool:
    name = lineage.effective_harness(profile)
    if name != harnesses.CLAUDE_HARNESS:
        entry = harnesses.get(name)
        return bool(entry and entry.usage)
    kimi = _kimi_provider_context(profile)
    if kimi is not None:
        return bool(kimi[1])
    fireworks = _fireworks_context(profile)
    if fireworks is not None:
        return bool(fireworks)
    if not providers.uses_anthropic_oauth(providers.resolve_name(profile)):
        return False
    token, _profile_scoped = lineage.resolve_token(profile)
    return bool(token)


def resolve_target(profile: Profile) -> Profile:
    """Resolve a bare legacy profile to an initialized same-name harness.

    Explicit ``PROFILE:HARNESS`` selectors always win. A bare profile keeps
    its configured default whenever that path has a usable usage credential.
    The fallback covers profiles created before harness selectors existed,
    such as a ``codex`` profile whose isolated ``codex`` home is already
    logged in while the profile default still says ``claude``.
    """
    if profile.harness_override is not None:
        return profile
    current = lineage.effective_harness(profile)
    if current == profile.name or _default_usage_has_credentials(profile):
        return profile
    entry = harnesses.get(profile.name)
    if (
        entry is None
        or not entry.usage
        or not entry.profile_home(profile.config_dir).is_dir()
    ):
        return profile
    return Profile(
        name=profile.name,
        config_dir=profile.config_dir,
        harness_override=entry.name,
    )


def reader(profile: Profile) -> str:
    """Name the reader :func:`fetch` uses for ``profile``, without any I/O.

    Raises :class:`UsageError` with the reason when no reader applies, so a
    listing can tell "not available here" apart from a failed request before
    asking the account endpoint anything.
    """
    name = lineage.effective_harness(profile)
    entry = harnesses.get(name)
    if entry is None:
        raise UsageError(f"unknown harness {name!r}")
    if name == harnesses.CLAUDE_HARNESS:
        if _kimi_provider_context(profile) is not None:
            return "kimi-provider"
        if _fireworks_context(profile) is not None:
            return "fireworks-billing"
        if _opencode_go_context(profile) is not None:
            return "opencode-go"
        provider = providers.resolve_name(profile)
        if not providers.uses_anthropic_oauth(provider):
            raise UsageError(
                "Claude harness usage reporting requires an Anthropic service "
                f"provider; profile {profile.selector!r} selects provider "
                f"{provider!r} (service {providers.service(provider)!r})"
            )
        return "anthropic"
    if entry.usage in ("codex-app-server", "kimi-web-server"):
        return entry.usage
    raise UsageError(
        f"usage reporting is not available for harness {name!r}; "
        "claude, codex and kimi are currently supported"
    )


def fetch(profile: Profile) -> UsageReport:
    """Fetch usage through the API supported by the profile's harness."""
    kind = reader(profile)
    if kind == "kimi-provider":
        kimi = _kimi_provider_context(profile)
        if kimi is None:
            raise UsageError(f"profile {profile.selector!r} no longer selects Kimi Code")
        return _fetch_kimi_provider(profile, *kimi)
    if kind == "fireworks-billing":
        token = _fireworks_context(profile)
        if token is None:
            raise UsageError(f"profile {profile.selector!r} no longer selects Fireworks")
        return _fetch_fireworks(profile, token)
    if kind == "opencode-go":
        token = _opencode_go_context(profile)
        if token is None:
            raise UsageError(f"profile {profile.selector!r} no longer selects OpenCode Go")
        return _fetch_opencode_go(profile, token)
    if kind == "anthropic":
        return _fetch_claude(profile)
    entry = harnesses.get(lineage.effective_harness(profile))
    if kind == "codex-app-server":
        return _fetch_codex(profile, entry)
    return _fetch_kimi(profile, entry)
