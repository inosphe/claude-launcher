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

Every key refreshes on one period (:data:`PERIOD_SECONDS`, fifteen minutes):
an automatic read inside it is answered from the cache, and the daemon's
refresh clock (``daemon/usage_clock.py``) reads each key again once its own
period since the last provider read has run out (:func:`due`). A manual read
reaches the provider at once and so starts that key's period over. Each
provider read that succeeds is one sample in :mod:`usage_history`
(claunch-f5w4x.3); the newest sample is the cache a restarted daemon starts
from.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import (
    credentials,
    lineage,
    profile as profile_mod,
    profile_keys,
    providers,
    usage,
    usage_history,
)

log = logging.getLogger(__name__)

#: Seconds between two provider reads of one key, automatic or periodic.
PERIOD_SECONDS = 15 * 60
AUTO_COOLDOWN_SECONDS = PERIOD_SECONDS


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


def _entry(key: str) -> Optional[_Entry]:
    """The cache entry of ``key``; a key this daemon has not read yet starts
    from its newest history sample. Call with ``_guard`` held."""
    entry = _entries.get(key)
    if entry is None:
        try:
            sample = usage_history.last(key)
        except OSError:
            sample = None
        if sample is not None:
            payload = {k: v for k, v in sample.items() if k != "at"}
            entry = _entries[key] = _Entry(requested_at=sample["at"], payload=payload)
    return entry


def _reusable(entry: Optional[_Entry], now: float, manual: bool) -> bool:
    """Whether ``entry`` answers a read without reaching the account.

    One place for the period rule, because two readers ask it now: the
    profile's own reading (:func:`fetch`) and each pool key's
    (:func:`key_usage`). A manual read always reaches the provider.
    """
    return bool(
        entry is not None
        and not manual
        and now - entry.requested_at < AUTO_COOLDOWN_SECONDS
    )


def schedule(key: str) -> dict:
    """When ``key`` was last read from its provider and when its period ends.

    Both are ``None`` for a key never read: it is due now.
    """
    with _guard:
        entry = _entry(key)
    if entry is None:
        return {"updated_at": None, "next_at": None, "period_s": PERIOD_SECONDS}
    return {
        "updated_at": entry.requested_at,
        "next_at": entry.requested_at + PERIOD_SECONDS,
        "period_s": PERIOD_SECONDS,
    }


def due(rows: list[dict], now: Optional[float] = None) -> list[dict]:
    """The rows of :func:`targets` whose key's period has run out, one per key."""
    now = time.time() if now is None else now
    seen, found = set(), []
    for row in rows:
        if not row.get("supported") or row["key"] in seen:
            continue
        seen.add(row["key"])
        next_at = schedule(row["key"])["next_at"]
        if next_at is None or next_at <= now:
            found.append(row)
    return found


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
            row["schedule"] = schedule(row["key"])
        except _ERRORS as exc:
            row.setdefault("key", profile.name)
            row.update(supported=False, reason=str(exc))
        rows.append(row)
    return rows


def fetch(selector: str, *, manual: bool = False) -> tuple[dict, bool, float]:
    """Return normalized usage, throttling automatic reads per account.

    The selector is resolved to its (profile, provider) pair first, and the
    cache and lock are kept per pair. Manual reads always reach the provider.
    Automatic reads return the last result during the key's period.
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
        entry = _entry(key)
        if _reusable(entry, now, manual):
            if entry.error:
                raise usage.UsageError(entry.error)
            if entry.payload is not None:
                return dict(entry.payload), True, entry.requested_at

    with lock:
        now = time.time()
        with _guard:
            entry = _entries.get(key)
            if _reusable(entry, now, manual):
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
                        "remaining_dollars": item.remaining_dollars,
                        "status": item.status,
                        "detail": item.detail,
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
        try:
            usage_history.append(key, payload, updated_at)
        except OSError as exc:
            log.warning("usage history: cannot write %s: %s", key, exc)
        return payload, False, updated_at


def key_usage(
    selector: str, *, manual: bool = False, skip=()
) -> dict:
    """The usage each key of the pool a launch of ``selector`` reads reports.

    One account read per key, cached on the same period as the profile's own
    reading (:data:`PERIOD_SECONDS`) and keyed by the pool key rather than by
    the account, because a pool exists precisely so that its keys are
    different accounts. ``skip`` names key ids the caller reads another way —
    the key currently in the token file is the profile's own reading, and
    measuring it twice would spend one request to show the same numbers.

    Answer: ``{"owner", "keys": {key_id: row}, "unmeasurable"}``. ``owner`` is
    the profile whose pool it is (``None`` when no profile in the chain has
    one), and ``unmeasurable`` is the sentence from
    :func:`usage.unmeasurable_reason` when the harness's usage belongs to its
    login and no key can be asked (``keys`` is then empty).

    A key whose read fails is a row with ``error`` and no windows: one account
    endpoint failing must not blank the other keys' numbers. Unlike
    :func:`fetch` this never raises for that reason, and it writes no
    :mod:`usage_history` samples — the history is the usage page's, keyed by
    the profile's account, and a pool key is not one of its rows.
    """
    try:
        selected, _provider, base = _resolve(selector)
    except _ERRORS as exc:
        raise usage.UsageError(str(exc)) from exc
    source = profile_keys.owner(selected)
    if source is None:
        return {"owner": None, "keys": {}, "unmeasurable": None}
    pool = profile_keys.load(source)
    if pool is None or not pool.keys:
        return {"owner": source.name, "keys": {}, "unmeasurable": None}
    try:
        reason = usage.unmeasurable_reason(usage.reader(selected))
    except _ERRORS as exc:
        reason = f"the profile's usage reader is not available ({exc})"
    if reason is not None:
        return {"owner": source.name, "keys": {}, "unmeasurable": reason}
    skipped = set(skip or ())
    rows = {}
    for key in pool.keys:
        if key.id in skipped:
            continue
        rows[key.id] = _key_reading(selected, base, key, manual=manual)
    return {"owner": source.name, "keys": rows, "unmeasurable": None}


def _key_reading(profile, base: str, key, *, manual: bool) -> dict:
    """One pool key's cached reading (:func:`key_usage`'s per-key half).

    The cache key holds the token's digest: a key id survives a token
    replacement (``claunch set-token --key``), so the id alone would serve the
    previous account's numbers under the new credential.
    """
    token = str(key.token or "").strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    cache_key = f"{base}/key:{key.id}:{digest}"
    now = time.time()
    with _guard:
        lock = _locks.setdefault(cache_key, threading.Lock())
        entry = _entries.get(cache_key)
        if _reusable(entry, now, manual):
            return _key_row(key, entry)

    with lock:
        now = time.time()
        with _guard:
            entry = _entries.get(cache_key)
            if _reusable(entry, now, manual):
                return _key_row(key, entry)
            entry = _Entry(requested_at=now)
            _entries[cache_key] = entry

        try:
            report = usage.report_for_token(profile, token)
            if report is None:
                raise usage.UsageError(
                    usage.unmeasurable_reason(usage.reader(profile))
                    or "this key's own usage cannot be read"
                )
            payload = {
                "source": report.source,
                "windows": [
                    {
                        "name": item.name,
                        "utilization": item.utilization,
                        "resets_at": item.resets_at,
                        "used_dollars": item.used_dollars,
                        "limit_dollars": item.limit_dollars,
                        "remaining_dollars": item.remaining_dollars,
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in report.windows
                ],
            }
        except _ERRORS as exc:
            with _guard:
                entry.error = str(exc)
                entry.requested_at = time.time()
            return _key_row(key, entry)
        with _guard:
            entry.payload = payload
            entry.requested_at = time.time()
        return _key_row(key, entry)


def _key_row(key, entry: _Entry) -> dict:
    payload = entry.payload or {}
    return {
        "key_id": key.id,
        "windows": payload.get("windows") or [],
        "source": payload.get("source"),
        "updated_at": entry.requested_at,
        "error": entry.error,
    }
