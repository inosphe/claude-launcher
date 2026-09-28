"""Read one managed session's latest recorded context size.

Claude, Codex and Pi persist the required values in different JSONL formats:

* Claude assistant entries split the input into fresh, cache-read, and
  cache-write token counts.  Their sum is the context sent for that turn.
* Codex ``token_count`` events carry ``last_token_usage`` for the latest model
  request and a separate cumulative ``total_token_usage``.  The former is the
  context reading.  Codex also records ``model_context_window``.
* Pi ``message`` entries whose ``message.role`` is ``assistant`` carry a
  ``usage`` block of ``input``, ``cacheRead``, ``cacheWrite`` and ``output``
  (camelCase, and ``input`` is the *uncached* part, so the three add up the
  way Claude's do).  The file is the one claunch pins with ``--session``
  (see ``daemon/harness.pi_session_file``); Pi records no context window.

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
selected by their own conversation ids.  Pi subagents write their own session
files under the same home, never into the parent's.

The same transcripts also answer how many tool calls the session made
lately (:func:`tool_calls_for_session`): the rail grades a busy session's dot
by it alongside the screen's own movement, because a turn running tool after
tool can repaint very little of the screen.

Subscription quota reporting is implemented by :mod:`claude_launcher.usage`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .. import harnesses as harness_registry
from .. import lineage, providers
from .. import profile as profile_mod
from . import codex_sessions
from .briefing import locate_transcript
from .harness import CLAUDE_HARNESS, PI_HARNESS, pi_session_file

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

#: How far back :func:`tool_calls_for_session` counts, in seconds. Five
#: minutes of 60 real Claude transcripts on this machine (2083 windows in
#: which the session answered at least once, 2026-09-23): median 7 calls,
#: p25 3, p75 15, p90 22. A shorter window reads zero between two slow tools.
TOOL_WINDOW = 300.0

#: path -> (mtime, size, tool-call times). Same identity rule as ``_reads``;
#: the times are kept rather than a count because the count depends on the
#: moment it is asked, and an unchanged file must not cost a re-read.
_tool_reads: Dict[str, Tuple[float, int, List[float]]] = {}

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

#: (profile, borrow) -> (the spec's context_window or None, when it was
#: resolved) -- :func:`declared_context_window`, held for ``WINDOW_TTL`` too.
_declared: Dict[Tuple[str, str], Tuple[Optional[int], float]] = {}


def forget() -> None:
    """Drop the caches. For tests, and for anything that moves a transcript."""
    _reads.clear()
    _tool_reads.clear()
    _located.clear()
    _windows.clear()
    _declared.clear()


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


def pi_usage_of(entry: dict) -> Optional[dict]:
    """Normalize one Pi session ``message`` entry, or ``None``.

    Only an assistant message with a usage block whose input side is not
    empty is a reading.  Pi writes ``input`` as the uncached portion and
    ``cacheRead``/``cacheWrite`` beside it (``totalTokens`` is their sum plus
    ``output``), so the context sent is the three added -- the same arithmetic
    as Claude's, under different names.  User messages, tool results and
    session bookkeeping (``session``, ``model_change``, ``compaction``) carry
    no usage and are skipped.
    """
    if not isinstance(entry, dict) or entry.get("type") != "message":
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    fresh = _int(usage.get("input"))
    cache_read = _int(usage.get("cacheRead"))
    cache_write = _int(usage.get("cacheWrite"))
    total = fresh + cache_read + cache_write
    if not total:
        return None
    return {
        "tokens": total,
        "input": fresh,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": _int(usage.get("output")),
        "model": str(msg.get("model") or "") or None,
        "at": str(entry.get("timestamp") or "") or None,
    }


def read_tail(path: Path, usage=usage_of) -> Optional[dict]:
    """The newest context reading in ``path``, or ``None``.

    ``usage`` is the per-entry normalizer -- Claude's by default, Pi's for a
    Pi session file; both formats are one reading per line, so the same
    reverse scan serves them.

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
            reading = usage(entry)
            if reading is not None:
                return reading
        if window >= size or window >= MAX_TAIL:
            return None
        window = min(window * 4, MAX_TAIL)


def read_pi_tail(path: Path) -> Optional[dict]:
    """The newest Pi context reading in ``path``, or ``None``."""
    return read_tail(path, pi_usage_of)


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


def _entry_time(entry: dict) -> Optional[float]:
    """The entry's top-level ``timestamp`` as epoch seconds, or ``None``.

    Claude, Codex and Pi all stamp every line with an ISO-8601 time there.
    """
    raw = entry.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def tool_calls_in(entry: dict) -> int:
    """How many tool calls one transcript line records, in any harness.

    Claude: ``tool_use`` blocks in an assistant message (sidechain entries
    are a subagent's and are skipped, as for the context reading). Codex: a
    ``response_item`` whose payload is a ``function_call`` or
    ``custom_tool_call``. Pi: ``toolCall`` blocks in an assistant message.
    """
    if entry.get("isSidechain"):
        return 0
    payload = entry.get("payload")
    if entry.get("type") == "response_item" and isinstance(payload, dict):
        return 1 if payload.get("type") in ("function_call", "custom_tool_call") else 0
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return 0
    content = msg.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        1 for block in content
        if isinstance(block, dict) and block.get("type") in ("tool_use", "toolCall")
    )


def read_tool_times(path: Path, since: float) -> Optional[List[float]]:
    """Times of the tool calls in ``path`` made at or after ``since``.

    Reads from the end and widens, like :func:`read_tail`, until a line
    older than ``since`` shows the window is covered or ``MAX_TAIL`` is
    spent. ``None`` when the file cannot be read.
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
        times: List[float] = []
        covered = False
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            at = _entry_time(entry)
            if at is None:
                continue
            if at < since:
                covered = True
                break
            times.extend([at] * tool_calls_in(entry))
        if covered or window >= size or window >= MAX_TAIL:
            return times
        window = min(window * 4, MAX_TAIL)


def tool_calls_for_session(sdef, now: Optional[float] = None) -> Optional[int]:
    """Tool calls this session made in the last :data:`TOOL_WINDOW` seconds.

    ``None`` when there is no transcript to read (unsupported harness, no
    conversation yet), so "not known" never renders as zero. Cached against
    the file's identity: the tail is read once per change of the file, and
    the count is taken against ``now`` at every ask. Everything older than
    the window before the file's own mtime is out of the window for every
    later ``now`` as well, so that is where the read may stop.
    """
    path = transcript_of(sdef)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _tool_reads.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        times = cached[2]
    else:
        read = read_tool_times(path, stat.st_mtime - TOOL_WINDOW)
        if read is None:
            return None
        times = read
        _tool_reads[key] = (stat.st_mtime, stat.st_size, times)
    at = time.time() if now is None else now
    return sum(1 for t in times if at - t <= TOOL_WINDOW)


def transcript_of(sdef) -> Optional[Path]:
    """This session's supported transcript, with path lookup caching."""
    harness = str(getattr(sdef, "harness", None) or CLAUDE_HARNESS)
    # The harnesses with a record this module can read. Anything else reads
    # as "not known", never as zero. (Membership is checked at call time so a
    # test may stand another name in for claude's.)
    if harness not in (CLAUDE_HARNESS, CODEX_HARNESS, PI_HARNESS):
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
            entry = harness_registry.get(harness)
            if entry is None:
                path = None
            elif harness == CODEX_HARNESS:
                path = codex_sessions.find(
                    entry.profile_home(prof.config_dir), str(cid)
                )
            else:
                # Pi: the file claunch itself named at launch (``--session``)
                # under the per-profile home the runner hands pi as its
                # PI_CODING_AGENT_DIR. No scan -- the path is a function of
                # (home, cwd, id), so a missing file is simply no reading yet.
                candidate = Path(pi_session_file(
                    str(entry.profile_home(prof.config_dir)),
                    os.path.abspath(str(getattr(sdef, "cwd", "") or os.getcwd())),
                    str(cid),
                ))
                path = candidate if candidate.is_file() else None
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
    harness = getattr(sdef, "harness", None)
    if harness == CODEX_HARNESS:
        reading = read_codex_tail(path)
    elif harness == PI_HARNESS:
        reading = read_pi_tail(path)
    else:
        reading = read_tail(path)
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
    """The spec's ``context_window`` for this session's backend, if any.

    Remembered for ``WINDOW_TTL`` per (profile, borrow), as
    :func:`compact_window_of` is: the resolution walks the profile chain and
    the provider registry through the config store, and the session list asked
    it again for every row on every poll -- the largest share of the GIL that
    list's worker held while the event loop waited (claunch-2t37a).
    """
    key = (str(getattr(sdef, "profile", "") or ""),
           str(getattr(sdef, "borrow", "") or ""))
    hit = _declared.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[1] < WINDOW_TTL:
        return hit[0]
    try:
        prof = profile_mod.require_selector(key[0])
        borrow = getattr(sdef, "borrow", None)
        auth = profile_mod.require(str(borrow)) if borrow else prof
        value = providers.spec_for(prof, providers.resolve_name(auth)).context_window
    except Exception:
        value = None
    _declared[key] = (value, now)
    return value


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
    # Tool calls in the last TOOL_WINDOW seconds, for the rail's busy grade.
    # Absent (not zero) when there is no transcript to count from.
    calls = tool_calls_for_session(sdef)
    if calls is not None:
        info["tool_calls"] = calls
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
