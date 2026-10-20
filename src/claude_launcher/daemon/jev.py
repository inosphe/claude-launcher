"""Jev: TypeSafe's System One model, called over plain HTTP (claunch-165w6).

Jev answers typed questions about a *state*: a Choice picks one option and
returns the whole distribution plus a confidence, which is what a daemon
decision wants -- something code branches on, not prose it has to parse.
One endpoint serves it::

    POST {base_url}/v1/systemone
    Authorization: Bearer <key>
    {"model": "jev-latest", "state": ..., "questions": {<id>: {...}}}

and the answer comes back keyed by the same ids (``answers``) with the
versioned model that answered (``model``) and ``usage``. See
https://docs.typesafe.ai/api.

The vendor SDK is deliberately not a dependency: the daemon already speaks
aiohttp (observer.py, briefing.py), and one POST does not justify a new
package on every install.

Limits this module's callers have to respect, from https://docs.typesafe.ai/models
(jev-1.13): 64k tokens per request, 32k for the ``state`` plus the longest
question. Nothing here measures tokens; the caller caps what it puts in.

The key is never written into the daemon config (``claunch daemon config``
prints every key it holds). It is read, in order, from the key file the web
UI writes (:func:`key_path`, mode 0600) and from the environment variable the
``jev_api_key_env`` setting names. The file exists so a key can be set
without restarting the daemon: the daemon's environment is fixed at start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import aiohttp

from . import paths

try:
    import truststore
except ImportError:  # pragma: no cover - optional on some installs
    truststore = None

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
DEFAULT_KEY_ENV = "TYPESAFE_API_KEY"
#: Total seconds for one HTTP attempt. Jev is a fast model; a call that takes
#: longer than this is not going to be worth waiting for at a turn's end.
TIMEOUT = 15.0
#: Statuses the API documents as "back off and retry" (rate limit, overloaded).
RETRY_STATUSES = (429, 529)
RETRY_DELAY = 2.0
RETRIES = 1


class JevError(Exception):
    """The call did not produce answers. The message never carries the key
    or the request body: both may hold what should not reach a log."""


def key_path() -> Path:
    return paths.daemon_dir() / "jev.key"


def read_key_file() -> str:
    try:
        return key_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def write_key_file(key: str) -> None:
    """Store the key (an empty string removes the file). 0600 from creation,
    so there is no moment when another account can read it."""
    path = key_path()
    if not key:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def resolve_key(cfg: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """``(key, source)``: source is ``"file"``, ``"env:<NAME>"`` or None."""
    key = read_key_file()
    if key:
        return key, "file"
    name = str(cfg.get("jev_api_key_env") or DEFAULT_KEY_ENV).strip()
    if name:
        key = os.environ.get(name, "").strip()
        if key:
            return key, f"env:{name}"
    return "", None


def endpoint(cfg: Dict[str, Any]) -> str:
    base = str(cfg.get("jev_base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    return base + "/v1/systemone"


def model_of(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("jev_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


#: The features that may pin a model of their own (claunch-v5nhp). Each one
#: reads ``jev_model_<feature>`` and falls back to the global ``jev_model``,
#: so an operator can move one caller to a new release before the alias does.
FEATURES = ("qa_nudge", "megathread")


def model_for(cfg: Dict[str, Any], feature: str) -> str:
    """The model this feature asks, from ``jev_model_<feature>`` when set."""
    key = "jev_model_" + str(feature or "").strip()
    value = str(cfg.get(key) or "").strip() if feature else ""
    return value or model_of(cfg)


async def _post(url: str, key: str, body: dict) -> Tuple[int, Any]:
    connector = (
        aiohttp.TCPConnector(ssl=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        if truststore else None
    )
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=TIMEOUT), connector=connector,
    ) as http:
        async with http.post(
            url, json=body, headers={"Authorization": "Bearer " + key},
        ) as response:
            if response.status != 200:
                return response.status, None
            return 200, await response.json(content_type=None)


async def system_one(
    cfg: Dict[str, Any], state: Any, questions: Dict[str, dict], *,
    key: Optional[str] = None, model: Optional[str] = None,
) -> Dict[str, Any]:
    """One evaluation. Returns the response document (``model``, ``answers``,
    ``usage``). Raises :class:`JevError` on no key, a non-200 after the
    retries, a transport failure or a body without ``answers``.

    ``model`` overrides the global ``jev_model`` for one caller; features
    that may pin their own pass ``model_for(cfg, <feature>)``.
    """
    if key is None:
        key, _ = resolve_key(cfg)
    if not key:
        raise JevError("no Jev API key (key file or environment variable)")
    body = {"model": model or model_of(cfg), "state": state, "questions": questions}
    url = endpoint(cfg)
    status = 0
    for attempt in range(RETRIES + 1):
        try:
            status, data = await _post(url, key, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise JevError(f"Jev request failed: {type(exc).__name__}") from None
        if status == 200:
            if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                raise JevError("Jev response has no answers")
            return data
        if status in RETRY_STATUSES and attempt < RETRIES:
            await asyncio.sleep(RETRY_DELAY)
            continue
        break
    raise JevError(f"Jev HTTP {status}")
