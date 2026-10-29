"""The daemon's own LLM calls, metered: briefing, observer, search, Jev.

The Stats page's overview reads :mod:`metering`'s ledger, which holds one line
per request the routing shim forwarded -- a *session's* turns. The daemon also
calls models itself, outside any session and outside the shim (claunch-60y0d):

* ``briefing`` -- :func:`daemon.briefing.call_llm`, a session's dashboard card
  (and ``probe``, the Settings page's connection test);
* ``observer`` -- :func:`daemon.observer.complete`, one pass over a session;
* ``embedding`` / ``rerank`` -- :class:`daemon.rag.RagClient`, the search
  index (``index``) and a search's query (``query``);
* ``jev`` -- :func:`daemon.jev.system_one`, the QA nudge and megathread
  routing.

None of these reach the shim, so none of them are in the metering ledger, and
before this module nothing kept their counts except the observer's own
per-row meter. Each call now appends one line here: when, which feature and
purpose, which provider and model answered, which session it was about, and
the token counts the provider reported.

A call whose provider reported no usage (a Cohere rerank answers in search
units, a failed call answers nothing) is kept with ``reported: false`` and
no token figure. It counts as a call, never as zero tokens.

Files are one per local month (``llm-usage/YYYY-MM.jsonl``) so the widest
window the page offers (30 days) reads at most two files.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from . import paths

log = logging.getLogger(__name__)

#: The features that write here, in the order the page lists them.
FEATURES = ("briefing", "observer", "embedding", "rerank", "jev")

#: The windows the page offers, the same as :data:`statsboard.WINDOWS`.
WINDOWS: Dict[str, int] = {"today": 1, "week": 7, "month": 30}

#: The token parts kept per line, the same four the overview reports.
PARTS = ("input", "cache_read", "cache_write", "output")

_lock = threading.Lock()


def ledger_dir() -> Path:
    return paths.daemon_dir() / "llm-usage"


def _month_file(at: datetime) -> Path:
    return ledger_dir() / f"{at.strftime('%Y-%m')}.jsonl"


def _int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def parts_of(usage) -> Optional[Dict[str, int]]:
    """The four token parts out of a provider's ``usage`` object, or ``None``
    when it carries no token count at all.

    OpenAI counts the prompt whole with the cached part inside it
    (``prompt_tokens_details.cached_tokens``, or DeepSeek's
    ``prompt_cache_hit_tokens``), so the cached part is taken out of
    ``input``. Anthropic counts the three input-side quantities apart. An
    embeddings or rerank answer that reports only ``total_tokens`` is all
    input.
    """
    if not isinstance(usage, dict):
        return None
    if "input_tokens" in usage or "output_tokens" in usage:
        fresh = _int(usage.get("input_tokens"))
        cached = _int(usage.get("cache_read_input_tokens"))
        written = _int(usage.get("cache_creation_input_tokens"))
        output = _int(usage.get("output_tokens"))
    else:
        prompt = _int(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details")
        cached = _int(usage.get("prompt_cache_hit_tokens"))
        if cached is None and isinstance(details, dict):
            cached = _int(details.get("cached_tokens"))
        output = _int(usage.get("completion_tokens"))
        written = None
        fresh = None if prompt is None else max(0, prompt - (cached or 0))
        if prompt is None and output is None:
            fresh = _int(usage.get("total_tokens"))
    if fresh is None and output is None and cached is None and written is None:
        return None
    return {"input": fresh or 0, "cache_read": cached or 0,
            "cache_write": written or 0, "output": output or 0}


def host_of(url: str) -> str:
    try:
        return urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""


def record(feature: str, *, provider: str = "", endpoint: str = "", model: str = "",
           session: str = "", purpose: str = "", usage=None, ok: bool = True,
           error: str = "", seconds: Optional[float] = None,
           at: Optional[float] = None, extra: bool = False) -> Optional[dict]:
    """Append one call to the ledger and return the line, never raising.

    ``provider`` is the provider name behind the profile the feature is
    configured with; a typed endpoint belongs to no provider, and its host
    names it instead. ``session`` is the session the call was about, empty
    when it was about none (an index pass over issues nobody owns, a
    connection test). ``extra`` marks a second or later share of one
    request (``record_shared``): it carries tokens but is not another call.
    """
    try:
        at = time.time() if at is None else float(at)
        parts = parts_of(usage)
        line = {
            # Floored: a rounded-up stamp could land after a "now" read a
            # moment later and fall outside a window that should hold it.
            "ts": math.floor(at * 1000) / 1000,
            "at": datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds"),
            "feature": str(feature),
            "purpose": str(purpose or ""),
            "provider": str(provider or "") or host_of(endpoint) or "unknown",
            "model": str(model or ""),
            "session": str(session or ""),
            "ok": bool(ok),
            "reported": parts is not None,
        }
        if extra:
            line["extra"] = True
        if parts is not None:
            line.update(parts)
            line["total"] = sum(parts.values())
        if error:
            line["error"] = str(error)[:200]
        if seconds is not None:
            line["ms"] = int(seconds * 1000)
        path = _month_file(datetime.fromtimestamp(at).astimezone())
        text = json.dumps(line, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
        return line
    except Exception:  # metering must never fail the call it meters
        log.warning("llm usage record failed", exc_info=True)
        return None


def split_usage(usage, weights: List[int]) -> List[Optional[dict]]:
    """One request's ``usage`` shared out over its inputs by ``weights``.

    An embeddings request carries many documents, often of different
    sessions, and the provider reports one count for the request. Each
    input's share is proportional to its weight (its length in characters);
    the remainder of the integer division goes to the last share so the
    shares sum to the reported count.
    """
    parts = parts_of(usage)
    if parts is None:
        return [None for _ in weights]
    ws = [max(0, w) for w in weights]
    if not sum(ws):
        ws = [1 for _ in weights]
    whole = sum(ws)
    out: List[dict] = []
    spent = {k: 0 for k in PARTS}
    for i, w in enumerate(ws):
        share = {}
        for k in PARTS:
            if i == len(ws) - 1:
                share[k] = parts[k] - spent[k]
            else:
                share[k] = parts[k] * w // whole
                spent[k] += share[k]
        out.append({"input_tokens": share["input"], "output_tokens": share["output"],
                    "cache_read_input_tokens": share["cache_read"],
                    "cache_creation_input_tokens": share["cache_write"]})
    return out


def record_shared(feature: str, owners: List[str], weights: List[int], usage, **kw) -> None:
    """Record one request whose inputs belong to several sessions: one line
    per owner, carrying that owner's share of ``usage``. Inputs of the same
    owner are summed first, so a request is at most one line per session.
    Every line after the first is marked ``extra`` so the request still
    counts as one call (and one error) everywhere but in its sessions' rows."""
    grouped: Dict[str, int] = {}
    for owner, w in zip(owners, weights):
        grouped[owner or ""] = grouped.get(owner or "", 0) + max(0, w)
    names = list(grouped)
    shares = split_usage(usage, [grouped[n] for n in names])
    for i, (name, share) in enumerate(zip(names, shares)):
        record(feature, session=name, usage=share, extra=i > 0, **kw)


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _months(start: datetime, end: datetime) -> List[str]:
    out, cur = [], start.replace(day=1)
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


def load(start: float, end: float, zone: Optional[tzinfo] = None) -> List[dict]:
    """Every line stamped in ``[start, end]``, oldest file first."""
    lo = datetime.fromtimestamp(start, zone).astimezone()
    hi = datetime.fromtimestamp(end, zone).astimezone()
    out: List[dict] = []
    for month in _months(lo, hi):
        path = ledger_dir() / f"{month}.jsonl"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                line = json.loads(raw)
            except ValueError:
                continue  # a torn write at the tail
            ts = line.get("ts") if isinstance(line, dict) else None
            if isinstance(ts, (int, float)) and start <= ts <= end:
                out.append(line)
    return out


def ledger_since() -> str:
    """The first day the ledger holds, or ``""`` when it holds nothing."""
    try:
        files = sorted(p for p in ledger_dir().glob("*.jsonl"))
    except OSError:
        return ""
    for path in files:
        try:
            with path.open(encoding="utf-8") as fh:
                first = json.loads(fh.readline() or "null")
        except (OSError, ValueError):
            continue
        if isinstance(first, dict) and first.get("at"):
            return str(first["at"])[:10]
    return ""


def _blank() -> dict:
    return {"calls": 0, "errors": 0, "unreported": 0, "total": 0,
            **{k: 0 for k in PARTS},
            "cost": 0.0, "priced_tokens": 0, "unpriced_tokens": 0}


def _rate_row(rates) -> Optional[dict]:
    return rates if isinstance(rates, dict) and not any(
        isinstance(v, dict) for v in rates.values()) else None


def rate_for(prices, provider: str, model: str) -> Optional[dict]:
    """The per-1M-token rates for ``model`` from the Settings price table
    (``daemon.model_prices``), or ``None`` when the table has no row for it.

    The same order as ``model_prices.rate_for`` (claunch-grs01.2 CONTRACT):
    the provider's row (``{provider: {model: {input, ...}}}``), then the
    ``'*'`` row, then a row keyed by model id at the top -- the older flat
    form, which that contract reads as ``'*'``."""
    if not isinstance(prices, dict) or not model:
        return None
    for key in ([provider] if provider else []) + ["*"]:
        nested = prices.get(key)
        if isinstance(nested, dict) and _rate_row(nested) is None:
            row = _rate_row(nested.get(model))
            if row is not None:
                return row
    return _rate_row(prices.get(model))


def _rate(rates: dict, key: str) -> float:
    try:
        v = float(rates.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v >= 0 else 0.0


def line_cost(line: dict, rates: Optional[dict]) -> Optional[float]:
    """What one ledger line costs at ``rates``, the same sum as the
    dashboard's ``readingCost``: a rate left unset counts as 0, except cache
    write, which falls back to the input rate (most providers bill a cache
    write as plain input)."""
    if rates is None or not line.get("reported"):
        return None
    write = rates.get("cache_write")
    write_rate = _rate(rates, "input") if write is None or write == "" else _rate(rates, "cache_write")
    return (_rate(rates, "input") * int(line.get("input") or 0)
            + _rate(rates, "cache_read") * int(line.get("cache_read") or 0)
            + write_rate * int(line.get("cache_write") or 0)
            + _rate(rates, "output") * int(line.get("output") or 0)) / 1e6


def _add(acc: dict, line: dict, every: bool = False) -> None:
    """Add ``line`` to ``acc``. An ``extra`` share of a request adds its
    tokens but not another call, unless ``every`` (a per-session row, where
    each of the request's sessions took part in that one call)."""
    if every or not line.get("extra"):
        acc["calls"] += 1
        if not line.get("ok", True):
            acc["errors"] += 1
        if not line.get("reported"):
            acc["unreported"] += 1
    if not line.get("reported"):
        return
    for k in PARTS:
        acc[k] += int(line.get(k) or 0)
    total = int(line.get("total") or 0)
    acc["total"] += total
    cost = line.get("cost")
    if cost is None:
        acc["unpriced_tokens"] += total
    else:
        acc["cost"] += cost
        acc["priced_tokens"] += total


def _grouped(lines: Iterable[dict], key: str, every: bool = False) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for line in lines:
        _add(out.setdefault(str(line.get(key) or ""), _blank()), line, every)
    return out


def _ordered(groups: Dict[str, dict], name: str, order=None) -> List[dict]:
    rows = [{name: k, **v} for k, v in groups.items()]
    if order:
        rank = {f: i for i, f in enumerate(order)}
        rows.sort(key=lambda r: (rank.get(r[name], len(rank)), r[name]))
    else:
        rows.sort(key=lambda r: (-r["total"], -r["calls"], r[name]))
    return rows


def priced(lines: List[dict], prices) -> List[dict]:
    """``lines`` with each one's ``cost`` at the Settings price table
    (``None`` where its model has no row there, or it reported no tokens)."""
    return [{**l, "cost": line_cost(l, rate_for(prices, str(l.get("provider") or ""),
                                                str(l.get("model") or "")))}
            for l in lines]


def summarize(lines: List[dict], days: List[str], zone: Optional[tzinfo] = None) -> dict:
    """The window's sums: overall, per feature, per provider (with its
    models), per session (with its features and providers) and per day.
    A line carrying ``cost`` (:func:`priced`) adds it to every row it falls
    in; ``priced_tokens``/``unpriced_tokens`` say how much of a row's total
    the cost covers."""
    totals = _blank()
    for line in lines:
        _add(totals, line)
    features = _ordered(_grouped(lines, "feature"), "feature", FEATURES)
    for row in features:
        mine = [l for l in lines if l.get("feature") == row["feature"]]
        row["purposes"] = _ordered(_grouped(mine, "purpose"), "purpose")
    providers = _ordered(_grouped(lines, "provider"), "provider")
    for row in providers:
        mine = [l for l in lines if l.get("provider") == row["provider"]]
        row["models"] = _ordered(_grouped(mine, "model"), "model")
        row["features"] = _ordered(_grouped(mine, "feature"), "feature", FEATURES)
    sessions = _ordered(_grouped(lines, "session", every=True), "session")
    for row in sessions:
        mine = [l for l in lines if (l.get("session") or "") == row["session"]]
        row["features"] = _ordered(_grouped(mine, "feature", every=True), "feature", FEATURES)
        row["providers"] = sorted({str(l.get("provider") or "") for l in mine})
        row["last_at"] = max((str(l.get("at") or "") for l in mine), default="")
    by_day: Dict[str, dict] = {d: {"day": d, **_blank(), "features": {}} for d in days}
    for line in lines:
        ts = line.get("ts")
        day = datetime.fromtimestamp(ts, zone).astimezone(zone).strftime("%Y-%m-%d")
        row = by_day.get(day)
        if row is None:
            continue
        _add(row, line)
        feat = str(line.get("feature") or "")
        row["features"][feat] = row["features"].get(feat, 0) + int(line.get("total") or 0)
    return {"totals": totals, "features": features, "providers": providers,
            "sessions": sessions, "days": list(by_day.values())}


def window_bounds(window: str, now: Optional[float] = None,
                  zone: Optional[tzinfo] = None):
    """``(start, end, days)``: local midnight ``days - 1`` ago, now, and the
    local dates between them -- the same whole-day windows as the overview."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of: {', '.join(WINDOWS)}")
    now = time.time() if now is None else now
    here = datetime.fromtimestamp(now, zone).astimezone(zone)
    first = (here - timedelta(days=WINDOWS[window] - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    days = []
    cur = first.date()
    while cur <= here.date():
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return first.timestamp(), now, days


def overview(window: str = "today", *, provider: str = "", session: str = "",
             now: Optional[float] = None, zone: Optional[tzinfo] = None,
             prices=None, currency: str = "$") -> dict:
    """The Stats page's embedded-LLM section for one window.

    ``provider`` / ``session`` narrow every figure to that provider's or that
    session's calls; ``all_providers`` keeps the unnarrowed provider list so
    the page's strip still offers the others. ``prices`` is the Settings
    price table (``daemon.model_prices``): each row's ``cost`` is estimated
    from it, in ``currency``.
    """
    start, end, days = window_bounds(window, now, zone)
    lines = priced(load(start, end, zone), prices)
    every = _ordered(_grouped(lines, "provider"), "provider")
    if provider:
        lines = [l for l in lines if l.get("provider") == provider]
    if session:
        # One session's view: each shared request it took part in is a call.
        lines = [{k: v for k, v in l.items() if k != "extra"} for l in lines
                 if (l.get("session") or "") == session]
    zone_name = datetime.fromtimestamp(end, zone).astimezone(zone).tzname() or ""
    return {
        "window": window,
        "provider": provider,
        "session": session,
        "since": datetime.fromtimestamp(start, zone).astimezone(zone).isoformat(timespec="seconds"),
        "until": datetime.fromtimestamp(end, zone).astimezone(zone).isoformat(timespec="seconds"),
        "zone": zone_name,
        "ledger_since": ledger_since(),
        "currency": currency or "$",
        "all_providers": [{"provider": r["provider"], "total": r["total"],
                           "calls": r["calls"]} for r in every],
        **summarize(lines, days, zone),
    }
