"""Several sessions' usage over one window, for the Stats page's overview.

:mod:`sessionstats` answers what **one** session spent, read from its
transcript. The overview asks a different question -- which sessions spent
what over today, the last seven days or the last thirty -- and reading every
transcript on a poll is not affordable: the machine this was written on runs
1,601 sessions with about 11 GB of transcripts, and a month window would have
to read most of them.

It reads :mod:`metering`'s per-request ledger instead. That ledger already
holds one line per API request the routing shim forwarded: when it ran, which
session sent it, which model answered, the provider's own token counts, and
the upstream it went to. A month of this fleet's traffic is about 24 MB, and
the reading here is incremental -- only the bytes appended since the previous
poll are parsed, into per-session per-day sums.

Two consequences, and the page states both rather than papering over them:

* Only providers that go through the routing shim write ledger lines. A
  session on an OAuth route (Anthropic's subscription, a Codex login) has
  none. Those rows carry ``source: "none"`` and **no** figure -- never a zero,
  which would read as "spent nothing" -- and their detail is read from the
  transcript on demand by the existing ``/api/sessions/<name>/stats`` route.
* ``total`` is fresh input + cache read + cache write + output. The four stay
  apart in the payload because they cost differently, and the cache share is
  reported beside them: one page calling two different quantities "input" is
  the defect claunch-ha2s9 recorded.

Provider names are not in the ledger. They are resolved from the upstream URL
each record carries, against the provider registry's declared backends
(:func:`provider_labels`); two providers that share one upstream (``kimi`` and
``kimi-small`` here) group under the shortest name that covers both. A
session's usage in the window is kept **per provider**, so a session that
moved -- an operator's reborrow onto another profile's credentials, or a
profile whose backend was repointed -- shows both providers and the switch.

The session record keeps only the *current* borrow. Nothing records a borrow's
history, so :func:`overview` reports the switches it can prove from the ledger
(the upstream/model that answered before and after), and the page says that is
what the mark means. ``borrow`` beside it is the value in force now.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import metering, providers, store
from . import sessionstats

#: The windows the page offers, and how many local days each one spans
#: (today is the current local day alone, so the page's bars line up with the
#: day boundaries a person thinks in).
WINDOWS: Dict[str, int] = {"today": 1, "week": 7, "month": 30}

#: Days of per-session sums kept in memory. One more than the widest window,
#: so a "month" request is answered from state already accumulated.
RETENTION_DAYS = 31

#: How many switches one session may report. A session that alternates
#: models on every call would otherwise grow without bound; the newest are
#: the ones a reader came for.
MAX_SWITCHES = 40

#: How many *changes* of one provider's answer a session keeps, plus the
#: provider's last call. The ledger's files are per fingerprint -- that is,
#: per provider -- so the order records were *read* in is not the order they
#: happened in: a session whose provider changed mid-window has its history
#: split across files, and the change has to be derived from each record's
#: ``ts``. What the page marks is where the answer changed, so the marks are
#: kept, not every call: a hundred calls on one model followed by a hundred on
#: another is one mark, and stays one however long those runs get. The last
#: call is kept beside them, because the same provider coming back after
#: another one answered is a change too, and only a later timestamp can say
#: it did.
TAIL_MAX = 64

UNIT = "day"

#: The bin the ledger accumulates for the fleet's time series, in seconds.
#: Every kept record lands in one; a five-minute bin is the finest the
#: Chart page draws, and a coarser request bin is a whole number of these.
SERIES_BIN_S = 300

#: The bucket widths the Chart page's series may use, narrowest first. The
#: page asks for a window; the width is chosen here so that one screen is at
#: most :data:`SERIES_MAX_POINTS` points whatever the window or the ledger's
#: depth.
SERIES_BUCKETS = (300, 900, 1800, 3600, 10800, 21600, 43200, 86400)

#: The most points one series answer carries: one 5-minute point across the
#: 24-hour window (288 of them, plus the one the alignment to the bucket grid
#: can add -- hence the slack over 288). The page draws every point it is
#: given, and that many still read at one screen's width.
SERIES_MAX_POINTS = 290

#: The Chart page's range keys and the seconds each one spans; ``all`` is
#: however far back the ledger goes. The page has its own copy of these
#: labels, and the two are checked against each other by a test.
SERIES_WINDOWS: Dict[str, Optional[int]] = {
    "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None,
}

#: The two ways a session can be over. Each has its own box on the page: a
#: killed session was ended early, an archived one was put away after its
#: work, and a reader often wants one without the other.
ENDED_CATEGORIES = ("killed", "archived")

#: Providers whose backend is the vendor's own login rather than a declared
#: base URL. They never reach the routing shim, so the ledger has nothing for
#: them; their figures come from the session's own transcript instead
#: (``source: "transcript"``), or not at all (``source: "none"``).
OAUTH_PROVIDERS = ("default", "claude", "codex", "devin")

#: How many sessions one overview call may read from their transcripts. The
#: reading is incremental and kept for :data:`TRANSCRIPT_TTL`, but the first
#: pass over a busy machine's transcripts is the cost claunch-3r94d took off
#: the event loop; the cap keeps one request from paying it for every session
#: at once.
MAX_TRANSCRIPTS = 200

#: How long one session's day-bucketed reading is reused. The reading itself
#: advances incrementally (``tokenusage`` keeps the file offset); this only
#: saves re-walking the file list on every poll while the page is open.
TRANSCRIPT_TTL = 60.0


def _number(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def record_components(record: dict) -> Optional[Tuple[int, int, int, int]]:
    """``(input, cache_read, cache_write, output)`` for one ledger record.

    The two wire formats disagree about what ``input_tokens`` means
    (:func:`metering.prompt_tokens`), and folding them without the same care
    would count a Pi/OpenAI prompt's cached part twice. Anthropic Messages
    counts the three input-side quantities apart; OpenAI Chat Completions
    counts the prompt whole, cache inside it, so the cached part is taken out
    to keep the four components comparable.

    ``None`` when the record carries no token counts at all: an error answer,
    or a body the shim could not read. It is not a zero-token request.
    """
    if record.get("counted") is False:
        return None
    raw = record.get("input_tokens")
    if raw is None:
        return None
    fresh = _number(raw)
    cached = _number(record.get("cache_read"))
    written = _number(record.get("cache_write"))
    output = _number(record.get("output_tokens"))
    path = str(record.get("path") or "")
    if path.endswith("/chat/completions"):
        fresh = max(0, fresh - cached)
    return fresh, cached, written, output


def _epoch(stamp) -> Optional[float]:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _iso(at: float, zone: tzinfo) -> str:
    return datetime.fromtimestamp(at, zone).isoformat(timespec="seconds")


def local_zone() -> tzinfo:
    """The daemon's local zone, as :mod:`sessionstats` buckets by."""
    return sessionstats.local_zone()


def day_key(at: float, zone: tzinfo) -> str:
    return datetime.fromtimestamp(at, zone).strftime("%Y-%m-%d")


def window_bounds(window: str, now: Optional[float] = None,
                  zone: Optional[tzinfo] = None) -> Tuple[float, float]:
    """``(start, end)`` as epochs: local midnight ``days - 1`` ago, and now.

    Whole local days, so "today" starts at midnight and "week" covers seven
    local dates including today. The previous reading of "last seven days"
    as a rolling 7 x 24 hours cut most of a day off the chart and put a
    request just after midnight in a bucket no date label named.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(WINDOWS)}")
    days = WINDOWS[window]
    zone = zone or local_zone()
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, zone).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    return start.timestamp(), now


def window_days(window: str, now: Optional[float] = None,
                zone: Optional[tzinfo] = None) -> List[str]:
    """The local date labels the window covers, oldest first."""
    start, end = window_bounds(window, now, zone)
    zone = zone or local_zone()
    first = datetime.fromtimestamp(start, zone).date()
    last = datetime.fromtimestamp(end, zone).date()
    out = []
    while first <= last:
        out.append(first.isoformat())
        first += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def _upstream_key(url: str) -> str:
    """An upstream's identity: scheme+host+path, without a trailing ``/v1``.

    The ledger carries the same backend under both spellings --
    ``https://api.fireworks.ai/inference`` and ``.../inference/v1`` are one
    upstream seen through two harnesses' base URLs -- and a mapping keyed on
    the raw string would file them as two providers.
    """
    text = str(url or "").strip().lower().rstrip("/")
    if text.endswith("/v1"):
        text = text[:-3].rstrip("/")
    return text


def _cover(names: Iterable[str]) -> str:
    """One label for the providers behind a shared upstream.

    ``kimi`` and ``kimi-small`` declare the same backend; the shortest name
    that prefixes the rest names the backend both reach ("kimi"). Names that
    do not nest are joined, so nothing is silently dropped.
    """
    ordered = sorted(set(n for n in names if n))
    if not ordered:
        return ""
    first = ordered[0]
    if all(n.startswith(first) for n in ordered):
        return first
    return "/".join(ordered)


def provider_labels(doc: Optional[dict] = None) -> Dict[str, str]:
    """Upstream identity -> provider label, from the live provider registry.

    Providers without a declared base URL (the OAuth routes: ``default``,
    ``claude``, ``codex``, ``devin``) never write ledger lines, so they are
    not in the map; a record from such a route cannot exist.
    """
    doc = store.load() if doc is None else doc
    found: Dict[str, set] = {}
    for name, env in providers.registry(doc).items():
        if name in OAUTH_PROVIDERS or not name:
            continue
        url = str((env or {}).get("ANTHROPIC_BASE_URL") or "").strip()
        if url:
            found.setdefault(_upstream_key(url), set()).add(name)
    return {key: _cover(names) for key, names in found.items()}


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "unknown"
    except ValueError:
        return "unknown"


def provider_of(record: dict, labels: Dict[str, str]) -> str:
    """The provider label a ledger record's upstream resolves to.

    An upstream the registry does not declare (a loopback shim started by a
    test, a backend since removed from the config) is named by its host
    rather than dropped or guessed into a neighbour.
    """
    upstream = str(record.get("upstream") or "").strip()
    if not upstream:
        return "unknown"
    label = labels.get(_upstream_key(upstream))
    return label or _host(upstream)


def declared_provider(profile: str, borrow: str = "", doc: Optional[dict] = None) -> str:
    """The provider a session's definition resolves to, borrow included.

    Used only for rows the ledger cannot speak for (``source: "none"``): for
    a metered row the upstream that actually answered is the better fact.
    """
    try:
        from .. import profile as profile_mod  # local: keep import cost off the module

        own = profile_mod.require_selector(str(profile or ""))
        auth = profile_mod.require(str(borrow)) if borrow else own
        return providers.resolve_name(auth, doc)
    except Exception:  # noqa: BLE001 - a deleted profile is not this page's failure
        return ""


# --------------------------------------------------------------------------- #
# the ledger, read incrementally
# --------------------------------------------------------------------------- #
class Ledger:
    """Per-session, per-day, per-provider sums from the metering ledger.

    The first :meth:`refresh` parses every line the daemon's metering
    directory holds; every later one parses only what was appended since, so
    the page can poll this without re-reading the ledger. A file that shrank
    or was replaced (``claunch tps clear``, a new fingerprint writing over an
    old name) makes the state unusable -- the sums would keep counting the
    removed lines -- so it is rebuilt from zero rather than patched.
    """

    def __init__(self, directory: Optional[Path] = None,
                 labels: Optional[Dict[str, str]] = None,
                 zone: Optional[tzinfo] = None):
        self.directory = Path(directory) if directory is not None else metering.records_dir()
        self.labels = dict(labels or {})
        self.zone = zone or local_zone()
        # file name -> (inode, bytes consumed, incomplete trailing line)
        self._files: Dict[str, Tuple[int, int, str]] = {}
        # session -> {"days": {day: {provider: [in, read, write, out, requests]}},
        #             "first": epoch, "last": epoch, "model": str,
        #             "last_key": (provider, model), "switches": [...]}
        self.sessions: Dict[str, dict] = {}
        self.rebuilds = 0
        self._pruned_at = 0.0
        # Two polls can arrive together (the page polls every 30 s, and a
        # second dashboard tab doubles that): overlapping reads would both
        # consume the same appended bytes and count them once each.
        self._lock = threading.Lock()

    # -- reading ----------------------------------------------------------
    def refresh(self, now: Optional[float] = None) -> None:
        with self._lock:
            self._refresh(now)

    def _refresh(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        # A rebuild clears every file's offset, not just the one that was
        # truncated: the files this loop had already passed would otherwise
        # stay missing until the next poll -- one answer short, for no reason
        # a reader could see. So a rebuild re-runs the scan from the start.
        # Three passes, not two: a rebuild during a rebuild (two files
        # rewritten between polls) would otherwise end with an empty state
        # and one blank table for a reader to wonder about.
        for _ in range(3):
            if not self._scan():
                break
        self._prune(now)

    def _scan(self) -> bool:
        """Read every unread byte; True when a rebuild forced the scan to stop."""
        try:
            paths = sorted(self.directory.glob("*.jsonl"))
        except OSError:
            paths = []
        # A deleted record file (`claunch tps clear`) and one that shrank are
        # the same fact: the sums this reader holds no longer describe the
        # directory. A file that is gone cannot be found by the per-file check
        # below, so the name set is compared first.
        if set(self._files) - {path.name for path in paths}:
            self.reset()
            return True
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            before = self._files.get(path.name)
            if before is not None and (stat.st_ino != before[0]
                                       or stat.st_size < before[1]):
                self.reset()
                return True
            offset = before[1] if before else 0
            tail = before[2] if before else ""
            if stat.st_size == offset and not tail:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
            except OSError:
                continue
            text = tail + chunk.decode("utf-8", "replace")
            lines = text.split("\n")
            tail = lines.pop()          # the last line may still be growing
            for line in lines:
                self._add(line)
            self._files[path.name] = (stat.st_ino, offset + len(chunk), tail)
        return False

    def reset(self) -> None:
        self._files.clear()
        self.sessions.clear()
        self.rebuilds += 1
        self._pruned_at = 0.0

    def _add(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        name = str(record.get("session") or "").strip()
        if not name:
            return
        parts = record_components(record)
        if parts is None:
            return
        at = _epoch(record.get("ts"))
        if at is None:
            return
        zone = self.zone
        day = day_key(at, zone)
        provider = provider_of(record, self.labels)
        model = str(record.get("model") or "")
        row = self.sessions.setdefault(name, {
            "days": {}, "first": at, "last": at, "tail": {},
            # SERIES_BIN_S bin start (epoch s) -> [fresh, read, write, out,
            # requests]. The per-day map cannot answer finer than a day, and
            # the Chart page's series needs a shape over time (claunch-x15q5).
            "bins": {},
        })
        bucket = row["days"].setdefault(day, {}).setdefault(provider, {
            "input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
            "requests": 0, "first": at, "last": at,
        })
        for key, value in zip(("input", "cache_read", "cache_write", "output"), parts):
            bucket[key] += value
        bucket["requests"] += 1
        bucket["first"] = min(bucket["first"], at)
        bucket["last"] = max(bucket["last"], at)
        row["first"] = min(row["first"], at)
        row["last"] = max(row["last"], at)
        slot = row["bins"].setdefault(int(at // SERIES_BIN_S) * SERIES_BIN_S,
                                      [0, 0, 0, 0, 0])
        for i, value in enumerate(parts):
            slot[i] += value
        slot[4] += 1
        # One provider's changes are one deque: the order inside it is the
        # order they happened in (a file is append-only), while the order
        # *between* deques is not known until the times are sorted. Only a
        # change is appended -- a run of calls on one model is one mark --
        # and the last call is kept beside the deque so a provider that
        # answers again after another one did is seen to have done so.
        tail = row["tail"].setdefault(provider, {"changes": deque(maxlen=TAIL_MAX),
                                                 "model": None, "last": (at, model)})
        if tail["model"] != model:
            tail["changes"].append((at, model))
            tail["model"] = model
        tail["last"] = (at, model)

    def _prune(self, now: float) -> None:
        if now - self._pruned_at < 3600 and self._pruned_at:
            return
        self._pruned_at = now
        oldest = day_key(now - RETENTION_DAYS * 86400, self.zone)
        cutoff = (datetime.strptime(oldest, "%Y-%m-%d")
                  .replace(tzinfo=self.zone).timestamp())
        for row in self.sessions.values():
            days = row["days"]
            for day in [d for d in days if d < oldest]:
                del days[day]
            for b in [b for b in (row.get("bins") or {}) if b < cutoff]:
                del row["bins"][b]
            for provider, tail in list((row.get("tail") or {}).items()):
                kept = [e for e in (tail.get("changes") or []) if e[0] >= cutoff]
                last = tail.get("last")
                if kept:
                    tail["changes"] = deque(kept, maxlen=TAIL_MAX)
                elif last and last[0] >= cutoff:
                    tail["changes"] = deque([last], maxlen=TAIL_MAX)
                else:
                    del row["tail"][provider]
                    continue
                if last and last[0] < cutoff:
                    tail["last"] = tail["changes"][-1] if tail["changes"] else None

    def earliest_day(self) -> Optional[str]:
        """The oldest local date any kept record falls on, for the page's note.

        The ledger only exists since metering was switched on, so a "month"
        window can be narrower than a month of history. Saying when the
        record starts is how a reader tells "this fleet spent nothing last
        week" from "nobody was measuring last week".
        """
        days = [day for row in self.sessions.values() for day in row["days"]]
        return min(days) if days else None

    # -- reading the sequence ---------------------------------------------
    def _switch_series(self, row: dict, since_epoch: float):
        """``(switches, last_model)`` for one session, ordered by record time.

        The ledger's files are per fingerprint, so a provider change splits a
        session's records across files and the read order is not the order
        they happened in. Each provider's tail is already in time order; the
        merge of the tails, sorted by ``ts``, is the sequence a reader wants.
        A provider that answered, then lost the session, then answered again
        shows as two switches because its calls sit on both sides of the
        other provider's in that merged order.
        """
        events = []
        seen = set()
        for provider, tail in (row.get("tail") or {}).items():
            points = list(tail.get("changes") or [])
            # The provider's newest call is a change point too when it is not
            # already the last mark: without it, "answered, handed over, came
            # back" collapses to one mark at the hand-over.
            if tail.get("last"):
                points.append(tail["last"])
            for at, model in points:
                if at < since_epoch or (at, provider, model) in seen:
                    continue
                seen.add((at, provider, model))
                events.append((at, provider, model))
        events.sort(key=lambda e: (e[0], e[1]))
        switches = []
        current = None
        model = ""
        for at, provider, called in events:
            key = (provider, called)
            if current is not None and key != current:
                switches.append({
                    "at": _iso(at, self.zone),
                    "at_epoch": at,
                    "kind": "provider" if current[0] != provider else "model",
                    "from": current[0], "to": provider,
                    "from_model": current[1], "to_model": called,
                })
            current = key
            model = called or model
        if len(switches) > MAX_SWITCHES:
            switches = switches[-MAX_SWITCHES:]
        return switches, model

    # -- reading the sums -------------------------------------------------
    def window(self, since_day: str, since_epoch: float) -> Dict[str, dict]:
        """Per-session sums whose days are ``since_day`` or later.

        ``providers`` is ordered by tokens, largest first, so the row's first
        entry is the provider it actually ran on. ``switches`` are cut to the
        window: a session that changed provider last month does not carry a
        mark into this week's table.
        """
        out: Dict[str, dict] = {}
        for name, row in self.sessions.items():
            days = {d: v for d, v in row["days"].items() if d >= since_day}
            if not days:
                continue
            per_provider: Dict[str, dict] = {}
            day_totals: Dict[str, int] = {}
            first = last = None
            for day, providers_in_day in days.items():
                for provider, bucket in providers_in_day.items():
                    acc = per_provider.setdefault(provider, {
                        "input": 0, "cache_read": 0, "cache_write": 0,
                        "output": 0, "requests": 0, "first": bucket["first"],
                        "last": bucket["last"],
                    })
                    for key in ("input", "cache_read", "cache_write",
                                "output", "requests"):
                        acc[key] += bucket[key]
                    acc["first"] = min(acc["first"], bucket["first"])
                    acc["last"] = max(acc["last"], bucket["last"])
                    day_totals[day] = day_totals.get(day, 0) + sum(
                        bucket[k] for k in ("input", "cache_read",
                                            "cache_write", "output"))
                    first = (bucket["first"] if first is None
                             else min(first, bucket["first"]))
                    last = (bucket["last"] if last is None
                            else max(last, bucket["last"]))
            providers_out = []
            for provider, sums in per_provider.items():
                providers_out.append({
                    "provider": provider,
                    "input": sums["input"], "cache_read": sums["cache_read"],
                    "cache_write": sums["cache_write"], "output": sums["output"],
                    "total": sum(sums[k] for k in
                                 ("input", "cache_read", "cache_write", "output")),
                    "requests": sums["requests"],
                })
            providers_out.sort(key=lambda p: (-p["total"], p["provider"]))
            switches, model = self._switch_series(row, since_epoch)
            out[name] = {
                "input": sum(p["input"] for p in providers_out),
                "cache_read": sum(p["cache_read"] for p in providers_out),
                "cache_write": sum(p["cache_write"] for p in providers_out),
                "output": sum(p["output"] for p in providers_out),
                "requests": sum(p["requests"] for p in providers_out),
                "total": sum(p["total"] for p in providers_out),
                "providers": providers_out,
                "days": day_totals,
                "first": first,
                "last": last,
                "model": model,
                "switches": switches,
            }
        return out

    # -- the time series ---------------------------------------------------
    def series(self, window: str = "7d", *, sessions: Iterable[dict] = (),
               project: str = "", include_killed: bool = False,
               include_archived: bool = False,
               now: Optional[float] = None) -> dict:
        """The fleet's token use over time, for the Chart page's series.

        One point per bucket of :data:`SERIES_BUCKETS` chosen for the window,
        each carrying the four components' **sums** in that bucket and the
        requests that produced them. The sums are what the reader is owed:
        drawn as a rate (``total / bucket_s``) the area under the curve is
        the window's total, which is the same reading the session overlay's
        throughput line gives for one call (claunch-x15q5).

        ``sessions`` is the metadata :func:`session_meta` builds, filtered the
        way the page filters it: ``project`` ("" = every project) and the two
        ended-category boxes. A session the ledger has no record of
        contributes nothing and is counted in ``unmetered`` rather than
        drawn as zero, the same rule the overview's rows follow.

        ``by_session`` carries the same four components per session over the
        same window and the same filters, so a row on the Chart page can print
        the figure the Stats page's table prints for it without a second read:
        the two are one pass over one set of bins. A wanted session the ledger
        cannot speak for is absent from the map (the page draws ``-`` there,
        never a zero -- the overview's rule, claunch-ha2s9).

        Buckets are aligned to multiples of their own width since the epoch,
        so a poll draws the same boundaries as the poll before it, and the
        bucket at the right edge is the one in progress (``until`` says when
        it was cut, and the page draws it to there).
        """
        if window not in SERIES_WINDOWS:
            raise ValueError(
                f"window must be one of: {', '.join(SERIES_WINDOWS)}")
        now = time.time() if now is None else now
        wanted = set()
        for item in sessions:
            name = str(item.get("name") or "")
            if not name:
                continue
            if project and str(item.get("project") or "").strip() != project:
                continue
            if _hidden(str(item.get("category") or ""),
                       include_killed, include_archived):
                continue
            wanted.add(name)
        # The ledger's own names are not added: a record whose session the
        # store no longer lists cannot be told killed from archived, so the
        # page's two boxes have nothing to act on. What the series draws is
        # what the rows beside it draw.
        unmetered = 0
        bins = [(b, name) for name in wanted
                for b in (self.sessions.get(name, {}).get("bins") or {})]
        span = SERIES_WINDOWS[window]
        if span is None:
            start = min((b for b, _ in bins), default=None)
            start = now - 86400 if start is None else float(start)
        else:
            start = now - span
        start = min(start, now - 60.0)
        # The narrowest width that keeps the aligned grid within one screen.
        # A bucket is aligned to a multiple of its own width (so every poll
        # draws the same boundaries), which moves `start` back by up to one
        # width -- the count is therefore taken after aligning, not from the
        # unaligned span, or a 24-hour window came back one point long.
        width = SERIES_BUCKETS[-1]
        for candidate in SERIES_BUCKETS:
            aligned = math.floor(start / candidate) * candidate
            if math.ceil((now - aligned) / candidate) <= SERIES_MAX_POINTS:
                width = candidate
                break
        start = math.floor(start / width) * width
        count = max(1, math.ceil((now - start) / width))
        points = [{"t": start + i * width, "input": 0, "cache_read": 0,
                   "cache_write": 0, "output": 0, "total": 0, "requests": 0}
                  for i in range(count)]
        # Per session, the same four components over the same buckets: the
        # Chart page's rows carry the figure the Stats table carries for the
        # session, and one pass over `bins` answers both. A session the ledger
        # has seen starts at zero -- measured, and it spent nothing in this
        # window -- while one it has never seen stays out of the map, which is
        # how the page tells "nothing here" from "not measured here".
        by_session: Dict[str, dict] = {
            name: {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
                   "total": 0, "requests": 0}
            for name in wanted if name in self.sessions
        }
        for at, name in bins:
            if at < start or at >= start + count * width:
                continue
            slot = points[int((at - start) // width)]
            acc = self.sessions[name]["bins"][at]
            slot["input"] += acc[0]
            slot["cache_read"] += acc[1]
            slot["cache_write"] += acc[2]
            slot["output"] += acc[3]
            slot["requests"] += acc[4]
            slot["total"] += acc[0] + acc[1] + acc[2] + acc[3]
            row = by_session[name]
            for i, key in enumerate(("input", "cache_read", "cache_write",
                                     "output")):
                row[key] += acc[i]
            row["total"] += acc[0] + acc[1] + acc[2] + acc[3]
            row["requests"] += acc[4]
        totals = {key: sum(p[key] for p in points) for key in
                  ("input", "cache_read", "cache_write", "output",
                   "total", "requests")}
        for name in wanted:
            if name not in self.sessions:
                unmetered += 1
        return {
            "window": window,
            "bucket_s": width,
            "start": start,
            "until": now,
            "points": points,
            "totals": totals,
            "by_session": by_session,
            "cache_share": (totals["cache_read"] / totals["total"]
                            if totals["total"] else None),
            "sessions": len(wanted) - unmetered,
            "unmetered": unmetered,
            "ledger_since": self.earliest_day(),
        }


def window_start_epoch(since_day: str, zone: tzinfo) -> float:
    day = datetime.strptime(since_day, "%Y-%m-%d").replace(tzinfo=zone)
    return day.timestamp()


# --------------------------------------------------------------------------- #
# the other source: sessions the ledger cannot speak for
# --------------------------------------------------------------------------- #
def _read_transcript(sdef, zone: tzinfo) -> Optional[dict]:
    """One session's transcript reading, or ``None`` when it has none.

    This is the same call the session detail panel makes
    (``/api/sessions/<name>/stats``), so a window figure taken from here and
    the panel's all-time figure come from one place and cannot disagree about
    what a token is.
    """
    try:
        return sessionstats.for_session(sdef, unit=UNIT, zone=zone)
    except Exception:  # noqa: BLE001 - a missing transcript is not a 500
        return None


class Transcripts:
    """Window figures for sessions that never reached the routing shim.

    An OAuth route (Anthropic's subscription, a Codex login) writes no metering
    line. :mod:`sessionstats` reads those sessions' transcripts, and this keeps
    that reading for a short while so a poll pays only for what was appended.
    ``reader`` is injectable so a test can answer without a transcript on disk.
    """

    def __init__(self, *, zone: Optional[tzinfo] = None,
                 ttl: float = TRANSCRIPT_TTL, reader=None) -> None:
        self.zone = zone or local_zone()
        self.ttl = ttl
        self.reader = reader or (lambda sdef: _read_transcript(sdef, self.zone))
        self._rows: Dict[str, Tuple[float, Optional[dict]]] = {}
        self._lock = threading.Lock()

    def reading(self, sdef, now: Optional[float] = None) -> Optional[dict]:
        name = str(getattr(sdef, "name", "") or "")
        if not name:
            return None
        moment = time.time() if now is None else now
        with self._lock:
            hit = self._rows.get(name)
        if hit is not None and moment - hit[0] < self.ttl:
            return hit[1]
        try:
            reading = self.reader(sdef)
        except Exception:  # noqa: BLE001 - a broken transcript is not a 500
            reading = None
        with self._lock:
            self._rows[name] = (moment, reading)
        return reading

    def forget(self, name: str) -> None:
        with self._lock:
            self._rows.pop(name, None)


def transcript_window(reading: Optional[dict],
                      days: List[str]) -> Optional[dict]:
    """The part of one transcript reading that falls inside ``days``.

    The four components and the day keys are the ones the ledger sums use, so
    a transcript row and a ledger row mean the same thing on the page. A
    reading that is still being caught up is reported as ``partial``.
    """
    if not reading or not reading.get("available"):
        return None
    wanted = set(days)
    out = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
           "total": 0, "requests": 0, "days": {}, "first": None, "last": None,
           "partial": bool(reading.get("partial"))}
    for bucket in reading.get("buckets") or ():
        start = str(bucket.get("start") or "")
        if start[:10] not in wanted:
            continue
        fresh = _number(bucket.get("input"))
        cache_read = _number(bucket.get("cache_read"))
        cache_write = _number(bucket.get("cache_write"))
        output = _number(bucket.get("output"))
        total = fresh + cache_read + cache_write + output
        if not total and not _number(bucket.get("requests")):
            continue
        out["input"] += fresh
        out["cache_read"] += cache_read
        out["cache_write"] += cache_write
        out["output"] += output
        out["total"] += total
        out["requests"] += _number(bucket.get("requests"))
        out["days"][start[:10]] = out["days"].get(start[:10], 0) + total
    # ``first``/``last`` stay None on purpose. A transcript reading is bucketed
    # by day, so ``start`` is the day boundary -- midnight -- and reporting it
    # as the last activity would put a time on the row that nothing measured.
    # The ledger sums carry real timestamps; this path has only the day.
    return out



# --------------------------------------------------------------------------- #
# session metadata and the overview payload
# --------------------------------------------------------------------------- #
def _stamp(value) -> Optional[float]:
    return _epoch(value) if value else None


def _activity(meta: dict) -> Optional[float]:
    """The newest moment this session can have been doing something."""
    stamps = [_stamp(meta.get(k)) for k in
              ("last_activity_at", "exited_at", "paused_at", "held_at",
               "delayed_at", "failed_at", "archived_at", "created_at")]
    present = [s for s in stamps if s is not None]
    return max(present) if present else None


def _ended(category: str) -> bool:
    return category in ENDED_CATEGORIES


def _profile_label(borrow, null_token) -> str:
    """How the page names the credentials a session was running on."""
    if null_token:
        return "null"
    return str(borrow or "").strip() or "own"


def profile_switches(events) -> List[dict]:
    """The profile changes a session's own event history records.

    ``SessionManager.events`` writes a ``borrow`` event whenever a reborrow
    moves a session onto another profile's credentials, or clears the borrow;
    its details carry ``previous``/``current`` and the null-token pair. The
    metering ledger cannot see that: a reborrow whose two profiles reach the
    same upstream leaves no provider change behind, so a switch list built
    from the ledger alone says nothing happened.

    Entries come back in the ledger's switch shape -- ``kind: "profile"``,
    with the profile names in ``from``/``to`` -- so the page draws one list.
    The ``at`` here is the event's own stamp; the overview re-renders it in
    the page's zone.
    """
    out = []
    for event in events or ():
        if not isinstance(event, dict) or event.get("kind") != "borrow":
            continue
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        before = _profile_label(details.get("previous"),
                                details.get("previous_null"))
        after = _profile_label(details.get("current"),
                               details.get("null_token"))
        if before == after:
            continue
        at = _epoch(event.get("at"))
        if at is None:
            continue
        out.append({"at": event.get("at"), "at_epoch": at,
                    "kind": "profile", "from": before, "to": after,
                    "from_model": "", "to_model": ""})
    return out


def _switches_in_window(item: dict, since_epoch: float,
                        zone: tzinfo) -> List[dict]:
    """``profile_switches`` of one meta, cut to the window and re-stamped."""
    out = []
    for entry in item.get("profile_switches") or ():
        at = entry.get("at_epoch")
        if at is None or at < since_epoch:
            continue
        out.append({**entry, "at": _iso(at, zone)})
    return out


def _hidden(category: str, include_killed: bool, include_archived: bool) -> bool:
    """Whether a session in ``category`` is left out of the table.

    The two endings are separate answers because they mean different things to
    a reader: a killed session was ended early, an archived one was put away
    after its work finished. A page that shows one usually wants to see the
    other too, but not always -- so the caller says which.
    """
    if category == "killed":
        return not include_killed
    if category == "archived":
        return not include_archived
    return False


def _empty_row(name: str) -> dict:
    return {
        "session": name, "status": "", "category": "", "harness": "",
        "profile": "", "borrow": "", "issue": "", "project": "",
        "source": "none", "provider": "", "providers": [],
        "model": "", "input": 0, "cache_read": 0, "cache_write": 0,
        "output": 0, "total": 0, "requests": 0,
        "first_at": None, "last_at": None, "days": {}, "switches": [],
        "declared_provider": "", "ended": False,
    }


def overview(*, window: str = "today", include_ended: bool = False,
             include_killed: bool = False, include_archived: bool = False,
             now: Optional[float] = None, zone: Optional[tzinfo] = None,
             sessions: Iterable[dict] = (), ledger: Optional[Ledger] = None,
             labels: Optional[Dict[str, str]] = None,
             doc: Optional[dict] = None,
             transcripts: Optional["Transcripts"] = None) -> dict:
    """The Stats page's overview payload for one window.

    ``sessions`` is the daemon's session list reduced to the metadata a row
    needs (name, harness, profile, borrow, issue, status, category and the
    timestamps); :func:`session_meta` builds it from a ``Session``. The
    ledger is read here, so a caller that keeps one :class:`Ledger` across
    polls pays only for what was appended.

    ``transcripts`` answers for the sessions the ledger cannot: an OAuth route
    writes no metering line, so a session on one gets its window figures from
    its own transcript (the same reading the detail panel shows). Omitting it
    leaves those rows at ``source: "none"``.

    ``include_killed`` and ``include_archived`` are the page's two boxes;
    ``include_ended`` turns both on at once and is what the older ``?ended=1``
    asked for.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(WINDOWS)}")
    include_killed = bool(include_killed or include_ended)
    include_archived = bool(include_archived or include_ended)
    zone = zone or local_zone()
    now = time.time() if now is None else now
    start, end = window_bounds(window, now, zone)
    days = window_days(window, now, zone)
    labels = provider_labels(doc) if labels is None else labels
    if ledger is None:
        ledger = Ledger(labels=labels, zone=zone)
    ledger.labels = dict(labels)
    ledger.refresh(now)

    sums = ledger.window(days[0], start)
    meta = {str(m.get("name")): m for m in sessions if m.get("name")}

    rows: List[dict] = []
    ended_excluded = 0
    killed_excluded = 0
    archived_excluded = 0
    seen = set()
    for name, usage in sums.items():
        item = meta.get(name, {})
        category = str(item.get("category") or "")
        if item and _hidden(category, include_killed, include_archived):
            ended_excluded += 1
            killed_excluded += 1 if category == "killed" else 0
            archived_excluded += 1 if category == "archived" else 0
            continue
        seen.add(name)
        row = _empty_row(name)
        row.update({
            "source": "ledger",
            "status": item.get("status") or "gone",
            "category": category or "gone",
            "harness": item.get("harness") or "",
            "profile": item.get("profile") or "",
            "borrow": item.get("borrow") or "",
            "issue": item.get("issue") or "",
            "project": item.get("project") or "",
            "model": usage.get("model") or "",
            "input": usage["input"], "cache_read": usage["cache_read"],
            "cache_write": usage["cache_write"], "output": usage["output"],
            "requests": usage["requests"], "total": usage["total"],
            "providers": usage["providers"],
            "days": usage["days"],
            "switches": usage["switches"],
            "first_at": _iso(usage["first"], zone) if usage.get("first") else None,
            "last_at": _iso(usage["last"], zone) if usage.get("last") else None,
            "declared_provider": declared_provider(
                item.get("profile") or "", item.get("borrow") or "", doc),
        })
        row["provider"] = (usage["providers"][0]["provider"]
                           if usage["providers"] else "")
        rows.append(row)

    # Sessions the ledger cannot speak for. An unmetered row is listed only
    # when the window can have touched it; otherwise every session the machine
    # ever ran would pad a table about today. Two ways to fill it: the
    # session's own transcript (an OAuth route or one the shim never saw), or
    # nothing at all -- the row is drawn with a dash and never with a zero.
    unmetered = 0
    transcript_rows = 0
    transcript_skipped = 0
    partial = False
    for name, item in meta.items():
        if name in seen or name in sums:
            continue
        category = str(item.get("category") or "")
        if _hidden(category, include_killed, include_archived):
            ended_excluded += 1
            killed_excluded += 1 if category == "killed" else 0
            archived_excluded += 1 if category == "archived" else 0
            continue
        last = _activity(item)
        created = _stamp(item.get("created_at"))
        if last is None:
            continue
        if last < start:
            continue
        if created is not None and created > end:
            continue
        part = None
        sdef = item.get("sdef")
        if transcripts is not None and sdef is not None:
            if transcript_rows + unmetered < MAX_TRANSCRIPTS:
                part = transcript_window(transcripts.reading(sdef, now), days)
            else:
                transcript_skipped += 1
        if part and part["total"]:
            partial = partial or part["partial"]
            row = _empty_row(name)
            row.update({
                "source": "transcript",
                "status": item.get("status") or "", "category": category,
                "harness": item.get("harness") or "",
                "profile": item.get("profile") or "",
                "borrow": item.get("borrow") or "",
                "issue": item.get("issue") or "",
                "project": item.get("project") or "",
                "input": part["input"], "cache_read": part["cache_read"],
                "cache_write": part["cache_write"], "output": part["output"],
                "requests": part["requests"], "total": part["total"],
                "days": part["days"],
                "first_at": _iso(part["first"], zone) if part["first"] else None,
                "last_at": _iso(part["last"], zone) if part["last"] else None,
                "ended": _ended(category),
                "declared_provider": declared_provider(
                    item.get("profile") or "", item.get("borrow") or "", doc),
            })
            row["provider"] = row["declared_provider"]
            rows.append(row)
            transcript_rows += 1
            continue
        row = _empty_row(name)
        row.update({
            "status": item.get("status") or "", "category": category,
            "harness": item.get("harness") or "",
            "profile": item.get("profile") or "",
            "borrow": item.get("borrow") or "",
            "issue": item.get("issue") or "",
            "project": item.get("project") or "",
            "ended": _ended(category),
            "declared_provider": declared_provider(
                item.get("profile") or "", item.get("borrow") or "", doc),
            "last_at": _iso(last, zone),
            "first_at": (_iso(created, zone) if created else None),
        })
        row["provider"] = row["declared_provider"]
        rows.append(row)
        unmetered += 1

    # The ledger proves the provider and model changes; only the session's own
    # event history proves a reborrow. One list, in time order, so a reader
    # sees both kinds of change where they happened.
    for row in rows:
        extra = _switches_in_window(meta.get(row["session"]) or {}, start, zone)
        if not extra:
            continue
        merged = sorted(list(row.get("switches") or []) + extra,
                        key=lambda s: s.get("at_epoch") or 0)
        row["switches"] = merged[-MAX_SWITCHES:]

    rows.sort(key=lambda r: (-(r["total"] or 0), r["session"]))

    by_provider: Dict[str, dict] = {}
    for row in rows:
        for entry in row["providers"]:
            acc = by_provider.setdefault(entry["provider"],
                                         {"provider": entry["provider"],
                                          "sessions": 0, "total": 0, "requests": 0})
            acc["sessions"] += 1
            acc["total"] += entry["total"]
            acc["requests"] += entry["requests"]
    totals = {k: sum(r[k] for r in rows) for k in
              ("input", "cache_read", "cache_write", "output", "total", "requests")}
    total = totals["total"]
    return {
        "window": window,
        "unit": UNIT,
        "days": days,
        "since": _iso(start, zone),
        "until": _iso(end, zone),
        "zone": datetime.fromtimestamp(now, zone).strftime("%z"),
        "include_ended": bool(include_killed and include_archived),
        "include_killed": bool(include_killed),
        "include_archived": bool(include_archived),
        "totals": totals,
        "cache_share": (totals["cache_read"] / total) if total else None,
        "providers": sorted(by_provider.values(),
                            key=lambda p: (-p["total"], p["provider"])),
        "sessions": rows,
        "counts": {
            "sessions": len(rows),
            "metered": sum(1 for r in rows if r["source"] == "ledger"),
            "transcript": transcript_rows,
            "unmetered": unmetered,
            "transcript_skipped": transcript_skipped,
            "ended_excluded": ended_excluded,
            "killed_excluded": killed_excluded,
            "archived_excluded": archived_excluded,
        },
        # The ledger's own start: a window wider than it cannot be answered
        # from here, and the page says so instead of drawing a short bar.
        "ledger_since": ledger.earliest_day(),
        # A transcript still being caught up: the row grows on the next poll.
        "partial": partial,
    }


def session_meta(session, events=None) -> dict:
    """One ``Session`` reduced to what a row (and the filters) read.

    Deliberately small: the overview asks this of every session on the
    machine, and ``Session.info()`` carries a definition, a briefing and
    environment that no table column uses.

    ``events`` is the daemon's per-session event history
    (``SessionManager.events``); given it, the meta carries the profile
    changes a reborrow left behind, which the metering ledger cannot prove.
    """
    sdef = getattr(session, "sdef", None)
    name = getattr(sdef, "name", "") or ""
    category = ""
    try:
        from . import session as session_mod

        category = session_mod.session_category(session)
    except Exception:  # noqa: BLE001 - a double without the helper is not a 500
        category = ""
    activity = None
    try:
        activity = session.last_activity_at()
    except Exception:  # noqa: BLE001
        activity = None
    seen_events = []
    if events is not None:
        try:
            seen_events = events.rows(session)
        except Exception:  # noqa: BLE001 - a double without the store is not a 500
            seen_events = []
    return {
        "name": name,
        "harness": getattr(sdef, "harness", "") or "",
        "profile": getattr(sdef, "profile", "") or "",
        "borrow": getattr(sdef, "borrow", "") or "",
        "issue": getattr(sdef, "issue", "") or "",
        "project": getattr(sdef, "project", "") or "",
        "status": (session.status() if not getattr(session, "exited", False)
                   else "exited"),
        "category": category,
        "created_at": getattr(session, "created_at", None),
        "last_activity_at": activity,
        # The definition itself, for the transcript reader: an OAuth session's
        # figures are read from the conversation this points at.
        "sdef": sdef,
        # Profile changes the session's own event history records (reborrow).
        "profile_switches": profile_switches(seen_events),
        "exited_at": getattr(session, "exited_at", None),
        "paused_at": getattr(session, "paused_at", None),
        "held_at": getattr(session, "held_at", None),
        "delayed_at": getattr(session, "delayed_at", None),
        "failed_at": getattr(session, "failed_at", None),
        "archived_at": getattr(session, "archived_at", None),
    }
