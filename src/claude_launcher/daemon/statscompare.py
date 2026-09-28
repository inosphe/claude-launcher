"""Several sessions' statistics side by side, for the Stats page's compare mode.

:mod:`sessionstats` reads one session. This module takes those readings and
answers "how do these sessions differ": a fixed set of per-session figures
(:data:`METRICS`), and for each figure the spread across the compared
sessions -- mean, median, standard deviation, minimum, maximum -- with every
session's z-score and rank against the others. It also lays the sessions'
buckets on one shared timeline so their activity can be drawn together.

The statistics are descriptive. A handful of sessions is a small sample and
nothing here tests a difference for significance; a z-score says how far one
session sits from the others' mean in units of their spread, and nothing
more. The standard deviation is the sample one (``n - 1``), so it needs two
sessions; with one, or with all values equal, z-scores are ``None``.

Only readings with ``available`` true enter the statistics; the others are
listed with their reason. The inputs are the readings themselves, so the
figures carry every approximation :mod:`sessionstats` states (estimated token
sizes, turn attribution for ``triggered``).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .sessionstats import CATEGORIES, UNITS

#: At most this many sessions in one comparison (the API refuses more).
MAX_SESSIONS = 12


def _total(r: dict) -> float:
    return r["totals"]["total"]


def _per_request(r: dict) -> Optional[float]:
    n = r["totals"]["requests"]
    return r["totals"]["total"] / n if n else None


def _cache_share(r: dict) -> Optional[float]:
    t = r["totals"]["total"]
    return r["totals"]["cache_read"] / t if t else None


def _active(r: dict) -> float:
    return sum(1 for b in r["buckets"] if b["requests"])


def _per_active(r: dict) -> Optional[float]:
    active = _active(r)
    return r["totals"]["requests"] / active if active else None


def _source(category: str, field: str) -> Callable[[dict], Optional[float]]:
    def value(r: dict) -> Optional[float]:
        total = sum(s[field] if field != "triggered" else s["triggered"]["total"]
                    for s in r["sources"])
        if not total:
            return None
        row = next(s for s in r["sources"] if s["category"] == category)
        return row["share"][field]
    return value


def _machine_share(field: str) -> Callable[[dict], Optional[float]]:
    """daemon + mesh: what reached the conversation without a person typing."""
    daemon, mesh = _source("daemon", field), _source("mesh", field)

    def value(r: dict) -> Optional[float]:
        a, b = daemon(r), mesh(r)
        return None if a is None else a + (b or 0.0)
    return value


#: (key, label, kind, value-of-one-reading). ``kind`` tells the page how to
#: print it: ``count``, ``tokens``, ``share`` (0..1) or ``ratio``.
METRICS: List[Tuple[str, str, str, Callable[[dict], Optional[float]]]] = [
    ("requests", "requests", "count", lambda r: r["totals"]["requests"]),
    ("tokens", "total tokens", "tokens", _total),
    ("output", "output tokens", "tokens", lambda r: r["totals"]["output"]),
    ("tokens_per_request", "tokens per request", "tokens", _per_request),
    ("cache_read_share", "cache read share", "share", _cache_share),
    ("active_buckets", "active buckets", "count", _active),
    ("requests_per_active_bucket", "requests per active bucket", "ratio", _per_active),
    ("compactions", "compactions", "count", lambda r: r["compactions"]),
    *[(f"{c}_messages_share", f"{c} input share", "share", _source(c, "messages"))
      for c in CATEGORIES],
    ("machine_messages_share", "daemon + mesh input share", "share",
     _machine_share("messages")),
    ("machine_tokens_share", "daemon + mesh token share", "share",
     _machine_share("tokens")),
    ("machine_triggered_share", "daemon + mesh triggered share", "share",
     _machine_share("triggered")),
    ("human_triggered_share", "human triggered share", "share",
     _source("human", "triggered")),
]


def describe(values: Sequence[Optional[float]]) -> dict:
    """Spread of one figure across sessions, plus each one's z-score and rank
    (1 = largest; ties share the better rank). ``None`` values are sessions
    the figure does not apply to (e.g. tokens per request with no request):
    they are left out of the statistics and get ``None`` back."""
    known = [v for v in values if v is not None]
    n = len(known)
    out: dict = {"n": n, "mean": None, "median": None, "stdev": None,
                 "min": None, "max": None}
    if n:
        ordered = sorted(known)
        mid = n // 2
        out.update(
            mean=sum(known) / n,
            median=ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2,
            min=ordered[0],
            max=ordered[-1],
        )
    if n >= 2:
        mean = out["mean"]
        out["stdev"] = math.sqrt(sum((v - mean) ** 2 for v in known) / (n - 1))
    sd = out["stdev"]
    out["z"] = [None if v is None or not sd else (v - out["mean"]) / sd
                for v in values]
    out["rank"] = [None if v is None else 1 + sum(1 for k in known if k > v)
                   for v in values]
    return out


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(stamp) if stamp else None


def timeline(readings: Sequence[dict], unit: str) -> dict:
    """The sessions' buckets on one axis: the union of their bucket starts,
    capped to the newest ``UNITS[unit]``. A session's value is 0 where it had
    no activity and ``None`` where its own reading does not reach (its
    buckets were capped before that point)."""
    starts = sorted({b["start"] for r in readings for b in r["buckets"]},
                    key=_parse)[-UNITS[unit]:]
    series = []
    for r in readings:
        own = {b["start"]: b for b in r["buckets"]}
        first = _parse(r["buckets"][0]["start"]) if r["buckets"] else None
        since = _parse(r.get("since"))
        # Activity before the first bucket exists only when the reading was
        # capped; before a session began there is simply nothing.
        capped = first is not None and since is not None and since < first
        tokens, requests = [], []
        for s in starts:
            b = own.get(s)
            if b is not None:
                tokens.append(b["total"])
                requests.append(b["requests"])
            elif capped and _parse(s) < first:
                tokens.append(None)
                requests.append(None)
            else:
                tokens.append(0)
                requests.append(0)
        series.append({"session": r["session"], "tokens": tokens,
                       "requests": requests})
    return {"starts": starts, "series": series}


def compare(readings: Sequence[dict], unit: str) -> dict:
    """The compare-mode answer for ``readings`` (from
    :func:`sessionstats.read_located`, in the order the sessions were asked
    for)."""
    ok = [r for r in readings if r.get("available")]
    sessions = []
    for r in readings:
        row = {"session": r.get("session", ""), "available": bool(r.get("available"))}
        if r.get("available"):
            row.update(harness=r.get("harness"), since=r.get("since"),
                       at=r.get("at"), partial=bool(r.get("partial")))
        else:
            row["reason"] = r.get("reason", "")
        sessions.append(row)
    metrics = []
    for key, label, kind, value in METRICS:
        values = [value(r) for r in ok]
        metrics.append({"key": key, "label": label, "kind": kind,
                        "values": values, **describe(values)})
    shares = [{"session": r["session"],
               **{s["category"]: {"messages": s["share"]["messages"],
                                  "tokens": s["share"]["tokens"],
                                  "triggered": s["share"]["triggered"]}
                  for s in r["sources"]}}
              for r in ok]
    out = {
        "unit": unit,
        "sessions": sessions,
        "compared": [r["session"] for r in ok],
        "metrics": metrics,
        "shares": shares,
        "timeline": timeline(ok, unit),
    }
    if any(r.get("partial") for r in ok):
        out["partial"] = True
    return out
