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

Codex exposes its account limits through the stable app-server JSONL RPC.
Harnesses without a documented equivalent are rejected explicitly.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from . import config, credentials, harnesses, lineage, runner
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


def _fetch_claude(profile: Profile) -> UsageReport:
    """Claude usage (token may be inherited from a parent)."""
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


def _codex_window_name(minutes) -> str:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return "unknown"
    return {300: "five_hour", 10080: "seven_day"}.get(
        value, f"{value}_minute"
    )


def _codex_windows(payload: dict) -> List[UsageWindow]:
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict):
        return []
    windows: List[UsageWindow] = []
    for slot in ("primary", "secondary"):
        value = limits.get(slot)
        if not isinstance(value, dict):
            continue
        reset = _epoch_to_iso(str(value.get("resetsAt") or ""))
        windows.append(
            UsageWindow(
                name=_codex_window_name(value.get("windowDurationMins")),
                utilization=float(value.get("usedPercent") or 0.0),
                resets_at=reset,
                status=limits.get("rateLimitReachedType"),
            )
        )
    return windows


def _fetch_codex(profile: Profile, entry: harnesses.Harness) -> UsageReport:
    """Read ChatGPT Codex limits through the CLI's stable app-server RPC."""
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
    cmd = [*entry.launch_command(), "app-server", "--stdio"]
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
        raise UsageError(
            f"could not find Codex command {entry.program()!r}"
        ) from exc
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


def fetch(profile: Profile) -> UsageReport:
    """Fetch usage through the API supported by the profile's harness."""
    name = lineage.effective_harness(profile)
    entry = harnesses.get(name)
    if entry is None:
        raise UsageError(f"unknown harness {name!r}")
    if name == harnesses.CLAUDE_HARNESS:
        return _fetch_claude(profile)
    if entry.usage == "codex-app-server":
        return _fetch_codex(profile, entry)
    raise UsageError(
        f"usage reporting is not available for harness {name!r}; "
        "claude and codex are currently supported"
    )
