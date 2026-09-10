"""Read one managed session's latest recorded context size.

Claude and Codex persist the required values in different JSONL formats:

* Claude assistant entries split the input into fresh, cache-read, and
  cache-write token counts.  Their sum is the context sent for that turn.
* Codex ``token_count`` events carry ``last_token_usage`` for the latest model
  request and a separate cumulative ``total_token_usage``.  The former is the
  context reading.  Codex also records ``model_context_window``.

The result is normalized for the daemon API and dashboard as ``tokens``, the
three input components, output tokens, model, and timestamp.  Codex readings
also carry ``model_context_window``.  Claude readings can carry the configured
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` as ``compact_window``.

The reading is the latest value persisted by the harness.  During an active
answer it can therefore describe the preceding model request.  A session with
no recorded request returns ``None``.  Harnesses without a supported transcript
format also return ``None``.

Claude sidechain entries are skipped because their usage belongs to the
subagent conversation.  Codex subagents use separate rollout files and are
selected by their own conversation ids.

Subscription quota reporting is implemented by :mod:`claude_launcher.usage`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from .. import harnesses as harness_registry
from .. import lineage, providers
from .. import profile as profile_mod
from . import codex_sessions
from .briefing import locate_transcript
from .harness import CLAUDE_HARNESS

CODEX_HARNESS = "codex"

#: How much of the file's end is read looking for the newest usage record.
#: One record is small, but the lines before it are tool results and can be
#: large, so the read starts small and widens rather than paying for the worst
#: case on every poll.
FIRST_CHUNK = 64 * 1024
MAX_TAIL = 1024 * 1024

#: How long a *failed* lookup is remembered. Both Claude's fallback locator
#: and Codex's rollout locator can scan multiple files. The miss must expire
#: so a session that records its first request later does not stay blank.
MISS_TTL = 15.0

#: path -> (mtime, size, reading). The file's identity is what would change
#: the answer, so an unchanged file costs one stat and nothing else. A daemon
#: restart empties it, which is fine: the next poll reads again.
_reads: Dict[str, Tuple[float, int, Optional[dict]]] = {}

#: (harness, profile, conversation, cwd) -> (path or None, lookup time).
_located: Dict[Tuple[str, str, str, str], Tuple[Optional[Path], float]] = {}

#: The env var claude reads its auto-compact threshold from. The launcher
#: sets it per profile (see template.py) and the dashboard draws it as the
#: tick on each row's context gauge — the point the conversation compacts at,
#: which is what "how full" is actually measured against.
COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"

#: How long a resolved window is trusted before the profile chain and the
#: provider registry are consulted again. Resolution reads the config store,
#: which is a file — fine once, a waste on every poll of every session.
WINDOW_TTL = 15.0

#: (profile, borrow, null_token) -> (window or None, when it was resolved).
_windows: Dict[Tuple[str, str, bool], Tuple[Optional[int], float]] = {}


def forget() -> None:
    """Drop the caches. For tests, and for anything that moves a transcript."""
    _reads.clear()
    _located.clear()
    _windows.clear()


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def usage_of(entry: dict) -> Optional[dict]:
    """The context reading in one jsonl entry, or ``None`` if it holds none.

    An entry qualifies when it is an assistant turn of *this* conversation
    (not a subagent's) carrying a usage block with an input side to it.
    """
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain"):
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    fresh = _int(usage.get("input_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    total = fresh + cache_read + cache_write
    if not total:
        # A turn reporting no input at all is not a reading of anything — an
        # interrupted or malformed record, not a conversation of size zero.
        return None
    return {
        "tokens": total,
        "input": fresh,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": _int(usage.get("output_tokens")),
        "model": str(msg.get("model") or "") or None,
        "at": str(entry.get("timestamp") or "") or None,
    }


def codex_usage_of(entry: dict) -> Optional[dict]:
    """Normalize one Codex ``token_count`` rollout entry.

    ``total_token_usage`` accumulates across the conversation.  Context size
    comes from ``last_token_usage.input_tokens``; cached and cache-write input
    counts are subsets of that value in Codex's token-usage schema.
    """
    if not isinstance(entry, dict) or entry.get("type") != "event_msg":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    if not isinstance(last, dict):
        return None

    input_total = _int(last.get("input_tokens"))
    if not input_total:
        return None
    cache_read = min(input_total, _int(last.get("cached_input_tokens")))
    cache_write = min(
        input_total - cache_read,
        _int(last.get("cache_write_input_tokens")),
    )
    reading = {
        "tokens": input_total,
        "input": input_total - cache_read - cache_write,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": _int(last.get("output_tokens")),
        "model": None,
        "at": str(entry.get("timestamp") or "") or None,
    }
    window = _window_value(info.get("model_context_window"))
    if window:
        reading["model_context_window"] = window
    return reading


def codex_model_of(entry: dict) -> Optional[str]:
    """Return the model selected by one Codex ``turn_context`` entry."""
    if not isinstance(entry, dict) or entry.get("type") != "turn_context":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    return str(payload.get("model") or "").strip() or None


def read_tail(path: Path) -> Optional[dict]:
    """The newest context reading in ``path``, or ``None``.

    Reads from the end and widens until a reading is found or ``MAX_TAIL`` is
    spent. Giving up is the honest answer: a session whose last megabyte holds
    no assistant turn is one this module cannot speak for.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    window = FIRST_CHUNK
    while True:
        try:
            with path.open("rb") as fh:
                fh.seek(max(0, size - window))
                blob = fh.read()
        except OSError:
            return None
        lines = blob.decode("utf-8", errors="replace").splitlines()
        if size > window and lines:
            lines = lines[1:]          # the read began mid-line
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            reading = usage_of(entry)
            if reading is not None:
                return reading
        if window >= size or window >= MAX_TAIL:
            return None
        window = min(window * 4, MAX_TAIL)


def read_codex_tail(path: Path) -> Optional[dict]:
    """Return the newest Codex context reading in ``path``.

    The closest preceding ``turn_context`` supplies the model for the model
    request represented by the selected ``token_count``.  A newer
    ``turn_context`` can already exist when the next turn has started, so
    model entries encountered before the token event during the reverse scan
    are deliberately ignored.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    window = FIRST_CHUNK
    while True:
        try:
            with path.open("rb") as fh:
                fh.seek(max(0, size - window))
                blob = fh.read()
        except OSError:
            return None
        lines = blob.decode("utf-8", errors="replace").splitlines()
        if size > window and lines:
            lines = lines[1:]
        reading = None
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if reading is None:
                reading = codex_usage_of(entry)
                continue
            model = codex_model_of(entry)
            if model:
                reading["model"] = model
                return reading
        if window >= size or window >= MAX_TAIL:
            return reading
        window = min(window * 4, MAX_TAIL)


def transcript_of(sdef) -> Optional[Path]:
    """This session's supported transcript, with path lookup caching."""
    harness = str(getattr(sdef, "harness", None) or CLAUDE_HARNESS)
    if harness not in (CLAUDE_HARNESS, CODEX_HARNESS):
        return None
    cid = getattr(sdef, "conversation_id", None)
    if not cid:
        return None
    profile = str(getattr(sdef, "profile", "") or "")
    key = (
        harness,
        profile,
        str(cid),
        str(getattr(sdef, "cwd", "") or ""),
    )
    hit = _located.get(key)
    now = time.monotonic()
    if hit is not None:
        path, when = hit
        if path is not None and path.is_file():
            return path
        if path is None and now - when < MISS_TTL:
            return None
    if harness == CLAUDE_HARNESS:
        path = locate_transcript(sdef)
    else:
        try:
            prof = profile_mod.require_selector(profile)
            entry = harness_registry.get(CODEX_HARNESS)
            path = (
                codex_sessions.find(entry.profile_home(prof.config_dir), str(cid))
                if entry is not None
                else None
            )
        except Exception:
            # A deleted profile or unreadable harness registry must not make
            # the session-list endpoint fail.  The path cache retries misses.
            path = None
    _located[key] = (path, now)
    return path


def for_session(sdef) -> Optional[dict]:
    """One session's context reading, cached against the file's own identity."""
    path = transcript_of(sdef)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _reads.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    reading = (
        read_codex_tail(path)
        if getattr(sdef, "harness", None) == CODEX_HARNESS
        else read_tail(path)
    )
    _reads[key] = (stat.st_mtime, stat.st_size, reading)
    return reading


def _window_value(raw) -> Optional[int]:
    """``raw`` as a usable window, or ``None`` — an unset, empty or garbled
    value means "no window is configured", never a window of zero."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def compact_window_of(sdef) -> Optional[int]:
    """The ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` this session's child sees.

    Resolved with the same precedence :func:`runner.child_env` and
    ``build_command`` apply at spawn — the session's own env over the profile
    chain over the provider's env over the daemon's environment — because the
    number is only worth drawing if it is the one claude is actually acting
    on. The profile/provider legs read the config store, so their answer is
    remembered for ``WINDOW_TTL``; a session whose own env pins the var skips
    all of that. Anything unresolvable (a deleted profile, an unknown
    provider) degrades to the daemon's environment: that is what the spawn's
    base env was, and a wrong extra leg must not turn into a wrong number.
    """
    own = getattr(sdef, "env", None) or {}
    if COMPACT_WINDOW_ENV in own:
        return _window_value(own[COMPACT_WINDOW_ENV])
    key = (str(getattr(sdef, "profile", "") or ""),
           str(getattr(sdef, "borrow", "") or ""),
           bool(getattr(sdef, "null_token", False)))
    hit = _windows.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[1] < WINDOW_TTL:
        return hit[0]
    raw = os.environ.get(COMPACT_WINDOW_ENV)
    declared = None
    try:
        prof = profile_mod.require_selector(
            str(getattr(sdef, "profile", "") or "")
        )
        if not getattr(sdef, "null_token", False):
            borrow = getattr(sdef, "borrow", None)
            auth = profile_mod.require(str(borrow)) if borrow else prof
            # The spec's auto_compact_at is what every harness is launched
            # with (Claude's env var, Codex's -c limit, Pi's reserve).
            declared = providers.spec_for(
                prof, providers.resolve_name(auth)
            ).auto_compact_at
        raw = lineage.effective_env(prof).get(COMPACT_WINDOW_ENV, raw)
    except Exception:
        pass
    if getattr(sdef, "harness", None) != CLAUDE_HARNESS:
        # Claude's raw variable (shell, profile env) is Claude's knob alone;
        # another harness compacts at what the spec declared and its
        # translator handed it (Codex's -c limit, Pi's reserve).
        value = declared
    else:
        value = _window_value(raw)
        if declared and COMPACT_WINDOW_ENV not in lineage_env_of(sdef):
            value = declared
    _windows[key] = (value, now)
    return value


def lineage_env_of(sdef) -> dict:
    """The profile chain's raw env, ``{}`` when it cannot be read."""
    try:
        prof = profile_mod.require_selector(
            str(getattr(sdef, "profile", "") or "")
        )
        return lineage.effective_env(prof)
    except Exception:
        return {}


def declared_context_window(sdef) -> Optional[int]:
    """The spec's ``context_window`` for this session's backend, if any."""
    try:
        prof = profile_mod.require_selector(
            str(getattr(sdef, "profile", "") or "")
        )
        borrow = getattr(sdef, "borrow", None)
        auth = profile_mod.require(str(borrow)) if borrow else prof
        return providers.spec_for(prof, providers.resolve_name(auth)).context_window
    except Exception:
        return None


def attach(session) -> dict:
    """``session.info()`` with a ``context`` key when there is one to give.

    The key is absent rather than null when unknown, so a reader that draws it
    cannot accidentally render "not known" as a number. When a reading exists
    Claude readings also carry ``compact_window`` when one is configured.
    Codex's ``model_context_window`` is already part of its normalized
    reading.  The cached reading is copied before the Claude-only annotation
    is added, so cache entries remain shared safely across polls.
    """
    info = session.info()
    sdef = getattr(session, "sdef", None)
    reading = for_session(sdef)
    if reading:
        info["context"] = dict(reading)
        window = compact_window_of(sdef)
        if window:
            info["context"]["compact_window"] = window
        # A harness that reports its own window (Codex) wins; otherwise the
        # spec's declared context_window draws the ceiling.
        if not info["context"].get("model_context_window"):
            declared = declared_context_window(sdef)
            if declared:
                info["context"]["model_context_window"] = declared
    return info
