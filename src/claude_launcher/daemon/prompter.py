"""Prompt snippets pulled from a prompter server (the ``prompter`` project).

prompter is a FastAPI snippet manager. Its JSON API answers
``GET /api/snippets?kind=prompt`` with ``{"kind", "count", "snippets": [...]}``
and each snippet carries ``id``, ``name``, ``title`` and ``body``. It sends no
CORS headers, so the dashboard cannot ask it directly: the daemon fetches the
list and the web UI reads it from ``GET /api/prompter/prompts``.

The server is optional. ``daemon.prompter_url`` unset (the default) means the
feature is off, and an unreachable server answers ``connected: false`` with no
prompts — the footer shows nothing in either case. The URL is read live on
every call, so the settings page's PUT applies without a restart.
"""

from __future__ import annotations

import time
from typing import Dict, Optional
from urllib.parse import urlparse

import aiohttp

from .. import store

try:
    import ssl as _ssl
    import truststore as _truststore
except ImportError:  # Python < 3.10, or truststore not installed
    _truststore = None

#: Same reason as ``daemon/rag.py``: a corporate TLS-inspection root trusted
#: by the OS still fails OpenSSL's own chain checks, so verification goes
#: through the OS validator when truststore is available.
_OS_TRUST_CONTEXT = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT) if _truststore else None

#: Seconds one fetch may take. The footer asks on every session switch, so a
#: slow or dead server must not hold the answer for long.
TIMEOUT = 5.0

#: Seconds a fetched answer (success or failure) is reused for the same URL.
CACHE_TTL = 60.0

#: Longest URL the settings endpoint accepts.
MAX_URL = 2048

_cache: Dict[str, tuple] = {}


class PrompterURLError(ValueError):
    """The configured prompter URL is not an absolute http(s) address."""


def normalize_url(raw) -> Optional[str]:
    """The URL as stored: stripped, no trailing slash; ``None`` for blank."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PrompterURLError("'url' must be a string")
    url = raw.strip().rstrip("/")
    if not url:
        return None
    if len(url) > MAX_URL:
        raise PrompterURLError("prompter URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PrompterURLError("prompter URL must be an http:// or https:// address")
    return url


def configured_url() -> Optional[str]:
    """The prompter base URL from the config file, or ``None`` when off."""
    raw = store.daemon_config().get("prompter_url")
    try:
        return normalize_url(raw)
    except PrompterURLError:
        return None


def set_url(raw) -> Optional[str]:
    """Validate and persist the base URL; blank turns the feature off."""
    url = normalize_url(raw)
    store.set_daemon_field("prompter_url", url)
    _cache.clear()
    return url


def _clean(snippets) -> list:
    rows = []
    if not isinstance(snippets, list):
        return rows
    for row in snippets:
        if not isinstance(row, dict):
            continue
        body = str(row.get("body") or "").strip()
        name = str(row.get("name") or "").strip()
        if not body or not name:
            continue
        rows.append({
            "id": row.get("id"),
            "name": name,
            "title": str(row.get("title") or "").strip(),
            "body": body,
        })
    return rows


async def _fetch(url: str) -> dict:
    connector = aiohttp.TCPConnector(ssl=_OS_TRUST_CONTEXT) if _OS_TRUST_CONTEXT else None
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        headers={"Accept": "application/json"},
    ) as http:
        async with http.get(f"{url}/api/snippets", params={"kind": "prompt"}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    if not isinstance(data, dict):
        raise RuntimeError("unexpected response shape")
    return {"connected": True, "prompts": _clean(data.get("snippets")), "error": ""}


async def prompts(*, refresh: bool = False) -> dict:
    """What the footer shows: ``{enabled, connected, url, prompts, error}``."""
    url = configured_url()
    if not url:
        return {"enabled": False, "connected": False, "url": None, "prompts": [], "error": ""}
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and not refresh and now - hit[0] < CACHE_TTL:
        answer = hit[1]
    else:
        try:
            answer = await _fetch(url)
        except Exception as exc:  # network, TLS, JSON: all mean "not connected"
            answer = {"connected": False, "prompts": [], "error": str(exc) or type(exc).__name__}
        _cache[url] = (now, answer)
    return {"enabled": True, "url": url, **answer}
