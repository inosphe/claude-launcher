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
import threading
import time
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

#: How many switches one session may keep. A session that alternates models
#: on every call would otherwise grow without bound; the newest are the ones
#: a reader came for.
MAX_SWITCHES = 40

UNIT = "day"

#: Providers whose backend is the vendor's own login rather than a declared
#: base URL. They never reach the routing shim, so the ledger has nothing for
#: them and their rows read ``source: "none"``.
OAUTH_PROVIDERS = ("default", "claude", "codex", "devin")


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
        try:
            paths = sorted(self.directory.glob("*.jsonl"))
        except OSError:
            paths = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            before = self._files.get(path.name)
            if before is not None and (stat.st_ino != before[0]
                                       or stat.st_size < before[1]):
                self.reset()
                before = None
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
        self._prune(now)

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
            "days": {}, "first": at, "last": at,
            "model": model, "last_key": (provider, model), "switches": [],
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
        row["model"] = model or row["model"]
        key = (provider, model)
        previous = row["last_key"]
        if previous != key and previous[0] is not None:
            kind = "provider" if previous[0] != provider else "model"
            row["switches"].append({
                "at": _iso(at, zone),
                "at_epoch": at,
                "kind": kind,
                "from": previous[0], "to": provider,
                "from_model": previous[1], "to_model": model,
            })
            if len(row["switches"]) > MAX_SWITCHES:
                del row["switches"][:-MAX_SWITCHES]
        row["last_key"] = key

    def _prune(self, now: float) -> None:
        if now - self._pruned_at < 3600 and self._pruned_at:
            return
        self._pruned_at = now
        oldest = day_key(now - RETENTION_DAYS * 86400, self.zone)
        for row in self.sessions.values():
            days = row["days"]
            for day in [d for d in days if d < oldest]:
                del days[day]

    def earliest_day(self) -> Optional[str]:
        """The oldest local date any kept record falls on, for the page's note.

        The ledger only exists since metering was switched on, so a "month"
        window can be narrower than a month of history. Saying when the
        record starts is how a reader tells "this fleet spent nothing last
        week" from "nobody was measuring last week".
        """
        days = [day for row in self.sessions.values() for day in row["days"]]
        return min(days) if days else None

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
                "model": row.get("model") or "",
                "switches": [s for s in row.get("switches") or []
                             if s["at_epoch"] >= since_epoch],
            }
        return out


def window_start_epoch(since_day: str, zone: tzinfo) -> float:
    day = datetime.strptime(since_day, "%Y-%m-%d").replace(tzinfo=zone)
    return day.timestamp()


# --------------------------------------------------------------------------- #
# session metadata and the overview payload
# --------------------------------------------------------------------------- #
def _stamp(value) -> Optional[float]:
    return _epoch(value) if value else None


def _activity(meta: dict) -> Optional[float]:
    """The newest moment this session can have been doing something."""
    stamps = [_stamp(meta.get(k)) for k in
              ("last_activity_at", "exited_at", "paused_at", "held_at",
               "delayed_at", "failed_at", "created_at")]
    present = [s for s in stamps if s is not None]
    return max(present) if present else None


def _ended(category: str) -> bool:
    return category in ("killed", "archived")


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
             now: Optional[float] = None, zone: Optional[tzinfo] = None,
             sessions: Iterable[dict] = (), ledger: Optional[Ledger] = None,
             labels: Optional[Dict[str, str]] = None,
             doc: Optional[dict] = None) -> dict:
    """The Stats page's overview payload for one window.

    ``sessions`` is the daemon's session list reduced to the metadata a row
    needs (name, harness, profile, borrow, issue, status, category and the
    timestamps); :func:`session_meta` builds it from a ``Session``. The
    ledger is read here, so a caller that keeps one :class:`Ledger` across
    polls pays only for what was appended.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(WINDOWS)}")
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
    seen = set()
    for name, usage in sums.items():
        item = meta.get(name, {})
        category = str(item.get("category") or "")
        if item and _ended(category) and not include_ended:
            ended_excluded += 1
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
    # ever ran would pad a table about today.
    unmetered = 0
    for name, item in meta.items():
        if name in seen or name in sums:
            continue
        category = str(item.get("category") or "")
        if _ended(category) and not include_ended:
            ended_excluded += 1
            continue
        last = _activity(item)
        created = _stamp(item.get("created_at"))
        if last is None:
            continue
        if last < start:
            continue
        if created is not None and created > end:
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
        "include_ended": bool(include_ended),
        "totals": totals,
        "cache_share": (totals["cache_read"] / total) if total else None,
        "providers": sorted(by_provider.values(),
                            key=lambda p: (-p["total"], p["provider"])),
        "sessions": rows,
        "counts": {
            "sessions": len(rows),
            "metered": sum(1 for r in rows if r["source"] == "ledger"),
            "unmetered": unmetered,
            "ended_excluded": ended_excluded,
        },
        # The ledger's own start: a window wider than it cannot be answered
        # from here, and the page says so instead of drawing a short bar.
        "ledger_since": ledger.earliest_day(),
        "partial": False,
    }


def session_meta(session) -> dict:
    """One ``Session`` reduced to what a row (and the filters) read.

    Deliberately small: the overview asks this of every session on the
    machine, and ``Session.info()`` carries a definition, a briefing and
    environment that no table column uses.
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
        "exited_at": getattr(session, "exited_at", None),
        "paused_at": getattr(session, "paused_at", None),
        "held_at": getattr(session, "held_at", None),
        "delayed_at": getattr(session, "delayed_at", None),
        "failed_at": getattr(session, "failed_at", None),
        "archived_at": getattr(session, "archived_at", None),
    }
