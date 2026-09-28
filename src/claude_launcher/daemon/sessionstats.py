"""One session's statistics over time, and where its input came from.

:mod:`tokenusage` answers "how much has this conversation spent". This module
answers two questions the same transcript carries, for the dashboard's Stats
page:

* **When** -- the four usage components (fresh input, cache read, cache
  write, output) and the request count, in hour, day or week buckets.
* **Who asked** -- every input the conversation received, sorted by origin:

  ``human``    typed by a person at the terminal;
  ``daemon``   a mechanical notice claunch typed in (session reminder, cflow
               nudge, window release reminder, status-check refresh, ...),
               broken down by its header (``kinds``);
  ``mesh``     a message from another agent, relayed by the mesh -- one per
               ``batch`` entry, broken down by sender (``senders``);
  ``opening``  the first claunch delivery of the conversation, the task the
               session was created with;
  ``harness``  what the harness itself adds as a user turn (Claude Code's
               background-task notifications, skill bodies, compaction
               summaries). Tool results are not inputs and are not counted.

Everything claunch types into a terminal goes through ``Session.deliver``,
which puts the ``[claunch delivered <time>]`` stamp on the first line
(``session.delivery_stamp``); the header after it names the kind, and a mesh
block (``mesh.format_delivery``) lists one ``from`` per message. The daemon
keeps no log of these deliveries, so the transcript is the record read here.

Three figures are given per origin, all approximate:

``tokens``    the size of the injected text, estimated from its characters
              (:func:`estimate_tokens`; no tokenizer is run);
``triggered`` the usage of the requests that followed it: each request is
              assigned to the newest input before it, until the next input
              arrives. A notice typed into the middle of a person's task takes
              over the rest of that turn, so this over-counts the notice and
              under-counts the person whenever that happens;
``carry``     the estimated tokens times the number of main-conversation
              requests that followed it until the next compaction, i.e. how
              often that text was sent again as part of the context (mostly as
              cache reads).

Buckets use the daemon's local UTC offset at the time of the request (the
day an operator thinks in, the same rule as ``observer.usage_date``).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

from . import ctxsize, tokenusage
from .harness import CLAUDE_HARNESS, PI_HARNESS

CODEX_HARNESS = ctxsize.CODEX_HARNESS

#: Bucket units and how many of the newest buckets a reading returns.
UNITS: Dict[str, int] = {"hour": 72, "day": 90, "week": 104}

CATEGORIES = ("human", "daemon", "mesh", "opening", "harness")

#: How much one stats request reads itself before leaving the rest to the
#: catch-up thread. Larger than the session list's budget: this page asks
#: about one session, and a partial answer here means another poll.
STATS_BUDGET = 16 * 1024 * 1024

#: The :mod:`tokenusage` follower namespace this module reads under.
NAMESPACE = "stats"

STAMP = "[claunch delivered"
MESH_HEADER = "# claunch mesh: automated message delivery"
_STAMP_RE = re.compile(r"\[claunch delivered [^\]\n]*\]")
_PASTED_RE = re.compile(r"</?pasted_content[^>]*>")
_FROM_RE = re.compile(r"^\s+from:\s*['\"]?([^\s'\"]+)", re.M)

COMPONENTS = tokenusage.COMPONENTS


def estimate_tokens(text: str) -> int:
    """A rough token count: four ASCII characters per token, one token per
    other character (Hangul and other scripts tokenize far denser than
    English). An estimate for comparing origins, not a tokenizer."""
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return int(round(ascii_n / 4 + (len(text) - ascii_n)))


def _epoch(stamp) -> Optional[float]:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Input:
    """One input, or for a mesh block one message of its batch."""

    at: float
    category: str
    kind: str
    sender: Optional[str]
    messages: int
    chars: int
    tokens: int
    #: Arrived while a turn was running (queued into it), or continues the
    #: turn before it (a skill body, a compaction summary): it does not start
    #: a turn of its own, so the requests after it stay with that turn.
    joins: bool = False


def notice_kind(header: str) -> str:
    """The kind of a claunch notice, from the first line after the stamp."""
    head = header.strip()
    if head.startswith("# claunch"):
        topic = head[len("# claunch"):].lstrip(" :")
        topic = re.split(r"\s+(?:--|—)\s*", topic, maxsplit=1)[0]
        topic = re.sub(r"\s+from\s+\S+.*$", "", topic)
        topic = re.sub(r"\s*\([^)]*\)", "", topic).strip(" :")
        return topic[:48] or "notice"
    if head.startswith("cflow:"):
        return "cflow nudge"
    if head.startswith("[claunch status-check refresh]"):
        return "status-check refresh"
    if head.startswith("[Operator]"):
        return "operator"
    return "other"


def _header(segment: str) -> str:
    """The first meaningful line of a delivery, after its stamp."""
    for line in segment.splitlines()[1:]:
        line = line.strip()
        if line and line != "---":
            return line
    return ""


def _mesh_items(segment: str) -> List[Tuple[str, str]]:
    """``(sender, text)`` per message of a mesh delivery's ``batch``."""
    items: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in segment.splitlines(keepends=True):
        if line.startswith("- id:"):
            current = [line]
            items.append(current)
        elif current is not None and (line.startswith("note:") or line.startswith("...")):
            current = None
        elif current is not None:
            current.append(line)
    out = []
    for item in items:
        text = "".join(item)
        found = _FROM_RE.search(text)
        out.append((found.group(1) if found else "?", text))
    return out


def classify(text: str, at: float, first: bool, joins: bool = False) -> List[Input]:
    """The inputs one user turn's text holds.

    ``first`` says whether the conversation has seen a claunch delivery yet;
    the first one is its opening. A turn can hold several deliveries (a
    harness joins messages queued while it was busy), so each stamp starts
    a segment of its own.
    """
    out = _classify(text, at, first)
    for item in out:
        item.joins = joins
    return out


def _classify(text: str, at: float, first: bool) -> List[Input]:
    clean = _PASTED_RE.sub("", text)
    marks = list(_STAMP_RE.finditer(clean))
    if not marks:
        body = clean.strip()
        if not body:
            return []
        kind = "interrupt" if body.startswith("[Request interrupted") else "typed"
        return [Input(at, "human", kind, None, 1, len(body), estimate_tokens(body))]
    out: List[Input] = []
    lead = clean[:marks[0].start()].strip()
    if lead:
        out.append(Input(at, "human", "typed", None, 1, len(lead), estimate_tokens(lead)))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(clean)
        segment = clean[mark.start():end].strip()
        header = _header(segment)
        tokens = estimate_tokens(segment)
        if header.startswith(MESH_HEADER):
            items = _mesh_items(segment)
            used = 0
            for sender, item in items:
                n = estimate_tokens(item)
                used += n
                out.append(Input(at, "mesh", "message", sender, 1, len(item), n))
            rest = max(0, tokens - used)
            # The block's envelope (mesh name, count, protocol note) is paid
            # for too, but belongs to no one sender.
            out.append(Input(at, "mesh", "envelope", None, 0 if items else 1,
                             max(0, len(segment) - sum(len(t) for _, t in items)), rest))
        elif first and not out:
            out.append(Input(at, "opening", "opening", None, 1, len(segment), tokens))
        else:
            out.append(Input(at, "daemon", notice_kind(header), None, 1,
                             len(segment), tokens))
        first = False
    return out


class StatsReader(tokenusage.UsageReader):
    """Timestamped requests and inputs of one transcript.

    A subclass per harness turns entries into :meth:`request` and
    :meth:`user_text` calls; everything after that is shared.
    """

    def __init__(self, *, side: bool = False) -> None:
        super().__init__()
        self._side_only = side
        #: (at, input, cache_read, cache_write, output, subagent)
        self.requests: List[Tuple[float, int, int, int, int, bool]] = []
        self.inputs: List[Input] = []
        #: Compactions: the context was replaced by a summary at these times.
        self.bounds: List[float] = []
        self._opened = False
        self._seen = tokenusage._RecentIds(256)

    def request(self, at: Optional[float], fresh: int, cache_read: int,
                cache_write: int, output: int, side: bool = False) -> None:
        if at is None or not (fresh or cache_read or cache_write or output):
            return
        self.requests.append((at, fresh, cache_read, cache_write, output,
                              side or self._side_only))

    def user_text(self, at: Optional[float], text: str, joins: bool = False) -> None:
        if at is None or self._side_only:
            return
        found = classify(text, at, not self._opened, joins)
        if any(i.category in ("opening", "daemon", "mesh") for i in found):
            self._opened = True
        self.inputs.extend(found)

    def harness_input(self, at: Optional[float], kind: str, text: str,
                      joins: bool = False) -> None:
        if at is None or self._side_only:
            return
        self.inputs.append(Input(at, "harness", kind, None, 1, len(text),
                                 estimate_tokens(text), joins))

    @classmethod
    def for_subagent(cls) -> "StatsReader":
        return cls(side=True)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, dict)
                         and part.get("type") in ("text", "input_text"))
    return ""


class ClaudeStats(StatsReader):
    harness = CLAUDE_HARNESS
    markers = (b'"usage"', b'"user"', b'queued_command', b'compact_boundary')

    def skip(self, raw: bytes) -> bool:
        # A tool result is never an input, and in a long agent session tool
        # results are most of the file's bytes. The quoted name only occurs
        # unescaped as a JSON key or value (quoted inside text it is written
        # \"tool_result\"), and a line with usage is kept whatever it holds.
        return b'"tool_result"' in raw and b'"usage"' not in raw

    def feed(self, entry: dict) -> None:
        kind = entry.get("type")
        at = _epoch(entry.get("timestamp"))
        if kind == "assistant":
            msg = entry.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                return
            key = msg.get("id") or entry.get("requestId")
            if key and not self._seen.add(str(key)):
                return
            usage = msg["usage"]
            self.request(at, tokenusage._int(usage.get("input_tokens")),
                         tokenusage._int(usage.get("cache_read_input_tokens")),
                         tokenusage._int(usage.get("cache_creation_input_tokens")),
                         tokenusage._int(usage.get("output_tokens")),
                         bool(entry.get("isSidechain")))
        elif kind == "user":
            if entry.get("isSidechain"):
                return
            text = _text_of((entry.get("message") or {}).get("content"))
            if not text.strip():
                return
            if entry.get("isCompactSummary"):
                self.harness_input(at, "compaction summary", text, joins=True)
            elif entry.get("isMeta"):
                self.harness_input(at, "skill or meta", text, joins=True)
            elif text.lstrip().startswith("<task-notification>"):
                self.harness_input(at, "task notification", text)
            elif text.lstrip().startswith("<local-command-"):
                self.harness_input(at, "local command", text)
            else:
                self.user_text(at, text)
        elif kind == "attachment":
            att = entry.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                prompt = att.get("prompt")
                # Queued while the model was working: typed into the running
                # turn, which it does not start.
                self.user_text(at, prompt if isinstance(prompt, str) else _text_of(prompt),
                               joins=True)
        elif kind == "system" and entry.get("subtype") == "compact_boundary":
            if at is not None:
                self.bounds.append(at)

    @classmethod
    def subagent_files(cls, transcript: Path) -> List[Path]:
        return tokenusage.ClaudeReader.subagent_files(transcript)


class CodexStats(StatsReader):
    harness = CODEX_HARNESS
    markers = (b'"token_count"', b'"user"', b'"compacted"')

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._last_total: Optional[Tuple[int, ...]] = None

    def feed(self, entry: dict) -> None:
        at = _epoch(entry.get("timestamp"))
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        if entry.get("type") == "compacted":
            if at is not None:
                self.bounds.append(at)
            return
        if entry.get("type") == "response_item" and payload.get("type") == "message" \
                and payload.get("role") == "user":
            text = _text_of(payload.get("content"))
            stripped = text.lstrip()
            if not stripped:
                return
            if stripped.startswith("<environment_context>") \
                    or stripped.startswith("<user_instructions>") \
                    or stripped.startswith("# AGENTS.md"):
                self.harness_input(at, "environment", text)
            else:
                self.user_text(at, text)
            return
        if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        total = info.get("total_token_usage")
        last = info.get("last_token_usage")
        if not isinstance(total, dict) or not isinstance(last, dict):
            return
        key = tuple(tokenusage._int(total.get(k)) for k in
                    ("input_tokens", "cached_input_tokens", "output_tokens"))
        if key == self._last_total:
            return      # the same total re-announced, not a new request
        self._last_total = key
        input_all = tokenusage._int(last.get("input_tokens"))
        cached = min(input_all, tokenusage._int(last.get("cached_input_tokens")))
        self.request(at, input_all - cached, cached, 0,
                     tokenusage._int(last.get("output_tokens")))


class PiStats(StatsReader):
    harness = PI_HARNESS
    markers = (b'"usage"', b'"user"')

    def feed(self, entry: dict) -> None:
        if entry.get("type") != "message":
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return
        at = _epoch(entry.get("timestamp"))
        if msg.get("role") == "user":
            self.user_text(at, _text_of(msg.get("content")))
            return
        usage = msg.get("usage")
        if msg.get("role") != "assistant" or not isinstance(usage, dict):
            return
        key = entry.get("id")
        if key and not self._seen.add(str(key)):
            return
        self.request(at, tokenusage._int(usage.get("input")),
                     tokenusage._int(usage.get("cacheRead")),
                     tokenusage._int(usage.get("cacheWrite")),
                     tokenusage._int(usage.get("output")))


#: harness name -> stats reader. The same shape as :data:`tokenusage.READERS`.
READERS: Dict[str, Type[StatsReader]] = {
    CLAUDE_HARNESS: ClaudeStats,
    CODEX_HARNESS: CodexStats,
    PI_HARNESS: PiStats,
}


def register(harness: str, reader: Type[StatsReader]) -> None:
    READERS[harness] = reader


# -- aggregation ------------------------------------------------------------

def local_zone() -> tzinfo:
    """The daemon's UTC offset now, as a fixed zone."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def bucket_start(at: float, unit: str, zone: tzinfo) -> datetime:
    moment = datetime.fromtimestamp(at, zone)
    if unit == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        return day
    return day - timedelta(days=day.weekday())      # weeks start on Monday


def _step(unit: str) -> timedelta:
    return {"hour": timedelta(hours=1), "day": timedelta(days=1),
            "week": timedelta(weeks=1)}[unit]


def _usage(values=(0, 0, 0, 0)) -> dict:
    out = dict(zip(COMPONENTS, values))
    out["total"] = sum(values)
    return out


def _add(row: dict, fresh: int, cache_read: int, cache_write: int, output: int) -> None:
    row["input"] += fresh
    row["cache_read"] += cache_read
    row["cache_write"] += cache_write
    row["output"] += output
    row["total"] += fresh + cache_read + cache_write + output


def _source_row() -> dict:
    return {"messages": 0, "chars": 0, "tokens": 0, "carry": 0, "requests": 0,
            "triggered": _usage()}


def summarize(main: StatsReader, sides: List[StatsReader], unit: str,
              zone: Optional[tzinfo] = None) -> dict:
    """The reading for one transcript (and its subagent files)."""
    zone = zone or local_zone()
    requests = sorted(main.requests + [r for s in sides for r in s.requests])
    inputs = sorted(main.inputs, key=lambda i: i.at)
    bounds = sorted(main.bounds)

    totals = _usage()
    subagent_requests = 0
    for at, fresh, cr, cw, out, side in requests:
        _add(totals, fresh, cr, cw, out)
        subagent_requests += side
    totals["requests"] = len(requests)
    totals["subagent_requests"] = subagent_requests

    # Buckets: a contiguous run ending at the newest activity, at most
    # UNITS[unit] long and starting no earlier than the oldest.
    stamps = [r[0] for r in requests] + [i.at for i in inputs]
    buckets: List[dict] = []
    if stamps:
        step = _step(unit)
        last = bucket_start(max(stamps), unit, zone)
        first = max(bucket_start(min(stamps), unit, zone),
                    last - step * (UNITS[unit] - 1))
        index: Dict[datetime, dict] = {}
        moment = first
        while moment <= last:
            row = {"start": moment.isoformat(), **_usage(), "requests": 0,
                   "inputs": {c: 0 for c in CATEGORIES}}
            index[moment] = row
            buckets.append(row)
            moment += step
        for at, fresh, cr, cw, out, _side in requests:
            row = index.get(bucket_start(at, unit, zone))
            if row is not None:
                _add(row, fresh, cr, cw, out)
                row["requests"] += 1
        for item in inputs:
            row = index.get(bucket_start(item.at, unit, zone))
            if row is not None:
                row["inputs"][item.category] += item.messages

    # Carry: main-conversation requests after an input and before the next
    # compaction.
    main_times = [r[0] for r in requests if not r[5]]

    def carried(at: float) -> int:
        start = bisect_right(main_times, at)
        nxt = bisect_right(bounds, at)
        end = bisect_left(main_times, bounds[nxt]) if nxt < len(bounds) else len(main_times)
        return max(0, end - start)

    categories: Dict[str, dict] = {c: _source_row() for c in CATEGORIES}
    kinds: Dict[Tuple[str, str], dict] = {}
    senders: Dict[str, dict] = {}
    unattributed = _source_row()
    for item in inputs:
        carry = item.tokens * carried(item.at)
        rows = [categories[item.category],
                kinds.setdefault((item.category, item.kind), _source_row())]
        if item.sender:
            rows.append(senders.setdefault(item.sender, _source_row()))
        for row in rows:
            row["messages"] += item.messages
            row["chars"] += item.chars
            row["tokens"] += item.tokens
            row["carry"] += carry

    # Triggered: each request belongs to the newest turn-starting input
    # before it; the rows credited are the input's category and kind, and its
    # sender for a mesh message. Several inputs of one turn credit the first.
    turns: List[Tuple[float, List[dict]]] = []
    for item in inputs:
        if item.joins or (item.messages == 0 and item.category == "mesh"):
            continue        # the envelope rides with its messages
        rows = [categories[item.category], kinds[(item.category, item.kind)]]
        if item.sender:
            rows.append(senders[item.sender])
        if turns and turns[-1][0] == item.at:
            # Several inputs in one turn: credit the turn once, to the first.
            continue
        turns.append((item.at, rows))
    turn_times = [t[0] for t in turns]
    for at, fresh, cr, cw, out, _side in requests:
        pos = bisect_right(turn_times, at) - 1
        rows = turns[pos][1] if pos >= 0 else [unattributed]
        for row in rows:
            row["requests"] += 1
            _add(row["triggered"], fresh, cr, cw, out)

    def share(rows: Dict, key) -> None:
        msg_total = sum(r["messages"] for r in rows.values()) or 0
        tok_total = sum(r["tokens"] for r in rows.values()) or 0
        trig_total = sum(r["triggered"]["total"] for r in rows.values()) or 0
        carry_total = sum(r["carry"] for r in rows.values()) or 0
        for r in rows.values():
            r["share"] = {
                "messages": r["messages"] / msg_total if msg_total else 0.0,
                "tokens": r["tokens"] / tok_total if tok_total else 0.0,
                "triggered": r["triggered"]["total"] / trig_total if trig_total else 0.0,
                "carry": r["carry"] / carry_total if carry_total else 0.0,
            }

    share(categories, "category")
    sources = []
    for name in CATEGORIES:
        row = categories[name]
        row = {"category": name, **row, "kinds": sorted(
            ({"kind": k, **v} for (c, k), v in kinds.items() if c == name),
            key=lambda r: (-r["messages"], r["kind"]))}
        sources.append(row)
    sender_rows = sorted(({"sender": k, **v} for k, v in senders.items()),
                         key=lambda r: (-r["messages"], r["sender"]))

    first_at = min(stamps) if stamps else None
    last_at = max(stamps) if stamps else None
    return {
        "unit": unit,
        "utc_offset": datetime.now(zone).strftime("%z"),
        "since": datetime.fromtimestamp(first_at, timezone.utc).isoformat() if first_at else None,
        "at": datetime.fromtimestamp(last_at, timezone.utc).isoformat() if last_at else None,
        "totals": totals,
        "buckets": buckets,
        "sources": sources,
        "senders": sender_rows,
        "unattributed": {"requests": unattributed["requests"],
                         "triggered": unattributed["triggered"]},
        "compactions": len(bounds),
    }


# -- reading ----------------------------------------------------------------

def read_file(path: Path, harness: str, unit: str = "day",
              zone: Optional[tzinfo] = None,
              spend: Optional[tokenusage.Budget] = None) -> Optional[dict]:
    """The statistics of one transcript, or ``None`` for a harness this
    module has no reader for. ``partial`` is set while part of the files is
    still being read in the background."""
    cls = READERS.get(harness)
    if cls is None:
        return None
    if unit not in UNITS:
        raise ValueError(f"unit must be one of {', '.join(UNITS)}")
    own = spend is None
    if own:
        spend = tokenusage.Budget(STATS_BUDGET)
    try:
        tokenusage._maybe_prune()
        main = tokenusage._follow(path, cls, spend, NAMESPACE)
        if main is None:
            return None
        followers = [main]
        for sub in cls.subagent_files(path):
            other = tokenusage._follow(sub, cls.for_subagent, spend, NAMESPACE)
            if other is not None:
                followers.append(other)
        partial = any(f.behind or f.key in spend.late for f in followers)
        out = {"harness": harness,
               **summarize(main.reader, [f.reader for f in followers[1:]], unit, zone)}
        if partial:
            out["partial"] = True
        return out
    finally:
        if own:
            spend.release()


def locate(sdef) -> Tuple[dict, Optional[Path]]:
    """``(head, path)`` for one session: ``head`` names it and, when there is
    nothing to read, already carries ``available: false`` and the ``reason``
    (``path`` is then ``None``). Cheap -- the path lookup is cached -- so the
    daemon does it and hands only the path to the stats worker."""
    name = str(getattr(sdef, "name", "") or "")
    harness = str(getattr(sdef, "harness", None) or CLAUDE_HARNESS)
    head = {"session": name, "harness": harness}
    if harness not in READERS:
        return {**head, "available": False,
                "reason": f"no statistics reader for the {harness} harness"}, None
    path = ctxsize.transcript_of(sdef)
    if path is None:
        return {**head, "available": False,
                "reason": "no transcript found for this session's conversation"}, None
    return head, path


def read_located(head: dict, path: Optional[Path], unit: str = "day",
                 zone: Optional[tzinfo] = None) -> dict:
    """The Stats page's answer for a session :func:`locate` has found."""
    if path is None:
        return dict(head)
    reading = read_file(Path(path), head["harness"], unit, zone)
    if reading is None:
        return {**head, "available": False,
                "reason": "the transcript could not be read"}
    return {"session": head["session"], "available": True, **reading}


def for_session(sdef, unit: str = "day", zone: Optional[tzinfo] = None) -> dict:
    """The Stats page's answer for one session. ``available`` is false, with
    a ``reason``, when there is nothing to read."""
    head, path = locate(sdef)
    return read_located(head, path, unit, zone)
