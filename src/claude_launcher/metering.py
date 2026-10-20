"""Per-request throughput records for API-key providers.

Sessions on an OAuth route (the Anthropic subscription, Codex's login) talk to
their backend directly and are out of scope here. Every provider that carries
its own key (``service: custom`` — DeepSeek, Fireworks, OpenRouter, a Kimi
key, ...) is instead launched with ``ANTHROPIC_BASE_URL`` swung to the local
shim from :mod:`routing`, and the shim writes one JSON line per request it
forwards: when it started, when the first byte and the first token came back,
when it ended, which model answered, the token counts the response reported,
and the tokens-per-second those numbers give.

This module is the part that does not need a socket: the config switches, the
incremental response reader the shim feeds, the record files, and the
aggregation ``claunch tps`` prints. The shim (:mod:`routing_shim`) only calls
into it.

Two wire formats come through: Anthropic Messages (``/v1/messages``, what
Claude Code speaks) and OpenAI Chat Completions (``/v1/chat/completions``,
what the ``pi`` harness speaks). The reader tells them apart by the body — an
Anthropic event has a ``type``, an OpenAI chunk an ``object`` of
``chat.completion...`` — and writes the same record for both, so ``claunch
tps`` does not care which harness made the call.

Which session sent a request is not something a shared shim can see — one
shim serves every session on the same upstream. The daemon therefore hands
each session a private request header through Claude Code's
``ANTHROPIC_CUSTOM_HEADERS``; the shim strips it before forwarding and writes
the session name into the record.

Records live under ``<launcher home>/metering/<fingerprint>.jsonl``. Switch
the whole thing off with a top-level ``metering: false`` in the config file,
or one provider with ``providers.<name>.metering: false``.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import config, store

#: Top-level config key (``metering: false`` disables every shim's records).
CONFIG_KEY = "metering"

#: Process-level override of :data:`CONFIG_KEY` (``0`` off, ``1`` on).
ENV_OVERRIDE = "CLAUNCH_METERING"

#: Key under a dict-valued ``metering:`` that overrides :data:`SUMMARY_MAX_AGE`.
MAX_AGE_KEY = "summary_max_age"

#: Request header the daemon adds so the shim can attribute a request.
SESSION_HEADER = "x-claunch-session"

#: Claude Code's hook for extra request headers: newline-separated
#: ``Name: value`` lines.
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"

#: Non-streaming bodies are buffered up to this many bytes for the usage
#: lookup; a bigger one is forwarded as-is and recorded without token counts.
BUFFER_LIMIT = 8 * 1024 * 1024

#: How a record's ``ts`` is written and read back: local time with its own UTC
#: offset. The offset is what makes an age computable from a file that may
#: carry records written on either side of a clock change.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def enabled(doc: Optional[dict] = None) -> bool:
    """Whether metering is on at all (default: yes).

    ``CLAUNCH_METERING=0`` in the environment switches it off for that
    process regardless of the config file (``1`` forces it on) — for a shell
    that wants a direct connection, and for the test suite, whose env
    assembly tests would otherwise each start a proxy.
    """
    override = os.environ.get(ENV_OVERRIDE, "").strip().lower()
    if override in ("0", "false", "no", "off"):
        return False
    if override in ("1", "true", "yes", "on"):
        return True
    doc = store.load() if doc is None else doc
    value = doc.get(CONFIG_KEY, True)
    if isinstance(value, dict):
        value = value.get("enabled", True)
    return bool(value)


def provider_enabled(provider_name: str, doc: Optional[dict] = None) -> bool:
    """Whether ``provider_name`` opted out with ``metering: false``."""
    doc = store.load() if doc is None else doc
    if not enabled(doc):
        return False
    section = doc.get("providers")
    entry = section.get(provider_name) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        return True
    return bool(entry.get(CONFIG_KEY, True))


def records_dir() -> Path:
    return config.launcher_home() / "metering"


def record_file(fingerprint: str) -> Path:
    return records_dir() / f"{fingerprint}.jsonl"


# --------------------------------------------------------------------------- #
# the session header
# --------------------------------------------------------------------------- #
def apply_session_header(env: dict, session: str) -> None:
    """Add ``X-Claunch-Session: session`` to the env's custom headers.

    Lines already there (a user's own ``ANTHROPIC_CUSTOM_HEADERS``) are kept;
    an earlier session line is replaced so a restored session never reports
    under a stale name.
    """
    kept = [
        line
        for line in (env.get(CUSTOM_HEADERS_ENV) or "").splitlines()
        if line.strip() and not line.lower().startswith(SESSION_HEADER + ":")
    ]
    kept.append(f"X-Claunch-Session: {session}")
    env[CUSTOM_HEADERS_ENV] = "\n".join(kept)


# --------------------------------------------------------------------------- #
# reading a response as it streams through
# --------------------------------------------------------------------------- #
def _usage_fields(usage: dict) -> Dict[str, Optional[int]]:
    """The record's four counts out of either protocol's ``usage`` object.

    Anthropic: ``input_tokens`` / ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` / ``output_tokens``. OpenAI:
    ``prompt_tokens`` (which *includes* the cached part) /
    ``prompt_tokens_details.cached_tokens`` / ``completion_tokens``; OpenAI
    has no cache-write count.
    """
    def _int(doc: dict, key: str) -> Optional[int]:
        value = doc.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "input_tokens": _int(usage, "input_tokens")
        if "input_tokens" in usage
        else _int(usage, "prompt_tokens"),
        "cache_read": _int(usage, "cache_read_input_tokens")
        if "cache_read_input_tokens" in usage
        else _int(details, "cached_tokens"),
        "cache_write": _int(usage, "cache_creation_input_tokens"),
        "output_tokens": _int(usage, "output_tokens")
        if "output_tokens" in usage
        else _int(usage, "completion_tokens"),
    }


@dataclass
class Meter:
    """Watches one response go by and turns it into a record.

    Feed it ``chunk`` for every piece of the body the shim forwards, then
    ``finish``. Streamed (SSE) bodies are parsed event by event, so nothing is
    held back. Anthropic: ``message_start`` carries the model and input-side
    usage, the first ``content_block_delta`` marks the first token,
    ``message_delta`` carries the output count. OpenAI: every
    ``chat.completion.chunk`` carries the model, the first one with a
    non-empty ``delta`` marks the first token, and the last one (sent only
    when the request asked for ``stream_options.include_usage``) carries
    ``usage``. A non-streamed JSON body of either shape is buffered and read
    at the end.

    ``streamed`` records whether any content delta actually came through,
    which is what decides whether a generation rate can be computed at all —
    see :func:`finish`. It is not the same as ``stream``: that one is the
    response's content type, and an SSE body that ends before a single delta
    (an error event, a refusal) is streamed without ever being generated.
    """

    session: Optional[str]
    path: str
    started: float = field(default_factory=time.monotonic)
    model: Optional[str] = None
    stream: Optional[bool] = None
    status: Optional[int] = None
    encoding: Optional[str] = None
    first_byte: Optional[float] = None
    first_token: Optional[float] = None
    last_token: Optional[float] = None
    streamed: bool = False
    usage: Dict[str, Optional[int]] = field(default_factory=dict)
    _sse_tail: bytes = b""
    _buffer: bytearray = field(default_factory=bytearray)
    _overflow: bool = False
    _chunk_content: bool = False
    #: Characters of generated content seen so far (text, thinking, tool-call
    #: arguments). Only the live view reads it: it is what the rate of a call
    #: still in flight is estimated from, the usage block arriving at the end.
    out_chars: int = 0

    def headers(self, status: int, headers) -> None:
        """Called once the upstream answered (before any body byte)."""
        self.status = status
        ctype = (headers.get("content-type") or "").lower()
        self.stream = "text/event-stream" in ctype
        enc = (headers.get("content-encoding") or "").lower().strip()
        self.encoding = enc or None
        self.first_byte = time.monotonic()

    def chunk(self, data: bytes) -> None:
        if not data:
            return
        # One timestamp per read, handed down to the parser: everything this
        # read carried was observed to arrive at this instant, so the first and
        # the last content delta of a single read share it exactly. That is
        # what makes their window zero rather than a sliver of parse time.
        now = time.monotonic()
        if self.first_byte is None:
            self.first_byte = now
        if self.encoding not in (None, "identity"):
            return  # compressed: we forward it untouched and count nothing
        if self.stream:
            self._feed_read(data, now)
        elif not self._overflow:
            if len(self._buffer) + len(data) > BUFFER_LIMIT:
                self._overflow = True
                self._buffer = bytearray()
            else:
                self._buffer.extend(data)

    # -- SSE ---------------------------------------------------------------- #
    def _feed_read(self, data: bytes, now: float) -> None:
        """Feed one read, and mark when content was seen in it.

        The mark is per read and not per delta on purpose: two deltas parsed
        out of the same read reached us at the same instant, and the rate
        below is meant to be divided by the time content was *observed*
        arriving, not by how long it took to parse.
        """
        self._chunk_content = False
        self._feed_sse(data, now)
        if self._chunk_content:
            self.last_token = now

    def _feed_sse(self, data: bytes, now: float) -> None:
        buf = self._sse_tail + data
        while True:
            # An event ends at a blank line; either newline convention.
            idx_n = buf.find(b"\n\n")
            idx_rn = buf.find(b"\r\n\r\n")
            candidates = [i for i in (idx_n, idx_rn) if i >= 0]
            if not candidates:
                break
            end = min(candidates)
            sep = 4 if end == idx_rn else 2
            self._event(buf[:end], now)
            buf = buf[end + sep:]
        self._sse_tail = buf

    def _event(self, raw: bytes, now: float) -> None:
        data_lines = []
        for line in raw.splitlines():
            if line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            return
        try:
            doc = json.loads(b"\n".join(data_lines).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(doc, dict):
            return
        if str(doc.get("object") or "").startswith("chat.completion"):
            self._openai_chunk(doc, now)
            return
        kind = doc.get("type")
        if kind == "message_start":
            message = doc.get("message")
            if isinstance(message, dict):
                self._take_message(message)
        elif kind == "content_block_delta":
            if self.first_token is None:
                self.first_token = now
            self.streamed = True
            self._chunk_content = True
            delta = doc.get("delta")
            if isinstance(delta, dict):
                self.out_chars += _delta_chars(delta, ("text", "thinking", "partial_json"))
        elif kind == "message_delta":
            usage = doc.get("usage")
            if isinstance(usage, dict):
                self._merge_usage(usage)

    def _openai_chunk(self, doc: dict, now: float) -> None:
        """One ``chat.completion.chunk`` (or a whole ``chat.completion``)."""
        self._take_message(doc)
        if self.streamed:
            # Already counting: this chunk settles only whether the content is
            # still arriving, which ``_feed_read`` reads off ``_chunk_content``.
            if self._chunk_carries_content(doc):
                self._chunk_content = True
                self.out_chars += self._chunk_chars(doc)
            return
        if self._chunk_carries_content(doc):
            self.first_token = now
            self.streamed = True
            self._chunk_content = True
            self.out_chars += self._chunk_chars(doc)

    @staticmethod
    def _chunk_carries_content(doc: dict) -> bool:
        choices = doc.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and any(
                delta.get(key) for key in ("content", "reasoning_content", "tool_calls")
            ):
                return True
        return False

    @staticmethod
    def _chunk_chars(doc: dict) -> int:
        n = 0
        choices = doc.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            n += _delta_chars(delta, ("content", "reasoning_content"))
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    n += _delta_chars(fn, ("arguments",))
        return n

    # -- JSON --------------------------------------------------------------- #
    def _take_message(self, message: dict) -> None:
        model = message.get("model")
        if isinstance(model, str) and model:
            self.model = model
        usage = message.get("usage")
        if isinstance(usage, dict):
            self._merge_usage(usage)

    def _merge_usage(self, usage: dict) -> None:
        for key, value in _usage_fields(usage).items():
            if value is not None:
                self.usage[key] = value

    def _read_buffer(self) -> None:
        if self._overflow or not self._buffer:
            return
        try:
            doc = json.loads(bytes(self._buffer).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(doc, dict):
            self._take_message(doc)
            # No first-token mark is taken here. A whole JSON answer arrives
            # in one chunk, so any mark read off the arrival would sit at the
            # end of the call and leave the generation rate dividing by the
            # network's sliver -- an inflated rate, not a missing one. The
            # record keeps ``ttfb_ms`` for the wait and reports no generation
            # rate at all; see ``finish`` and ``reported_tps``.

    # -- the record --------------------------------------------------------- #
    def finish(self, ended: Optional[float] = None) -> dict:
        ended = time.monotonic() if ended is None else ended
        if not self.stream:
            self._read_buffer()
        elif self._sse_tail.strip():
            # A last event with no blank line after it: whatever it carried
            # reached us by the time the body ended.
            self._feed_read(self._sse_tail, ended)
            self._sse_tail = b""
        out_tokens = self.usage.get("output_tokens")
        total_s = ended - self.started
        # The generation window runs from the content that was seen first to
        # the content that was seen last -- not to the end of the body, which
        # carries trailing events, and not to the end of the call, which for an
        # answer that did not stream carries the whole wait for it.
        #
        # A rate needs that window to be positive, and a window is only
        # positive when the content was seen arriving in more than one read.
        # Content that arrived in a single read was never observed to take any
        # time, and dividing by the parse time of the read that carried it
        # invents a rate -- which is how a 27-token answer came to be recorded
        # at 435483 tok/s and a 5-token one at 26709. Neither answer has a
        # generation rate to give, so the record says null and keeps
        # ``tps_total``, the whole call's own average.
        gen_s = (
            self.last_token - self.first_token
            if self.first_token is not None and self.last_token is not None
            else None
        )
        tps = None
        if self.streamed and out_tokens and gen_s is not None and gen_s > 0:
            tps = round(out_tokens / gen_s, 2)
        tps_total = None
        if out_tokens and total_s > 0:
            tps_total = round(out_tokens / total_s, 2)
        return {
            "ts": time.strftime(TS_FORMAT),
            "session": self.session,
            "path": self.path,
            "status": self.status,
            "stream": bool(self.stream),
            # Not the same fact as ``stream``: an SSE answer that ended before
            # a single delta streamed without generating, and has no rate.
            "streamed": bool(self.streamed),
            # The window ``tps`` was divided by, in whole milliseconds. Written
            # so a reader can tell a rate that was measured from one left
            # behind by a formula that divided by nothing.
            "generation_ms": int(gen_s * 1000) if gen_s is not None and gen_s > 0 else None,
            "model": self.model,
            "input_tokens": self.usage.get("input_tokens"),
            "cache_read": self.usage.get("cache_read"),
            "cache_write": self.usage.get("cache_write"),
            "output_tokens": out_tokens,
            "ttfb_ms": _ms(self.first_byte, self.started),
            "ttft_ms": _ms(self.first_token, self.started),
            "total_ms": int(total_s * 1000),
            "tps": tps,
            "tps_total": tps_total,
            "counted": out_tokens is not None,
        }


def _delta_chars(delta: dict, keys) -> int:
    return sum(len(v) for v in (delta.get(k) for k in keys) if isinstance(v, str))


def _ms(later: Optional[float], earlier: float) -> Optional[int]:
    return None if later is None else int((later - earlier) * 1000)


# --------------------------------------------------------------------------- #
# reading a record's throughput
# --------------------------------------------------------------------------- #
def reported_tps(record: dict) -> Optional[float]:
    """The one rate to show for ``record``: its tokens over the whole call.

    The window starts at the request, which is the one window every call is
    guaranteed to have. Nothing has to be observed arriving for it to exist, so
    it is never zero, and it carries the wait for the first token -- which is
    what keeps the number bounded when the upstream generated the answer ahead
    of time and then flushed it, since the flush is clocked against the whole
    call rather than against itself.

    That last property is why this and not the generation rate is the number a
    reader shows. The generation rate measured on a record is the backend's own
    speed, and it is the more interesting number, but it can only be measured
    from an answer that was watched arriving a piece at a time: whether one was
    is a property of that call, so two rows would carry rates on different
    bases and could not be compared. :func:`generation_tps` is still there for
    a reader that wants the backend's speed and will say which calls have it.

    ``None`` when the call was not counted at all.
    """
    return _call_tps(record)


def generation_tps(record: dict) -> Optional[float]:
    """``record``'s rate over the generation, or ``None`` when it has none.

    Decided from the response's own shape rather than from the presence of the
    field, so that a record written before this rule was fixed is read
    correctly without rewriting the file. Two residues have to be turned away,
    and neither is trusted on ``tps`` alone: an answer that did not stream, and
    an answer whose content arrived in one read -- both were divided by a
    window that measured nothing. ``_timed_generation`` is that judgement.
    """
    if not record.get("stream"):
        return None
    tps = record.get("tps")
    if not tps or not _timed_generation(record):
        return None
    return float(tps)


def _call_tps(record: dict) -> Optional[float]:
    if not record.get("output_tokens"):
        return None
    total = record.get("tps_total")
    return float(total) if total else None


def _timed_generation(record: dict) -> bool:
    """Whether the window behind ``record``'s generation rate was worth dividing by.

    ``generation_ms`` is written by the reader from here on and answers this
    directly. A record that has none was written before the window was measured
    on its own, and there the window was ``ended - first_token`` -- which are
    exactly the record's ``total_ms`` and ``ttft_ms``. Their difference is the
    window, but both are rounded to whole milliseconds, so it carries up to a
    millisecond of error either way; a difference below two does not
    establish that a millisecond passed, and the answer may just as well have
    arrived in a single read.

    A record that says nothing either way is given the benefit of the doubt;
    the alternative is to throw away a rate that may be the only one there is.
    """
    ms = record.get("generation_ms")
    if ms is not None:
        return ms > 0
    total, ttft = record.get("total_ms"), record.get("ttft_ms")
    if total is None or ttft is None:
        return True
    return total - ttft >= 2


# --------------------------------------------------------------------------- #
# record files
# --------------------------------------------------------------------------- #
def append(fingerprint: str, record: dict, *, upstream: Optional[str] = None) -> None:
    """Append one record line; never raises (a full disk must not break a proxy)."""
    line = dict(record)
    line["fingerprint"] = fingerprint
    if upstream:
        line["upstream"] = upstream
    try:
        records_dir().mkdir(parents=True, exist_ok=True)
        with open(record_file(fingerprint), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError:
        pass


def load(
    *,
    session: Optional[str] = None,
    upstream: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """Records across every fingerprint, oldest first, filtered."""
    out: List[dict] = []
    try:
        files = sorted(records_dir().glob("*.jsonl"))
    except OSError:
        return out
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if session and rec.get("session") != session:
                continue
            if upstream and upstream not in str(rec.get("upstream") or ""):
                continue
            out.append(rec)
    out.sort(key=lambda r: str(r.get("ts") or ""))
    if limit is not None and limit >= 0:
        out = out[-limit:] if limit else []
    return out


def clear() -> int:
    """Delete every record file; returns how many were removed."""
    n = 0
    try:
        files = list(records_dir().glob("*.jsonl"))
    except OSError:
        return 0
    for path in files:
        try:
            path.unlink()
            n += 1
        except OSError:
            pass
    return n


# --------------------------------------------------------------------------- #
# aggregation (what `claunch tps` prints)
# --------------------------------------------------------------------------- #
def summarize(records: Iterable[dict]) -> dict:
    """Counts, token totals and TPS/TTFT medians over ``records``.

    The numbers under ``tps_*`` are :func:`reported_tps`, the whole-call rate --
    one definition over every call, which is what makes them comparable. The
    generation rate is a different measurement and gets ``generation_*``; a
    median over the two at once would be a median of nothing.
    """
    recs = list(records)
    tps = [v for v in (reported_tps(r) for r in recs) if v is not None]
    generation = [v for v in (generation_tps(r) for r in recs) if v is not None]
    ttft = [float(r["ttft_ms"]) for r in recs if r.get("ttft_ms") is not None]
    out_tokens = sum(int(r.get("output_tokens") or 0) for r in recs)
    in_tokens = sum(int(r.get("input_tokens") or 0) for r in recs)
    cache_read = sum(int(r.get("cache_read") or 0) for r in recs)
    return {
        "requests": len(recs),
        "counted": len(tps),
        "output_tokens": out_tokens,
        "input_tokens": in_tokens,
        "cache_read": cache_read,
        "tps_median": round(statistics.median(tps), 2) if tps else None,
        "tps_mean": round(statistics.fmean(tps), 2) if tps else None,
        "tps_min": round(min(tps), 2) if tps else None,
        "tps_max": round(max(tps), 2) if tps else None,
        "generation_n": len(generation),
        "generation_median": round(statistics.median(generation), 2) if generation else None,
        "ttft_ms_median": int(statistics.median(ttft)) if ttft else None,
    }


def by_key(records: Iterable[dict], key: str) -> Dict[str, dict]:
    """``summarize`` per distinct value of ``key`` (``model``, ``session``...)."""
    groups: Dict[str, List[dict]] = {}
    for rec in records:
        groups.setdefault(str(rec.get(key) or "-"), []).append(rec)
    return {name: summarize(recs) for name, recs in sorted(groups.items())}


# --------------------------------------------------------------------------- #
# what the web UI shows (a session's latest throughput)
# --------------------------------------------------------------------------- #
#: How many bytes off the end of a record file the UI reader looks at. A
#: record is ~350 bytes, so this is the last few hundred calls per shim --
#: plenty for "what is this session doing now", and a bounded read however
#: long the file has grown.
TAIL_BYTES = 256 * 1024

#: The rolling window ``session_summary`` computes its median over: how many
#: calls at most. On its own this is not a window -- a session that called ten
#: times over a day and a half has its "last 10 calls" spread across that day
#: and a half -- so :data:`SUMMARY_MAX_AGE` bounds the same window in time and
#: the summary is taken over what satisfies both.
SUMMARY_WINDOW = 10

#: The other axis of that window: how long a call keeps describing a session,
#: in seconds. A session whose newest call is older than this has no current
#: throughput to show, and ``session_summary`` says so (``idle``) instead of
#: leaving the last number standing.
#:
#: Ten minutes is where the UI already stopped believing the number -- it drew
#: a reading older than that dimmed -- so this makes the reading go away at
#: the point it was already being disclaimed, rather than at a new one.
#: Override with ``metering.summary_max_age``; ``0`` switches the bound off.
SUMMARY_MAX_AGE = 600.0


def summary_max_age(doc: Optional[dict] = None) -> float:
    """How long a call keeps describing a session, in seconds.

    :data:`SUMMARY_MAX_AGE` unless the config file overrides it with
    ``metering.summary_max_age`` (the same dict-valued ``metering:`` section
    ``enabled`` reads). ``0`` switches the bound off, which restores the
    count-only window this had before -- available for a reader who wants the
    last ten calls whenever they happened, and not the default, because that
    window has no upper bound in time.

    A value that is not a number is ignored rather than raising: this is read
    on the daemon's session poll, and a typo in the config file must not stop
    the session list from being served.
    """
    doc = store.load() if doc is None else doc
    section = doc.get(CONFIG_KEY)
    if isinstance(section, dict) and section.get(MAX_AGE_KEY) is not None:
        try:
            return max(0.0, float(section[MAX_AGE_KEY]))
        except (TypeError, ValueError):
            return SUMMARY_MAX_AGE
    return SUMMARY_MAX_AGE


#: Per-session summary key hung on the session record (see :func:`attach`).
INFO_KEY = "tps"

_tail_cache: Dict[str, tuple] = {}
_tail_lock = threading.Lock()


def _tail(path: Path) -> tuple:
    """``(key, records, by_session)`` for the end of one file, re-read only
    when the file changed.

    Keyed on (mtime, size): the daemon polls the session list every few
    seconds for every session, and an unchanged file must cost a ``stat``,
    not a parse. ``by_session`` is built with the parse, once per change:
    filtering the records per session instead was one pass over every
    record for every row of the list -- 850 rows by 2,824 records, about
    160ms of pure Python holding the GIL on each list build while the
    daemon's event loop waited for it (claunch-vyw9s).
    """
    try:
        st = path.stat()
    except OSError:
        return (None, [], {})
    key = (st.st_mtime_ns, st.st_size)
    with _tail_lock:
        hit = _tail_cache.get(str(path))
        if hit and hit[0] == key:
            return hit
    out: List[dict] = []
    try:
        with open(path, "rb") as fh:
            if st.st_size > TAIL_BYTES:
                fh.seek(st.st_size - TAIL_BYTES)
                fh.readline()  # drop the partial line the seek landed in
            data = fh.read()
    except OSError:
        return (None, out, {})
    by_session: Dict[str, List[dict]] = {}
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
            by_session.setdefault(rec.get("session"), []).append(rec)
    entry = (key, out, by_session)
    with _tail_lock:
        _tail_cache[str(path)] = entry
    return entry


def _tail_records(path: Path) -> List[dict]:
    """The records at the end of one file (see :func:`_tail`)."""
    return _tail(path)[1]


def snapshot() -> Dict[str, List[dict]]:
    """Every session's tail records across all record files, oldest first.

    One directory listing and one ``stat`` per file, however many sessions
    are then looked up in it: a caller answering many sessions at once (the
    session list) takes one snapshot and hands it to :func:`attach` for
    each row, rather than listing the directory once per row.
    """
    merged: Dict[str, List[dict]] = {}
    try:
        files = sorted(records_dir().glob("*.jsonl"))
    except OSError:
        return merged
    for path in files:
        for session, recs in _tail(path)[2].items():
            merged.setdefault(session, []).extend(recs)
    for recs in merged.values():
        recs.sort(key=lambda r: str(r.get("ts") or ""))
    return merged


def recent(
    session: str,
    *,
    limit: int = SUMMARY_WINDOW,
    index: Optional[Dict[str, List[dict]]] = None,
) -> List[dict]:
    """The last ``limit`` records ``session`` made, oldest first.

    ``index`` is a :func:`snapshot` already taken; left out, one is taken
    here.
    """
    recs = (snapshot() if index is None else index).get(session) or []
    return recs[-limit:] if limit else list(recs)


def record_age(record: dict, *, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since ``record`` was written, or ``None`` when it cannot be read.

    ``ts`` is local time carrying its own UTC offset (:data:`TS_FORMAT`), so
    the subtraction is correct for a file that spans a clock change or was
    carried over from another offset.

    A stamp that will not parse gives ``None`` rather than a large age: not
    being able to read a clock is not evidence that the call was old, and the
    callers below treat ``None`` as "leave it in the window".
    """
    stamp = record.get("ts")
    if not stamp:
        return None
    try:
        when = datetime.strptime(str(stamp), TS_FORMAT)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    now = datetime.now(timezone.utc) if now is None else now
    return (now - when).total_seconds()


def _in_window(record: dict, now: Optional[datetime], max_age: float) -> bool:
    """Whether ``record`` is recent enough to be part of the current window."""
    if not max_age:
        return True
    age = record_age(record, now=now)
    return age is None or age <= max_age


def session_summary(
    session: str,
    *,
    now: Optional[datetime] = None,
    max_age: Optional[float] = None,
    index: Optional[Dict[str, List[dict]]] = None,
) -> Optional[dict]:
    """What a session row shows: its latest call and a short rolling median.

    The window is bounded on both axes -- at most :data:`SUMMARY_WINDOW`
    calls, and only those made within :func:`summary_max_age` seconds. The
    count alone was not a window: measured on this machine's own records, a
    live session's "last 10 calls" spanned 124349 seconds (34.5 hours), and a
    median over calls 34 hours apart describes no moment at all.

    Three answers, and they are three different facts:

    * ``None`` -- the session has no records. Absence, so a reader cannot
      draw "never measured" as a slow session; :func:`attach` leaves the key
      off entirely.
    * ``idle`` true -- it has records and none is recent enough to describe
      now. The row keeps saying when the session last called out and on what,
      and carries no rate at all. The previous behaviour was to keep showing
      the last number, which is how a session that had been quiet for an hour
      still read as a throughput.
    * ``idle`` false -- the rates are from the bounded window above.

    ``tps`` is the last call's :func:`reported_tps` -- tokens over the whole
    call, one definition for every row. ``last`` is the newest record whether
    or not it was counted (an error answer still says when the session last
    called out); the medians are over the counted ones in the window.

    ``now`` and ``max_age`` are injection points for tests and for a caller
    that already knows both; left out, the clock and the config file answer.
    ``index`` is a :func:`snapshot` the caller already took.
    """
    recs = recent(session, limit=SUMMARY_WINDOW, index=index)
    if not recs:
        return None
    max_age = summary_max_age() if max_age is None else max(0.0, float(max_age))
    last = recs[-1]
    age = record_age(last, now=now)
    # Records arrive oldest first, so a newest one that is out of the window
    # puts every one of them out of it: one test answers both questions.
    window = [r for r in recs if _in_window(r, now, max_age)] if max_age else recs
    out = {
        "ts": last.get("ts"),
        "model": last.get("model"),
        "status": last.get("status"),
        "counted": bool(last.get("counted")),
        "idle": not window,
        "age_s": int(age) if age is not None else None,
        "max_age_s": int(max_age) if max_age else None,
        "window": len(window),
        "upstream": last.get("upstream"),
    }
    if not window:
        # Every rate field is present and empty rather than missing, so a
        # reader that only knows the old shape draws no number either.
        out.update(
            tps=None, ttft_ms=None, ttfb_ms=None,
            output_tokens=None, input_tokens=None, cache_read=None,
            tps_median=None, tps_median_n=0, ttft_ms_median=None,
        )
        return out
    tps = [v for v in (reported_tps(r) for r in window) if v is not None]
    ttft = [float(r["ttft_ms"]) for r in window if r.get("ttft_ms") is not None]
    out.update(
        tps=reported_tps(last),
        ttft_ms=last.get("ttft_ms"),
        ttfb_ms=last.get("ttfb_ms"),
        output_tokens=last.get("output_tokens"),
        input_tokens=last.get("input_tokens"),
        cache_read=last.get("cache_read"),
        tps_median=round(statistics.median(tps), 2) if tps else None,
        tps_median_n=len(tps),
        ttft_ms_median=int(statistics.median(ttft)) if ttft else None,
    )
    return out


def attach(
    info: dict,
    index: Optional[Dict[str, List[dict]]] = None,
    max_age: Optional[float] = None,
    live: Optional[Dict[str, List[dict]]] = None,
    groups: Optional[Dict[str, dict]] = None,
) -> dict:
    """``info`` (a session record) with a ``tps`` key when there is one to give.

    The same shape of hook as ``daemon/ctxsize.attach``: the key is absent
    rather than null when the session has no records, so the UI draws
    nothing rather than a zero.

    A session that has records but has been quiet longer than
    :func:`summary_max_age` still gets the key, carrying ``idle`` and no
    rate. The two are different facts and the UI draws them differently:
    nothing at all for a session that was never measured, "none" plus the age
    of the last call for one that has gone quiet.

    ``index``: a :func:`snapshot` shared by every row of one list build.
    ``max_age``: :func:`summary_max_age`, read once by that build -- each read
    is a copy of the whole config document (claunch-2t37a).
    ``live``: a :func:`live_index` taken by that build. When the session has a
    live file, the key carries ``live`` (:func:`live_brief`): whether a call is
    in flight right now, which the finished-call summary cannot say. A session
    whose first call is still in flight has ``live`` and nothing else.
    ``groups``: :func:`live_groups` over ``live``, taken once by that build;
    with it, ``live.parallel`` says how the session's shim is shared now.
    """
    name = info.get("name")
    if name:
        summary = session_summary(str(name), index=index, max_age=max_age)
        brief = live_brief(str(name), live, groups=groups) if live is not None else None
        if brief:
            summary = dict(summary or {})
            summary["live"] = brief
        if summary:
            info[INFO_KEY] = summary
    return info


# --------------------------------------------------------------------------- #
# live view: the calls still in flight (claunch-2fut4)
# --------------------------------------------------------------------------- #
# A record is written when a call ends, so everything above describes calls
# that are over: a two-minute generation shows nothing for two minutes, and the
# last number then stands for ``summary_max_age`` seconds after it. The live
# view is the other half. Each shim keeps, per session, the calls it is
# forwarding right now and the output they produced in short time bins, and
# rewrites one small file per session a few times a second while anything is in
# flight. The daemon reads those files on demand (``live_summary``): a rate over
# the last couple of seconds, a short series for a chart, and -- when nothing is
# in flight -- ``idle``, said outright.
#
# The output of a call in flight is counted in characters, because the token
# count arrives only with the usage block at the end. Characters become tokens
# through a per-session ratio taken from that session's own finished calls
# (``LIVE_TOKENS_PER_CHAR`` until there is one), so the live number is an
# estimate and is labelled as one; the record written at the end stays exact.

#: Width of one time bin, in seconds.
LIVE_BIN_S = 0.5

#: How much history a live file keeps, in seconds (the chart's width).
LIVE_SPAN_S = 30.0

#: How often the shim rewrites the files of sessions with a call in flight.
LIVE_FLUSH_S = 0.25

#: A live file not rewritten for this long says nothing about calls in flight:
#: the shim that wrote it stopped (killed mid-call, or replaced by an older
#: build). Its bins are still history; its in-flight counts are not believed.
LIVE_STALE_S = 3.0

#: The window the live rate is taken over, in seconds.
LIVE_RATE_WINDOW_S = 2.0

#: Tokens per character before a session has a finished call to calibrate on.
LIVE_TOKENS_PER_CHAR = 0.25

#: A finished call recalibrates the ratio only when it streamed at least this
#: many characters: a few characters against a token count carries mostly the
#: count's rounding.
LIVE_CALIBRATE_MIN_CHARS = 200


def live_dir() -> Path:
    return records_dir() / "live"


def _live_key(session: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in session)


def live_file(fingerprint: str, session: str) -> Path:
    return live_dir() / f"{_live_key(session)}@{fingerprint}.json"


class LiveBoard:
    """The calls one shim is forwarding now, per session, and their output bins.

    Driven by the shim: :meth:`begin` when a metered request goes out,
    :meth:`end` when it is over (with the record :meth:`Meter.finish` wrote),
    and :meth:`flush` on a short timer. Not thread-safe; the shim calls it
    from its one event loop.
    """

    def __init__(self, fingerprint: str, *, clock=time.time) -> None:
        self.fingerprint = fingerprint
        self._clock = clock
        self._sessions: Dict[str, dict] = {}
        self._ratio: Dict[str, float] = {}

    def _state(self, session: str) -> dict:
        st = self._sessions.get(session)
        if st is None:
            st = {"calls": {}, "bins": {}, "dirty": True}
            self._sessions[session] = st
        return st

    def begin(self, meter: Meter) -> None:
        if not meter.session:
            return
        st = self._state(meter.session)
        st["calls"][id(meter)] = {"meter": meter, "seen": 0, "since": self._clock()}
        st["dirty"] = True

    def _take(self, st: dict, call: dict, ratio: float, now: float) -> None:
        delta = call["meter"].out_chars - call["seen"]
        if delta <= 0:
            return
        call["seen"] += delta
        start = round(now - now % LIVE_BIN_S, 3)
        st["bins"][start] = st["bins"].get(start, 0.0) + delta * ratio
        st["dirty"] = True

    def end(self, meter: Meter, record: Optional[dict] = None) -> None:
        """The call is over: count what it streamed last, and recalibrate.

        Safe to call twice or for a call that never began (the shim calls it
        from a ``finally``).
        """
        st = self._sessions.get(meter.session or "")
        call = st["calls"].pop(id(meter), None) if st else None
        if call is None:
            return
        ratio = self._ratio.get(meter.session, LIVE_TOKENS_PER_CHAR)
        self._take(st, call, ratio, self._clock())
        st["dirty"] = True
        out = (record or {}).get("output_tokens")
        if isinstance(out, int) and out > 0 and meter.out_chars >= LIVE_CALIBRATE_MIN_CHARS:
            measured = out / meter.out_chars
            self._ratio[meter.session] = round((ratio + measured) / 2, 4)

    def active(self) -> bool:
        return bool(self._sessions)

    def flush(self, now: Optional[float] = None) -> List[Path]:
        """Rewrite the file of every session that changed or has a call in flight.

        A session with a call in flight is rewritten on every flush even with
        nothing new: the ``updated`` stamp is the heartbeat that tells the
        daemon the in-flight count is current (:data:`LIVE_STALE_S`). A session
        with nothing in flight is written once more after its last change; its
        bins are kept here until they leave :data:`LIVE_SPAN_S`, so the next
        call's file still carries the calls before it, and only then is the
        session forgotten.
        """
        now = self._clock() if now is None else now
        written: List[Path] = []
        for session in list(self._sessions):
            st = self._sessions[session]
            ratio = self._ratio.get(session, LIVE_TOKENS_PER_CHAR)
            for call in st["calls"].values():
                self._take(st, call, ratio, now)
            floor = now - LIVE_SPAN_S
            st["bins"] = {t: v for t, v in st["bins"].items() if t >= floor}
            if not st["calls"] and not st["dirty"]:
                # Nothing to write. The bins stay until they age out of the
                # span: the next call's file is written from this state, and
                # forgetting it here would drop the previous call from the
                # chart the moment the next one began (an agent's calls are
                # seconds apart, between its tool runs).
                if not st["bins"]:
                    del self._sessions[session]
                continue
            calls = [c for c in st["calls"].values()]
            doc = {
                "session": session,
                "fingerprint": self.fingerprint,
                "updated": round(now, 3),
                "inflight": len(calls),
                "waiting": sum(1 for c in calls if c["meter"].first_token is None),
                "since": round(min(c["since"] for c in calls), 3) if calls else None,
                "model": next((c["meter"].model for c in calls if c["meter"].model), None),
                "bin_s": LIVE_BIN_S,
                "bins": [[t, round(v, 2)] for t, v in sorted(st["bins"].items())],
                "tokens_per_char": ratio,
            }
            path = live_file(self.fingerprint, session)
            if _write_atomic(path, doc):
                written.append(path)
                st["dirty"] = False
        return written


def _write_atomic(path: Path, doc: dict) -> bool:
    """Replace ``path`` with ``doc`` in one rename; never raises.

    A reader holding the old file open on Windows makes the rename fail; the
    next flush, a quarter of a second later, tries again.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _live_docs(session: str) -> List[dict]:
    out: List[dict] = []
    try:
        paths = list(live_dir().glob(f"{_live_key(session)}@*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("session") == session:
            out.append(doc)
    return out


def live_summary(
    session: str,
    *,
    now: Optional[float] = None,
    span: float = LIVE_SPAN_S,
    docs: Optional[List[dict]] = None,
    peers: Optional[Dict[str, List[dict]]] = None,
    groups: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    """What the session's backend is doing now, merged across its shims.

    ``None`` when the session has no live file at all (never went through a
    shim of this build). Otherwise ``state`` is one of:

    * ``idle`` -- no call in flight. ``rate`` is 0 once the window has passed.
    * ``waiting`` -- a call is in flight and has produced no content yet
      (``since`` says how long it has been waiting).
    * ``streaming`` -- content is arriving; ``rate`` is the estimated output
      tokens per second over the last :data:`LIVE_RATE_WINDOW_S` seconds.

    ``series`` is tokens per second per :data:`LIVE_BIN_S` bin over the last
    ``span`` seconds, oldest first, ending at the bin that holds ``now``.

    ``peers`` is a :func:`live_index` (every session's live files) and
    ``groups`` the :func:`live_groups` already taken over it. With either,
    a session that shares its shim with other sessions active now also gets
    ``parallel`` (:func:`parallel_for`): the shim's total rate, the mean per
    session and how many sessions it is split between (claunch-b28pp).
    """
    if docs is None:
        docs = _live_docs(session) if peers is None else (peers.get(session) or [])
    if not docs:
        return None
    now = time.time() if now is None else now
    inflight = waiting = 0
    since: Optional[float] = None
    model = None
    bins: Dict[float, float] = {}
    for doc in docs:
        fresh = now - float(doc.get("updated") or 0) <= LIVE_STALE_S
        if fresh and doc.get("inflight"):
            inflight += int(doc.get("inflight") or 0)
            waiting += int(doc.get("waiting") or 0)
            if doc.get("since") is not None:
                since = float(doc["since"]) if since is None else min(since, float(doc["since"]))
            model = model or doc.get("model")
        for pair in doc.get("bins") or []:
            try:
                t, v = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            bins[t] = bins.get(t, 0.0) + v
    n = max(1, int(round(span / LIVE_BIN_S)))
    last = now - now % LIVE_BIN_S
    series = []
    for i in range(n):
        t = round(last - (n - 1 - i) * LIVE_BIN_S, 3)
        series.append(round(bins.get(t, 0.0) / LIVE_BIN_S, 1))
    # The bin holding ``now`` is still filling, so the window ends at ``now``
    # and starts LIVE_RATE_WINDOW_S before it; a bin that only partly overlaps
    # the window contributes its overlapping share.
    recent_tokens = _window_tokens(bins.items(), now)
    rate = round(recent_tokens / LIVE_RATE_WINDOW_S, 1)
    if not inflight:
        state = "idle"
    elif waiting >= inflight and not recent_tokens:
        state = "waiting"
    else:
        state = "streaming"
    out = {
        "state": state,
        "inflight": inflight,
        "waiting": waiting,
        "since_s": round(now - since, 1) if since is not None else None,
        "model": model,
        "rate": rate,
        "estimated": True,
        "window_s": LIVE_RATE_WINDOW_S,
        "bin_s": LIVE_BIN_S,
        "series": series,
    }
    if groups is None and peers is not None:
        groups = live_groups(peers, now=now)
    parallel = parallel_for(session, groups) if groups else None
    if parallel:
        out["parallel"] = parallel
    return out


def _window_tokens(bins, now: float, window: float = LIVE_RATE_WINDOW_S) -> float:
    """Estimated tokens whose bin overlaps ``[now - window, now]``.

    A bin holds the output seen from its start to the end of its slot (for the
    bin still filling, to ``now``), so a bin that only partly overlaps the
    window contributes the overlapping share of its count. Summing whole bins
    that merely touch the window reads up to one slot more than the window it
    names -- a steady 200 tok/s stream read at a mid-slot ``now`` came out at
    250 (claunch-v4jgh).
    """
    floor = now - window
    total = 0.0
    for pair in bins:
        try:
            t, v = float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        end = min(t + LIVE_BIN_S, now)
        covered = end - t
        if covered <= 0:
            # A bin stamped at ``now``: what it holds arrived this instant,
            # inside the window. A bin stamped later says nothing about now.
            total += v if t <= now else 0.0
            continue
        overlap = end - max(t, floor)
        if overlap > 0:
            total += v * (overlap / covered)
    return total


def _doc_activity(doc: dict, now: float) -> tuple:
    """``(in flight, estimated tokens/s over the rate window)`` of one live file.

    The same two readings :func:`live_summary` takes, for one file alone: a
    file whose shim stopped writing has nothing in flight, and the rate is
    the window's tokens over :data:`LIVE_RATE_WINDOW_S`.
    """
    fresh = now - float(doc.get("updated") or 0) <= LIVE_STALE_S
    inflight = int(doc.get("inflight") or 0) if fresh else 0
    tokens = _window_tokens(doc.get("bins") or [], now)
    return inflight, tokens / LIVE_RATE_WINDOW_S


def live_groups(live: Dict[str, List[dict]], now: Optional[float] = None) -> Dict[str, dict]:
    """The sessions active now on each shim, keyed by the shim's fingerprint.

    One shim serves every session on the same upstream, so sessions that run
    at once share its throughput. A session counts on a shim while it has a
    call in flight there or produced output inside the rate window -- the
    same window :func:`live_summary` takes its ``rate`` over. Each group is
    ``{"rates": {session: tokens/s}, "calls": {session: in flight},
    "total": tokens/s}``; a shim with no active session has no group. The
    call counts are what tells a shim's total apart from one session's share
    of it: two sessions at 40 and 80 tok/s can be two calls and five
    (claunch-v4jgh).

    One pass over ``live`` (a :func:`live_index`): a session-list build takes
    it once and hands it to every row, rather than walking every file per row.
    """
    now = time.time() if now is None else now
    out: Dict[str, dict] = {}
    for session, docs in live.items():
        for doc in docs:
            fp = doc.get("fingerprint")
            if not fp:
                continue
            inflight, rate = _doc_activity(doc, now)
            if not inflight and rate <= 0:
                continue
            group = out.setdefault(str(fp), {"rates": {}, "calls": {}, "total": 0.0})
            group["rates"][session] = group["rates"].get(session, 0.0) + rate
            group["calls"][session] = group["calls"].get(session, 0) + inflight
            group["total"] += rate
    return out


def _parallel_brief(group: dict, session: str) -> dict:
    """The shared shim's figures, with ``session``'s own share first.

    ``counts`` is each active session's in-flight calls, the reader's own
    first and the rest by rate -- "1+5" says at a glance that the shim's
    total is not split evenly. ``by_session`` is the same list with names and
    rates for the tooltip, and ``calls`` their sum.
    """
    rates = group["rates"]
    calls = group.get("calls") or {}
    n = len(rates)
    total = group["total"]
    order = sorted(rates, key=lambda name: (name != session, -rates[name], name))
    counts = [int(calls.get(name, 0)) for name in order]
    return {
        "sessions": n,
        "total": round(total, 1),
        "mean": round(total / n, 1) if n else 0.0,
        "calls": sum(counts),
        "counts": counts,
        "by_session": [
            {"session": name, "calls": int(calls.get(name, 0)),
             "rate": round(rates[name], 1), "self": name == session}
            for name in order
        ],
        "estimated": True,
        "window_s": LIVE_RATE_WINDOW_S,
    }


def parallel_for(session: str, groups: Dict[str, dict]) -> Optional[dict]:
    """The shim ``session`` shares with other active sessions, or ``None``.

    ``sessions`` is how many sessions are active on that shim now (this one
    included), ``total`` their summed estimated tokens/s and ``mean`` the
    total over ``sessions``. ``counts`` is their in-flight calls,
    ``session``'s own first, and ``by_session`` the same per session with its
    name and rate (claunch-v4jgh). ``None`` unless the session itself is active on
    a shim with at least one other active session: a session running alone
    has nothing to share, and its own rate already is the shim's total.
    When a session is active on more than one shared shim, the one with the
    most sessions (then the higher total) is given.
    """
    best = None
    for group in groups.values():
        if session not in group["rates"] or len(group["rates"]) < 2:
            continue
        key = (len(group["rates"]), group["total"])
        if best is None or key > best[0]:
            best = (key, group)
    return _parallel_brief(best[1], session) if best else None


_live_cache: Dict[str, tuple] = {}


def live_index() -> Dict[str, List[dict]]:
    """Every live file, by session: one listing per session-list build.

    Each file is re-read only when its (mtime, size) moved, the same trade
    :func:`_tail` makes for the record files.
    """
    out: Dict[str, List[dict]] = {}
    try:
        paths = list(live_dir().glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        key = (st.st_mtime_ns, st.st_size)
        with _tail_lock:
            hit = _live_cache.get(str(path))
        if hit and hit[0] == key:
            doc = hit[1]
        else:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            with _tail_lock:
                _live_cache[str(path)] = (key, doc)
        if isinstance(doc, dict) and doc.get("session"):
            out.setdefault(str(doc["session"]), []).append(doc)
    return out


def live_brief(
    session: str,
    live: Dict[str, List[dict]],
    now: Optional[float] = None,
    groups: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    """:func:`live_summary` without the series: what a session row carries.

    ``groups``: :func:`live_groups` over ``live``, taken once by a caller that
    answers many rows; left out, it is taken here.
    """
    if groups is None:
        groups = live_groups(live, now=now)
    summary = live_summary(session, now=now, docs=live.get(session) or [], groups=groups)
    if summary is None:
        return None
    summary.pop("series", None)
    return summary
