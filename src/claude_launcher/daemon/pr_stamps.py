"""Stamps the PR page needs but a recorded row may not carry.

A row on ``#/pr`` is dated by its ``at`` and titled by its ``title``. The
wizard writes both. A v2 run's landing record carries ``at`` from
claunch-ijkge.4.1.3 on, but every row written before that — and every row
whose ``prs`` collection was settled away at the end of its run — reached the
page with neither, so the timeline collapsed to one ``날짜 미상`` bucket.

This module is the sidecar those values are recovered into:

* :func:`record` / :func:`write` — the one place a recovered value is stored.
  The file lives beside the daemon (``pr-stamps.json``), never inside another
  session's run directory: those belong to their sessions, and this module
  only ever reads them (through :mod:`tools.pr_stamp_backfill`).
* :func:`stamp` — what :mod:`session_prs` merges into a row that lacks a value.
  It never calls out: a cache hit answers, a miss answers nothing.
* :func:`fill` — the ``gh`` half, run off the request path: a miss is looked up
  once, the answer (or the failure) is cached, so polling stays a local read.
  It runs only where :func:`enable` was called — the daemon's own start-up —
  so code that merely reads the list (tests, CLI tools) never spawns ``gh``.

Both halves share one TTL rule: a title is kept for :data:`TITLE_TTL` and a
failure for :data:`FAILURE_TTL`, so a repository that cannot be reached is
asked again every fifteen minutes rather than on every poll.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from .. import atomic
from . import paths, pr_actions

#: A PR title never changes in practice; one lookup is enough for a week.
TITLE_TTL = 7 * 24 * 3600.0
#: A failed lookup is retried sooner than a success, but not on every poll.
FAILURE_TTL = 15 * 60.0
#: At most this many lookups per :func:`fill` pass (one gh call each).
FILL_LIMIT = 25

_PULL = re.compile(r"^/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?$")

_lock = threading.RLock()
_memo: Dict[str, Any] = {"path": None, "mtime": None, "entries": {}}
_filler = threading.Lock()
_enabled = False


def enable() -> None:
    """Let :func:`kick` start the ``gh`` pass.

    Called by the daemon once at start-up. Off by default so a process that
    only reads the PR list — a test, a CLI tool — cannot be made to run
    ``gh`` by the code it is exercising."""
    global _enabled
    _enabled = True


def disable() -> None:
    """Stop :func:`kick` from starting a pass (a test's teardown, mostly)."""
    global _enabled
    _enabled = False


def path():
    """The sidecar file this module reads and writes."""
    return paths.daemon_dir() / "pr-stamps.json"


def key(url: str) -> Optional[str]:
    """``<host>/<owner>/<repo>#<number>`` for a PR url, or ``None``.

    The same shape the PR routes use, so a value learned here is found again
    from any row that names that PR."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    match = _PULL.match(parsed.path or "")
    if not match or not parsed.netloc:
        return None
    return f"{parsed.netloc}/{match[1]}#{int(match[2])}"


def _now() -> float:
    return time.time()


def _read(path_obj) -> Dict[str, dict]:
    try:
        raw = json.loads(path_obj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _load() -> Dict[str, dict]:
    """The sidecar, re-read when the file changed under us."""
    target = path()
    try:
        stamp = target.stat().st_mtime_ns
    except OSError:
        stamp = None
    with _lock:
        if _memo["path"] == str(target) and _memo["mtime"] == stamp:
            return _memo["entries"]
        entries = _read(target) if stamp is not None else {}
        _memo.update({"path": str(target), "mtime": stamp, "entries": entries})
        return entries


def write(entries: Dict[str, dict]) -> None:
    """Merge ``entries`` into the sidecar (newest value wins per field)."""
    if not entries:
        return
    target = path()
    with _lock:
        merged = dict(_load())
        for name, fields in entries.items():
            if not isinstance(fields, dict):
                continue
            row = dict(merged.get(name) or {})
            row.update({k: v for k, v in fields.items() if v is not None})
            merged[name] = row
        target.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(target) as tmp:
            tmp.write_text(json.dumps({"version": 1, "entries": merged},
                                      ensure_ascii=False), encoding="utf-8")
            atomic.replace(tmp, target)
        try:
            stamp = target.stat().st_mtime_ns
        except OSError:
            stamp = None
        _memo.update({"path": str(target), "mtime": stamp, "entries": merged})


def entries() -> Dict[str, dict]:
    """Every entry the sidecar holds, keyed by PR. A copy, so a caller cannot
    mutate the cache by accident."""
    return {name: dict(row) for name, row in _load().items()}


def record(url: str, **fields) -> None:
    """Store ``fields`` (``at``, ``title``, ``status``) for one PR url."""
    name = key(url)
    if name:
        write({name: fields})


def stamp(url: str) -> Dict[str, Any]:
    """What the sidecar knows about ``url``: ``at`` and ``title`` when present.

    Local read only — this is called on the polling path, so a miss returns
    ``{}`` and the row keeps saying the date is unknown."""
    name = key(url)
    if not name:
        return {}
    entry = _load().get(name) or {}
    out: Dict[str, Any] = {}
    at = entry.get("at")
    if isinstance(at, str) and at:
        out["at"] = at
    title = entry.get("title")
    if isinstance(title, str) and title:
        out["title"] = title
    return out


def _stale(entry: dict, now: float) -> bool:
    """Whether this entry should be asked about again.

    A known title is kept until the lookup that learned it is
    :data:`TITLE_TTL` old. A title with no fetch time (written by the wizard's
    record, or by hand) is not re-asked at all — it is a value someone
    already had."""
    if entry.get("title"):
        fetched = entry.get("fetched_at")
        return isinstance(fetched, (int, float)) and now - fetched >= TITLE_TTL
    tried = entry.get("tried_at")
    if isinstance(tried, (int, float)) and now - tried < FAILURE_TTL:
        return False
    return True


def pending(urls: Iterable[str]) -> List[str]:
    """The urls whose title is still unknown and not recently failed."""
    now = _now()
    entries = _load()
    out: List[str] = []
    seen = set()
    for url in urls:
        name = key(url)
        if not name or name in seen:
            continue
        seen.add(name)
        if _stale(entries.get(name) or {}, now):
            out.append(url)
        if len(out) >= FILL_LIMIT:
            break
    return out


def fill(urls: Iterable[str], *, run=None) -> Dict[str, int]:
    """Look the missing titles up with ``gh`` and cache what comes back.

    One ``gh pr view`` per PR (the same normalised call the detail route
    uses), so this is deliberately called off the request path — see
    :func:`kick`."""
    todo = pending(urls)
    filled = failed = 0
    entries: Dict[str, dict] = {}
    now = _now()
    for url in todo:
        name = key(url)
        if not name:
            continue
        host, rest = name.split("/", 1)
        repo, number = rest.rsplit("#", 1)
        owner, _, repo_name = repo.partition("/")
        result = pr_actions.detail(host, owner, repo_name, int(number), run=run)
        pr = result.get("pr") if isinstance(result, dict) else None
        title = (pr or {}).get("title") if isinstance(pr, dict) else ""
        if result.get("ok") and isinstance(title, str) and title:
            entries[name] = {"title": title, "fetched_at": now}
            filled += 1
        else:
            error = result.get("error") if isinstance(result, dict) else None
            why = ""
            if isinstance(error, dict):
                why = str(error.get("message") or error.get("kind") or "")
            entries[name] = {"tried_at": now, "error": why[:200]}
            failed += 1
    write(entries)
    return {"filled": filled, "failed": failed, "asked": len(todo)}


def kick(urls: Iterable[str]) -> bool:
    """Start the ``gh`` half in the background when something is missing.

    Returns whether an attempt is in flight. The gate is a plain lock, not a
    queue: two polls in the same second schedule one pass. A caller outside
    the daemon process gets ``False`` — see :func:`enable`."""
    if not _enabled:
        return False
    urls = list(urls)
    if not pending(urls):
        return False
    if not _filler.acquire(blocking=False):
        return True

    def work() -> None:
        try:
            fill(urls)
        finally:
            _filler.release()

    threading.Thread(target=work, name="pr-stamps", daemon=True).start()
    return True
