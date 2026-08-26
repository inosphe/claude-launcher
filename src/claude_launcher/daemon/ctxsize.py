"""How full one session's context is, read from the transcript it is writing.

With twenty sessions on a screen the operator's question is no longer "what is
this one doing" but "which of these is about to compact". Nothing in the
launcher knew: the daemon owns processes, not conversations. The number does
exist, though, and the harness itself reports it — every assistant turn claude
writes to its jsonl carries the ``usage`` block the API answered with, and the
input side of that block *is* the context that turn was sent.

So this module reads the tail of the same file :mod:`briefing` reads, finds the
newest assistant turn, and adds up what was sent::

    context = input_tokens + cache_read_input_tokens + cache_creation_input_tokens

The three are one number split by how it was billed (fresh, replayed from
cache, written to cache), not three different things — a session at 150k reads
almost all of it from cache and that is still 150k of context.

Three limits are deliberate, and the UI must say them rather than paper over
them:

* **It is the last completed turn, not now.** The file is appended when a turn
  finishes, so a session mid-answer still shows the previous number. That is
  why ``at`` travels with the count: an idle session's hour-old number is
  correct (nothing changed), and a busy one's is a floor.
* **There is no denominator.** Nothing in the transcript records the context
  limit, and it is not guessable — a claude-opus-5 session in this very fleet
  was observed at 286,674 input tokens, so any hardcoded 200k would already be
  wrong. Absolute tokens only, with the model beside them; a wrong percentage
  is worse than none. What *is* knowable is the session's configured
  ``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` — the point claude will compact at,
  resolved from the same env the child was spawned with — and
  :func:`compact_window_of` hangs that beside the reading as
  ``compact_window`` so the UI can draw it as a threshold, still never as a
  percentage of a hard limit.
* **Only claude sessions have one.** Another harness keeps no such file, and a
  claude session that has not answered yet has no turn to read. Both come back
  as ``None`` — "not known", never zero.

Sidechain entries (a subagent's own turns) are skipped: a subagent runs on its
own context, and counting its usage would report the wrong conversation's size
on the row of the session that spawned it.

Nothing here is authoritative about *cost* — that is
:mod:`claude_launcher.usage`, which asks the API about subscription windows and
is a different question that happens to share a word.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from .. import lineage, providers
from .. import profile as profile_mod
from .briefing import locate_transcript
from .harness import CLAUDE_HARNESS

#: How much of the file's end is read looking for the newest assistant turn.
#: One turn is small, but the lines before it are tool results and can be
#: large, so the read starts small and widens rather than paying for the worst
#: case on every poll.
FIRST_CHUNK = 64 * 1024
MAX_TAIL = 1024 * 1024

#: How long a *failed* lookup is remembered. Locating a transcript whose
#: expected path is empty scans every project directory
#: (:func:`transcripts.find`), which is a fine safety net once and a waste
#: twenty times a second — but it must expire, or a session that only starts
#: answering later would stay blank forever.
MISS_TTL = 15.0

#: path -> (mtime, size, reading). The file's identity is what would change
#: the answer, so an unchanged file costs one stat and nothing else. A daemon
#: restart empties it, which is fine: the next poll reads again.
_reads: Dict[str, Tuple[float, int, Optional[dict]]] = {}

#: (profile, conversation, cwd) -> (path or None, when it was looked up).
_located: Dict[Tuple[str, str, str], Tuple[Optional[Path], float]] = {}

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


def transcript_of(sdef) -> Optional[Path]:
    """This session's transcript, remembered so the fallback scan stays rare."""
    if getattr(sdef, "harness", None) != CLAUDE_HARNESS:
        return None
    cid = getattr(sdef, "conversation_id", None)
    if not cid:
        return None
    key = (str(getattr(sdef, "profile", "") or ""), str(cid),
           str(getattr(sdef, "cwd", "") or ""))
    hit = _located.get(key)
    now = time.monotonic()
    if hit is not None:
        path, when = hit
        if path is not None and path.is_file():
            return path
        if path is None and now - when < MISS_TTL:
            return None
    path = locate_transcript(sdef)
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
    if getattr(sdef, "harness", None) != CLAUDE_HARNESS:
        return None
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
    try:
        prof = profile_mod.require_selector(
            str(getattr(sdef, "profile", "") or "")
        )
        if not getattr(sdef, "null_token", False):
            borrow = getattr(sdef, "borrow", None)
            auth = profile_mod.require(str(borrow)) if borrow else prof
            raw = providers.provider_env(
                providers.resolve_name(auth)).get(COMPACT_WINDOW_ENV, raw)
        raw = lineage.effective_env(prof).get(COMPACT_WINDOW_ENV, raw)
    except Exception:
        pass
    value = _window_value(raw)
    _windows[key] = (value, now)
    return value


def attach(session) -> dict:
    """``session.info()`` with a ``context`` key when there is one to give.

    The key is absent rather than null when unknown, so a reader that draws it
    cannot accidentally render "not known" as a number. When a reading exists
    it also carries ``compact_window`` (when one is configured): the reading
    is the numerator, and the compact threshold is the one denominator-like
    fact that actually exists — the point claude will compact at, not the
    model's unknowable hard limit. Copied, not annotated in place: the
    reading is a cache entry shared across polls.
    """
    info = session.info()
    sdef = getattr(session, "sdef", None)
    reading = for_session(sdef)
    if reading:
        info["context"] = dict(reading)
        window = compact_window_of(sdef)
        if window:
            info["context"]["compact_window"] = window
    return info
