"""How many tokens one managed session's conversation has spent so far.

:mod:`ctxsize` answers "how big is the conversation right now" from the newest
usage record. This module answers the other question the same records carry:
the running total over every model request the conversation made -- fresh
input, input replayed from the prompt cache, input written to it, and output.

The four are kept apart on purpose and never folded into one "input" figure.
A long agent session sends most of its input as cache reads (89.5% of the
prompt tokens across 53 sessions measured for ``claunch-ha2s9``), so a total
that mixes them is dominated by the cheapest component, and two places that
disagree on whether "input" includes the cache read as a 10x difference.
``total`` is the plain sum and is labelled as such.

Each harness writes its own format, so the reading is split in two layers:

* :class:`UsageReader` -- one per harness, fed the transcript's JSON entries
  in file order. It knows which entries are usage records, how to count
  each exactly once, and what it can add beyond the four numbers (Pi's
  recorded cost, Codex's reasoning tokens). :data:`READERS` maps a harness
  name to its reader; :func:`register` is how another harness joins. The
  transcript's location is not the reader's concern -- it comes from
  :func:`ctxsize.transcript_of`, which already knows where each harness keeps
  it.
* :class:`_Follower` -- the file side, shared by all readers. Transcripts are
  append-only JSONL, so a file is read once from the start and afterwards
  only from where the previous read stopped. A file that shrank or was
  replaced starts over with a fresh reader.

Per harness:

* Claude writes one ``assistant`` line per content block of a response, and
  every line of one response repeats the same ``message.id`` with the same
  usage (23,708 repeated lines, 0 with differing usage, over 40 transcripts
  on this machine, 2026-09-27). The reader counts each id once. Subagent
  turns (``isSidechain``, and the ``<conversation>/subagents/agent-*.jsonl``
  files newer Claude Code versions write) are totalled separately as
  ``subagents``: they are spent by this session but are not its own
  conversation.
* Codex keeps a running ``total_token_usage`` on every ``token_count`` event;
  the newest one is the answer. Its ``input_tokens`` already includes the
  cached part, which is subtracted out so the components line up with
  Claude's. A request is counted each time that total changes.
* Pi writes one ``message`` entry per assistant turn, with ``input`` (the
  uncached part), ``cacheRead``, ``cacheWrite``, ``output`` and a ``cost``
  block; the reader sums them, counting each entry ``id`` once.

The scope is the conversation the session is pinned to now
(``conversation_id``). A ``/clear`` moves the session to a new conversation,
and the session record does not keep the previous ids, so the earlier
conversation's spend is not part of this total.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple, Type

from . import ctxsize
from .harness import CLAUDE_HARNESS, PI_HARNESS

CODEX_HARNESS = ctxsize.CODEX_HARNESS

#: The session-info key the reading is published under.
INFO_KEY = "token_usage"

#: The four components, in the order the dashboard lists them.
COMPONENTS = ("input", "cache_read", "cache_write", "output")

#: Largest single read. A long transcript is read in pieces of this size so
#: no read holds the whole file in memory.
CHUNK = 1024 * 1024

#: How much one request reads itself, over every file of every session it
#: asks about. The session list is assembled for every session on each poll,
#: and the first read of a long transcript is slow (a 248 MB Claude
#: transcript took 9.6 s on this machine, 2026-09-27); past this budget the
#: rest is left to a background thread and the reading is published as
#: ``partial`` meanwhile. The budget is per request, not per session: the
#: first list request after a daemon restart asks about every record, and a
#: per-session budget let it read 4 MB x 385 sessions inline -- 10.5 s before
#: it answered (claunch-l6d70 review, s764).
INLINE_BUDGET = 4 * 1024 * 1024

#: A follower not asked about for this long is dropped. It holds the counts
#: of a file and a window of recent ids; a session that is asked about again
#: after that is read again from the start, in the background.
FOLLOWER_TTL = 30 * 60.0

#: How often the idle followers are looked for.
PRUNE_EVERY = 60.0


class Budget:
    """What one request may still read inline, and what it leaves for later.

    Make one per request (:func:`budget`), hand it to every :func:`attach`
    of that request, and call :meth:`release` once the request has asked
    about everything. ``left`` is shared by every file of every session the
    request asks about; ``late`` collects the files it could not finish,
    which :meth:`release` hands to the catch-up thread as one job.

    The hand-over waits for the end of the request on purpose: a catch-up
    thread already parsing while the request still walks its sessions takes
    the GIL off it. Measured over 400 real transcripts (2278 MB): 1.06 s for
    the request with the thread started as it went, 0.06 s with it started
    at the end.
    """

    def __init__(self, left: int) -> None:
        self.left = left
        #: path -> follower, so a request over thousands of subagent files
        #: asks "is this one late" in constant time.
        self.late: Dict[str, "_Follower"] = {}

    def release(self) -> None:
        """Hand what the request left unread to the catch-up thread."""
        late, self.late = list(self.late.values()), {}
        _queue(late)


def budget() -> Budget:
    """A fresh per-request budget of :data:`INLINE_BUDGET` bytes."""
    return Budget(INLINE_BUDGET)


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out > 0 else 0.0


@dataclass
class Tally:
    """Running totals of one stream of model requests."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    requests: int = 0
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    model: Optional[str] = None

    def add(self, *, input: int = 0, cache_read: int = 0, cache_write: int = 0,
            output: int = 0, at: Optional[str] = None,
            model: Optional[str] = None, requests: int = 1) -> None:
        self.input += input
        self.cache_read += cache_read
        self.cache_write += cache_write
        self.output += output
        self.requests += requests
        if at:
            self.first_at = self.first_at or at
            self.last_at = at
        if model:
            self.model = model

    def merge(self, other: "Tally") -> None:
        self.input += other.input
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.output += other.output
        self.requests += other.requests
        if other.first_at and (not self.first_at or other.first_at < self.first_at):
            self.first_at = other.first_at
        if other.last_at and (not self.last_at or other.last_at > self.last_at):
            self.last_at = other.last_at

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output

    def as_dict(self) -> dict:
        out = {key: getattr(self, key) for key in COMPONENTS}
        out["total"] = self.total
        out["requests"] = self.requests
        out["since"] = self.first_at
        out["at"] = self.last_at
        return out


class UsageReader:
    """Accumulates one harness's usage records, fed one entry at a time.

    Subclasses set :attr:`harness` and :attr:`markers` and implement
    :meth:`feed`. :attr:`markers` are byte strings of which every line the
    reader wants contains at least one; other lines are skipped before they
    are parsed, which is what keeps the first read of a transcript full of
    large tool results cheap. Empty means every line is parsed.
    """

    harness: str = ""
    markers: Tuple[bytes, ...] = ()

    def __init__(self) -> None:
        self.tally = Tally()
        #: Usage spent by subagents, when the harness distinguishes them.
        self.side = Tally()

    def feed(self, entry: dict) -> None:
        raise NotImplementedError

    def extra(self) -> dict:
        """Harness-specific keys added to the published reading."""
        return {}

    @classmethod
    def subagent_files(cls, transcript: Path) -> List[Path]:
        """Other files whose usage belongs to this session as subagent spend."""
        return []

    @classmethod
    def for_subagent(cls) -> "UsageReader":
        """The reader for one of :meth:`subagent_files`. Everything it
        counts is taken as subagent spend."""
        return cls()


class _RecentIds:
    """A bounded "seen" set. Repeated lines of one response are written next
    to each other, so a window of recent ids is enough to count each once,
    and it keeps memory flat on a transcript with tens of thousands."""

    def __init__(self, size: int = 1024) -> None:
        self._order: Deque[str] = deque()
        self._set: Set[str] = set()
        self._size = size

    def add(self, key: str) -> bool:
        """``True`` when ``key`` is new."""
        if key in self._set:
            return False
        self._order.append(key)
        self._set.add(key)
        if len(self._order) > self._size:
            self._set.discard(self._order.popleft())
        return True


class ClaudeReader(UsageReader):
    """Claude Code transcripts: assistant entries, one count per message id."""

    harness = CLAUDE_HARNESS
    markers = (b'"usage"',)

    def __init__(self, *, all_side: bool = False) -> None:
        super().__init__()
        self._seen = _RecentIds(256)
        #: A subagent's own file: every entry there is subagent spend.
        self._all_side = all_side

    def feed(self, entry: dict) -> None:
        if entry.get("type") != "assistant":
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return
        key = msg.get("id") or entry.get("requestId")
        if key and not self._seen.add(str(key)):
            return
        fresh = _int(usage.get("input_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))
        cache_write = _int(usage.get("cache_creation_input_tokens"))
        output = _int(usage.get("output_tokens"))
        if not (fresh or cache_read or cache_write or output):
            return
        side = self._all_side or bool(entry.get("isSidechain"))
        (self.side if side else self.tally).add(
            input=fresh, cache_read=cache_read, cache_write=cache_write,
            output=output, at=str(entry.get("timestamp") or "") or None,
            model=str(msg.get("model") or "") or None,
        )

    @classmethod
    def subagent_files(cls, transcript: Path) -> List[Path]:
        folder = transcript.parent / transcript.stem / "subagents"
        try:
            return sorted(folder.glob("agent-*.jsonl"))
        except OSError:
            return []

    @classmethod
    def for_subagent(cls) -> "UsageReader":
        return cls(all_side=True)


class CodexReader(UsageReader):
    """Codex rollouts: the newest cumulative ``total_token_usage`` wins,
    and the newest ``turn_context`` names the model."""

    harness = CODEX_HARNESS
    markers = (b'"token_count"', b'"turn_context"')

    def __init__(self) -> None:
        super().__init__()
        self._last: Optional[Tuple[int, int, int, int]] = None
        self.reasoning = 0

    def feed(self, entry: dict) -> None:
        model = ctxsize.codex_model_of(entry)
        if model:
            self.tally.model = model
            return
        if entry.get("type") != "event_msg":
            return
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        input_all = _int(total.get("input_tokens"))
        cache_read = min(input_all, _int(total.get("cached_input_tokens")))
        cache_write = min(input_all - cache_read,
                          _int(total.get("cache_write_input_tokens")))
        output = _int(total.get("output_tokens"))
        now = (input_all, cache_read, cache_write, output)
        if now == self._last:
            return          # the same total re-announced, not a new request
        at = str(entry.get("timestamp") or "") or None
        requests = self.tally.requests + 1
        first = self.tally.first_at or at
        model = self.tally.model
        self.tally = Tally(
            input=input_all - cache_read - cache_write, cache_read=cache_read,
            cache_write=cache_write, output=output, requests=requests,
            first_at=first, last_at=at or self.tally.last_at, model=model,
        )
        self.reasoning = _int(total.get("reasoning_output_tokens"))
        self._last = now

    def extra(self) -> dict:
        return {"reasoning": self.reasoning} if self.reasoning else {}


class PiReader(UsageReader):
    """Pi session files: assistant ``message`` entries, summed."""

    harness = PI_HARNESS
    markers = (b'"usage"',)

    def __init__(self) -> None:
        super().__init__()
        self._seen = _RecentIds(256)
        self.cost = 0.0

    def feed(self, entry: dict) -> None:
        if entry.get("type") != "message":
            return
        msg = entry.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return
        key = entry.get("id")
        if key and not self._seen.add(str(key)):
            return
        fresh = _int(usage.get("input"))
        cache_read = _int(usage.get("cacheRead"))
        cache_write = _int(usage.get("cacheWrite"))
        output = _int(usage.get("output"))
        if not (fresh or cache_read or cache_write or output):
            return
        self.tally.add(
            input=fresh, cache_read=cache_read, cache_write=cache_write,
            output=output, at=str(entry.get("timestamp") or "") or None,
            model=str(msg.get("model") or "") or None,
        )
        cost = usage.get("cost")
        if isinstance(cost, dict):
            self.cost += _float(cost.get("total"))

    def extra(self) -> dict:
        return {"cost": round(self.cost, 6)} if self.cost else {}


#: harness name -> reader class. A harness absent here has no reading, and
#: the dashboard draws nothing for it rather than a zero.
READERS: Dict[str, Type[UsageReader]] = {
    CLAUDE_HARNESS: ClaudeReader,
    CODEX_HARNESS: CodexReader,
    PI_HARNESS: PiReader,
}


def register(harness: str, reader: Type[UsageReader]) -> None:
    """Make ``harness`` readable. Its transcript must be one
    :func:`ctxsize.transcript_of` can locate."""
    READERS[harness] = reader


class _Follower:
    """One append-only JSONL file, read incrementally into one reader.

    ``lock`` is held for every read, by a poll or by the catch-up thread, so
    the two never interleave on one file.
    """

    def __init__(self, path: Path, make: Callable[[], UsageReader]) -> None:
        self.path = path
        self._make = make
        self.reader = make()
        self.lock = threading.Lock()
        #: Handed to the catch-up thread and not finished yet.
        self.queued = False
        #: Bytes consumed so far, and the file's size at the last look.
        self.offset = 0
        self.size = 0
        #: Bytes the last :meth:`refresh` read.
        self.spent = 0
        #: When a request last asked about this file (monotonic).
        self.used = time.monotonic()
        self._identity: Optional[Tuple[float, int]] = None

    @property
    def behind(self) -> bool:
        return self.queued or self._identity is None

    def refresh(self, budget: Optional[int] = None) -> Optional[bool]:
        """Read what was appended since the last call, at most about
        ``budget`` bytes of it (all of it when ``None``).

        ``True`` when the read reached the end of the file, ``False`` when it
        stopped at the budget, ``None`` when the file cannot be read.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return None
        identity = (stat.st_mtime, stat.st_size)
        self.spent = 0
        if identity == self._identity:
            return True
        self.size = stat.st_size
        if stat.st_size < self.offset:
            # Truncated or replaced: what was counted is no longer the file.
            self.reader = self._make()
            self.offset = 0
        markers = self.reader.markers
        spent = self.spent = 0
        reached_end = False
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                carry = b""
                while budget is None or spent < budget:
                    blob = fh.read(CHUNK if budget is None else min(CHUNK, budget - spent))
                    if not blob:
                        reached_end = True
                        break
                    spent += len(blob)
                    self.spent = spent
                    blob = carry + blob
                    cut = blob.rfind(b"\n")
                    if cut < 0:
                        carry = blob
                        continue
                    carry = blob[cut + 1:]
                    self._feed(blob[:cut].split(b"\n"), markers)
                    # ``blob`` starts at the old offset (the carry is the
                    # unfinished line after it), so this is what was consumed.
                    self.offset += cut + 1
                # A trailing line with no newline yet is still being written;
                # it is read again next time, from the start of that line.
        except OSError:
            return None
        if not reached_end:
            return False
        self._identity = identity
        return True

    def _feed(self, lines: Iterable[bytes], markers: Tuple[bytes, ...]) -> None:
        for raw in lines:
            if markers and not any(m in raw for m in markers):
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self.reader.feed(entry)


#: path -> follower. A daemon restart empties it; the next poll reads the
#: file again from the start.
_followers: Dict[str, _Follower] = {}

#: The one thread that reads a file past :data:`INLINE_BUDGET`, created on
#: first use. One thread, so a daemon restart facing twenty long transcripts
#: reads them one after another instead of twenty at once.
_catchup: Optional[ThreadPoolExecutor] = None
_pending: List[Future] = []
_catchup_lock = threading.Lock()


_last_prune = 0.0


def forget() -> None:
    """Drop every follower. For tests."""
    global _last_prune
    drain()
    _followers.clear()
    _last_prune = 0.0


def prune(now: Optional[float] = None) -> int:
    """Drop the followers nobody asked about for :data:`FOLLOWER_TTL`.
    Returns how many were dropped."""
    now = time.monotonic() if now is None else now
    stale = [key for key, f in list(_followers.items())
             if not f.queued and now - f.used > FOLLOWER_TTL]
    for key in stale:
        _followers.pop(key, None)
    return len(stale)


def _maybe_prune() -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune >= PRUNE_EVERY:
        _last_prune = now
        prune(now)


def drain(timeout: Optional[float] = None) -> None:
    """Wait for the catch-up thread to finish what it was handed. For tests."""
    with _catchup_lock:
        pending = list(_pending)
    for fut in pending:
        fut.result(timeout=timeout)


def _catch_up(late: List[_Follower]) -> None:
    for follower in late:
        try:
            with follower.lock:
                done = follower.refresh(None)
            key = str(follower.path)
            if done is None and _followers.get(key) is follower:
                _followers.pop(key, None)
        finally:
            follower.queued = False


def _queue(late: List[_Follower]) -> None:
    """Hand ``late`` to the catch-up thread as one job.

    One job per session, submitted after the poll's own reading is done: a
    background thread running while the poll still walks the session's
    files (a long Claude session had 607 subagent files) takes the GIL off
    it at every stat, which made a 4 MB budget cost 1.4 s.
    """
    global _catchup
    if not late:
        return
    for follower in late:
        follower.queued = True
    with _catchup_lock:
        if _catchup is None:
            _catchup = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tokenusage")
        _pending[:] = [f for f in _pending if not f.done()]
        _pending.append(_catchup.submit(_catch_up, late))


def _follow(path: Path, make: Callable[[], UsageReader],
            budget: Budget) -> Optional[_Follower]:
    """The follower of ``path``, brought up to date as far as a poll may.

    ``budget`` is what the request may still read inline, shared by every
    file of every session it asks about, and spent here. What is left of a
    long file (the first read after a daemon restart, typically) goes to the
    catch-up thread once the request releases its budget, and until that has
    finished the follower reports ``behind``.
    """
    late = budget.late
    key = str(path)
    follower = _followers.get(key)
    if follower is None:
        follower = _followers.setdefault(key, _Follower(path, make))
    follower.used = time.monotonic()
    if follower.queued or not follower.lock.acquire(blocking=False):
        return follower
    if budget.left <= 0 and follower.behind:
        # Nothing left to spend and never read: no stat, straight to later.
        follower.lock.release()
        late[key] = follower
        return follower
    try:
        done = follower.refresh(max(0, budget.left))
        budget.left -= follower.spent
    finally:
        follower.lock.release()
    if done is None:
        _followers.pop(key, None)
        return None
    if not done:
        late[key] = follower
    return follower


def read_file(path: Path, harness: str = CLAUDE_HARNESS,
              spend: Optional[Budget] = None) -> Optional[dict]:
    """The usage reading of one transcript file (and, for a harness that
    writes them, its subagent files). ``None`` when nothing was spent yet or
    the harness has no reader.

    While part of the files is still being read the reading carries
    ``partial: true`` with the counts so far. No progress figure is given:
    files past the budget are not even looked at by the poll, so their size
    is not known to it.
    """
    cls = READERS.get(harness)
    if cls is None:
        return None
    own = spend is None
    if own:
        spend = budget()
    try:
        return _read(path, cls, harness, spend)
    finally:
        if own:
            spend.release()


def _read(path: Path, cls: Type[UsageReader], harness: str,
          spend: Budget) -> Optional[dict]:
    _maybe_prune()
    main = _follow(path, cls, spend)
    if main is None:
        return None
    followers = [main]
    side = Tally()
    side.merge(main.reader.side)
    for sub in cls.subagent_files(path):
        other = _follow(sub, cls.for_subagent, spend)
        if other is not None:
            followers.append(other)
            side.merge(other.reader.side)
            side.merge(other.reader.tally)
    partial = any(f.behind or str(f.path) in spend.late for f in followers)
    tally = main.reader.tally
    if not tally.requests and not side.requests and not partial:
        return None
    out = {"harness": harness, **tally.as_dict(),
           "model": tally.model, **main.reader.extra()}
    if side.requests:
        out["subagents"] = side.as_dict()
    if partial:
        out["partial"] = True
    return out


def for_session(sdef, spend: Optional[Budget] = None) -> Optional[dict]:
    """This session's usage reading, or ``None`` when it has none to give.
    ``spend`` is the request's :class:`Budget`; without one the call gets a
    budget of its own."""
    harness = str(getattr(sdef, "harness", None) or CLAUDE_HARNESS)
    if harness not in READERS:
        return None
    path = ctxsize.transcript_of(sdef)
    if path is None:
        return None
    return read_file(path, harness, spend)


def attach(info: dict, sdef, spend: Optional[Budget] = None) -> dict:
    """``info`` with a :data:`INFO_KEY` reading when there is one.

    Absent rather than zero when unknown, the same rule as the ``context``
    and ``tps`` keys beside it. A failure to read is absence too: this rides
    the session list, which must not fail because one transcript could not
    be opened.

    A request that attaches many sessions passes one ``spend`` to all of
    them and releases it at the end (see :class:`Budget`).
    """
    try:
        reading = for_session(sdef, spend)
    except Exception:
        reading = None
    if reading:
        info[INFO_KEY] = reading
    return info
