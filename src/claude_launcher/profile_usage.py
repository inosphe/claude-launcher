"""Cached profile subscription usage for interactive dashboard refreshes.

The usage readers live in :mod:`usage`; this module owns only refresh policy so
new harness adapters continue to use the same normalized ``UsageReport``.

A quota belongs to an account, and the account is the profile's credential
for its provider -- not the harness that happens to run on it. So the unit
here is the (profile, provider) pair: ``work`` and ``work:claude`` that
resolve to the same pair share one cache entry and one provider call, and
the dashboard lists one row per pair (:func:`targets`). The key also names
the reader (:func:`usage.reader`): a harness that keeps its own login
(``work:codex``) reads another account than the profile's provider does,
and ``providers.resolve_name`` does not look at the harness.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import credentials, lineage, profile as profile_mod, providers, usage

AUTO_COOLDOWN_SECONDS = 5 * 60


@dataclass
class _Entry:
    requested_at: float
    payload: Optional[dict] = None
    error: Optional[str] = None


_guard = threading.Lock()
_entries: Dict[str, _Entry] = {}
_locks: Dict[str, threading.Lock] = {}


_ERRORS = (
    profile_mod.ProfileError,
    usage.UsageError,
    credentials.CredentialsError,
    lineage.LineageError,
    providers.ProviderError,
)


def _resolve(selector: str):
    """The profile usage reads for ``selector``, its provider, and its cache key.

    Raises :class:`usage.UsageError` when no reader applies.
    """
    selected = usage.resolve_target(profile_mod.require_selector(selector))
    provider = providers.resolve_name(selected)
    return selected, provider, _key(selected, provider, usage.reader(selected))


def _key(selected, provider: str, reader: str) -> str:
    return f"{selected.name}/{provider}/{reader}"


def targets() -> list[dict]:
    """One row per profile: its provider, the harness that reads the quota,
    and whether any reader applies (``supported``, else ``reason``).

    Answered from configuration alone; nothing here reaches an account.
    """
    rows = []
    for profile in profile_mod.list_all():
        row: dict = {"profile": profile.name, "provider": None, "harness": None}
        try:
            selected = usage.resolve_target(profile)
            row.update(
                key=profile.name,
                provider=providers.resolve_name(selected),
                harness=lineage.effective_harness(selected),
            )
            row["reader"] = usage.reader(selected)
            row["key"] = _key(selected, row["provider"], row["reader"])
            row["supported"] = True
        except _ERRORS as exc:
            row.setdefault("key", profile.name)
            row.update(supported=False, reason=str(exc))
        rows.append(row)
    return rows


def fetch(selector: str, *, manual: bool = False) -> tuple[dict, bool, float]:
    """Return normalized usage, throttling automatic reads per account.

    The selector is resolved to its (profile, provider) pair first, and the
    cache and lock are kept per pair. Manual reads always reach the provider.
    Automatic reads return the last result during the five-minute window.
    Per-pair locks collapse simultaneous page-entry requests into one
    provider call.
    """
    try:
        selected, provider, key = _resolve(selector)
    except _ERRORS as exc:
        raise usage.UsageError(str(exc)) from exc
    now = time.time()
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
        entry = _entries.get(key)
        if not manual and entry is not None and now - entry.requested_at < AUTO_COOLDOWN_SECONDS:
            if entry.error:
                raise usage.UsageError(entry.error)
            if entry.payload is not None:
                return dict(entry.payload), True, entry.requested_at

    with lock:
        now = time.time()
        with _guard:
            entry = _entries.get(key)
            if not manual and entry is not None and now - entry.requested_at < AUTO_COOLDOWN_SECONDS:
                if entry.error:
                    raise usage.UsageError(entry.error)
                if entry.payload is not None:
                    return dict(entry.payload), True, entry.requested_at
            entry = _Entry(requested_at=now)
            _entries[key] = entry

        try:
            report = usage.fetch(selected)
            payload = {
                "key": key,
                "profile": selected.name,
                "provider": provider,
                "harness": lineage.effective_harness(selected),
                "source": report.source,
                "windows": [
                    {
                        "name": item.name,
                        "utilization": item.utilization,
                        "resets_at": item.resets_at,
                        "used_dollars": item.used_dollars,
                        "limit_dollars": item.limit_dollars,
                        "status": item.status,
                    }
                    for item in report.windows
                ],
            }
        except _ERRORS as exc:
            with _guard:
                entry.error = str(exc)
            raise usage.UsageError(str(exc)) from exc
        with _guard:
            entry.payload = payload
            entry.requested_at = time.time()
            updated_at = entry.requested_at
        return payload, False, updated_at
