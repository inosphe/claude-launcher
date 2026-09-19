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
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import config, store

#: Top-level config key (``metering: false`` disables every shim's records).
CONFIG_KEY = "metering"

#: Process-level override of :data:`CONFIG_KEY` (``0`` off, ``1`` on).
ENV_OVERRIDE = "CLAUNCH_METERING"

#: Request header the daemon adds so the shim can attribute a request.
SESSION_HEADER = "x-claunch-session"

#: Claude Code's hook for extra request headers: newline-separated
#: ``Name: value`` lines.
CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"

#: Non-streaming bodies are buffered up to this many bytes for the usage
#: lookup; a bigger one is forwarded as-is and recorded without token counts.
BUFFER_LIMIT = 8 * 1024 * 1024


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
            return
        if self._chunk_carries_content(doc):
            self.first_token = now
            self.streamed = True
            self._chunk_content = True

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
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
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

#: The rolling window ``session_summary`` computes its median over.
SUMMARY_WINDOW = 10

#: Per-session summary key hung on the session record (see :func:`attach`).
INFO_KEY = "tps"

_tail_cache: Dict[str, tuple] = {}
_tail_lock = threading.Lock()


def _tail_records(path: Path) -> List[dict]:
    """The records at the end of one file, re-read only when the file changed.

    Keyed on (mtime, size): the daemon polls the session list every few
    seconds for every session, and an unchanged file must cost a ``stat``,
    not a parse.
    """
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    with _tail_lock:
        hit = _tail_cache.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
    out: List[dict] = []
    try:
        with open(path, "rb") as fh:
            if st.st_size > TAIL_BYTES:
                fh.seek(st.st_size - TAIL_BYTES)
                fh.readline()  # drop the partial line the seek landed in
            data = fh.read()
    except OSError:
        return out
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    with _tail_lock:
        _tail_cache[str(path)] = (key, out)
    return out


def recent(session: str, *, limit: int = SUMMARY_WINDOW) -> List[dict]:
    """The last ``limit`` records ``session`` made, oldest first."""
    out: List[dict] = []
    try:
        files = sorted(records_dir().glob("*.jsonl"))
    except OSError:
        return out
    for path in files:
        out.extend(r for r in _tail_records(path) if r.get("session") == session)
    out.sort(key=lambda r: str(r.get("ts") or ""))
    return out[-limit:] if limit else out


def session_summary(session: str) -> Optional[dict]:
    """What a session row shows: its latest call and a short rolling median.

    ``None`` when the session has no records at all -- absence, so a reader
    cannot draw "never measured" as a slow session. ``last`` is the newest
    record whether or not it was counted (an error answer still says when
    the session last called out); the medians are over the counted ones in
    the window.

    ``tps`` is the last call's :func:`reported_tps` -- tokens over the whole
    call, one definition for every row.
    """
    recs = recent(session, limit=SUMMARY_WINDOW)
    if not recs:
        return None
    last = recs[-1]
    tps = [v for v in (reported_tps(r) for r in recs) if v is not None]
    ttft = [float(r["ttft_ms"]) for r in recs if r.get("ttft_ms") is not None]
    return {
        "ts": last.get("ts"),
        "model": last.get("model"),
        "status": last.get("status"),
        "counted": bool(last.get("counted")),
        "tps": reported_tps(last),
        "ttft_ms": last.get("ttft_ms"),
        "ttfb_ms": last.get("ttfb_ms"),
        "output_tokens": last.get("output_tokens"),
        "input_tokens": last.get("input_tokens"),
        "cache_read": last.get("cache_read"),
        "window": len(recs),
        "tps_median": round(statistics.median(tps), 2) if tps else None,
        "tps_median_n": len(tps),
        "ttft_ms_median": int(statistics.median(ttft)) if ttft else None,
        "upstream": last.get("upstream"),
    }


def attach(info: dict) -> dict:
    """``info`` (a session record) with a ``tps`` key when there is one to give.

    The same shape of hook as ``daemon/ctxsize.attach``: the key is absent
    rather than null when the session has no records, so the UI draws
    nothing rather than a zero.
    """
    name = info.get("name")
    if name:
        summary = session_summary(str(name))
        if summary:
            info[INFO_KEY] = summary
    return info
