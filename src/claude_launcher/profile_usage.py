"""Cached profile subscription usage for interactive dashboard refreshes.

The usage readers live in :mod:`usage`; this module owns only refresh policy so
new harness adapters continue to use the same normalized ``UsageReport``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from . import credentials, profile as profile_mod, usage

AUTO_COOLDOWN_SECONDS = 5 * 60


@dataclass
class _Entry:
    requested_at: float
    payload: Optional[dict] = None
    error: Optional[str] = None


_guard = threading.Lock()
_entries: Dict[str, _Entry] = {}
_locks: Dict[str, threading.Lock] = {}


def fetch(selector: str, *, manual: bool = False) -> tuple[dict, bool, float]:
    """Return normalized usage, throttling automatic reads per profile.

    Manual reads always reach the provider. Automatic reads return the last
    result during the five-minute window. Per-selector locks collapse
    simultaneous page-entry requests into one provider call.
    """
    now = time.time()
    with _guard:
        lock = _locks.setdefault(selector, threading.Lock())
        entry = _entries.get(selector)
        if not manual and entry is not None and now - entry.requested_at < AUTO_COOLDOWN_SECONDS:
            if entry.error:
                raise usage.UsageError(entry.error)
            if entry.payload is not None:
                return dict(entry.payload), True, entry.requested_at

    with lock:
        now = time.time()
        with _guard:
            entry = _entries.get(selector)
            if not manual and entry is not None and now - entry.requested_at < AUTO_COOLDOWN_SECONDS:
                if entry.error:
                    raise usage.UsageError(entry.error)
                if entry.payload is not None:
                    return dict(entry.payload), True, entry.requested_at
            entry = _Entry(requested_at=now)
            _entries[selector] = entry

        try:
            selected = usage.resolve_target(profile_mod.require_selector(selector))
            report = usage.fetch(selected)
            payload = {
                "profile": selected.selector,
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
        except (profile_mod.ProfileError, usage.UsageError, credentials.CredentialsError) as exc:
            with _guard:
                entry.error = str(exc)
            raise usage.UsageError(str(exc)) from exc
        with _guard:
            entry.payload = payload
            entry.requested_at = time.time()
            updated_at = entry.requested_at
        return payload, False, updated_at
