"""How much a session's scratch directory holds, measured off the loop and kept.

The dashboard wants one number on a session's card and a breakdown behind it,
and the rail that draws those cards polls once a second. A walk over a scratch
directory is not something to put on that path: the sessions on this machine
routinely hold 8,000 to 13,000 files there, and the walk costs hundreds of
milliseconds to seconds (the same measurement that made
``artifact_cleanup._snapshot`` carry a deadline). Paid once a second per
session it would spend the whole event loop -- the daemon serves every request
of every session from one loop, so the poll would stall the terminal as well.

So the walk runs in a worker thread (``asyncio.to_thread``), the answer is
kept per session for :data:`TTL` seconds, and the rail reads only the cached
number (a dict lookup). ``?refresh=1`` -- the panel's button -- measures again
now; overlapping asks for one session share a single walk
(:meth:`Storage.measure`), because two open tabs must not double the cost of
exactly the request a person made because the number looked stale.

The walk follows ``artifact_cleanup._snapshot`` in the one decision that
matters for correctness: **links are never followed**. A session's scratch
holds preview worktrees and junctions to other trees, and following one would
count another tree's bytes as this session's (and a junction can loop). A
link itself is counted as zero bytes. Beyond that this walk answers a weaker
question than the cleanup's fingerprint -- bytes, files, dirs, and the total
per top-level entry -- under a time and depth budget (:data:`MAX_SECONDS`,
:data:`MAX_DEPTH`) that sets ``truncated`` rather than presenting a partial
walk as a total.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import stat
import time

from . import paths

log = logging.getLogger(__name__)

#: How long a measurement stands before the panel's next open walks again.
TTL = 60.0

#: Wall-clock budget for one walk. Past it the answer is a partial one and
#: says so; a scratch on a slow disk must not hold a worker thread open for a
#: minute while the panel waits.
MAX_SECONDS = 8.0

#: Directory depth past which the walk stops descending. Deep enough for the
#: worktrees that live under a scratch, shallow enough that a pathological
#: tree cannot keep a thread busy.
MAX_DEPTH = 24


def _is_link(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty(name: str, root: Path) -> dict:
    return {
        "session": name,
        "root": str(root),
        "bytes": 0,
        "files": 0,
        "dirs": 0,
        "scanned_at": _now(),
        "stale": False,
        "truncated": False,
        "error": None,
        "entries": [],
    }


def scan(name: str) -> dict:
    """Measure ``name``'s scratch directory. Runs in a worker thread.

    ``root`` is ``paths.session_scratch_dir(name)``. A directory that is not
    there answers zeros rather than an error -- a session that never wrote
    anything is an ordinary session, not a broken one. A walk that could not
    read part of the tree reports ``truncated`` and carries the first refusal
    in ``error``: the total is then a lower bound and the caller can say so
    instead of presenting it as exact.
    """
    root = Path(paths.session_scratch_dir(name))
    doc = _empty(name, root)
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return doc
    except OSError as exc:
        doc["error"] = f"{type(exc).__name__}: {exc}"
        return doc
    if _is_link(st):
        # Measuring through it would count a tree this session may not own.
        doc["error"] = "scratch directory is a link; not followed"
        return doc
    if not stat.S_ISDIR(st.st_mode):
        doc["error"] = "scratch path is not a directory"
        return doc

    deadline = time.monotonic() + MAX_SECONDS
    base = os.fspath(root)
    totals: dict[str, dict] = {}
    errors: list[str] = []

    def on_error(exc: OSError) -> None:
        # A subdirectory the walk cannot enter still has to be visible: the
        # caller's number is a lower bound from here on.
        errors.append(f"{type(exc).__name__}: {exc}")

    for current, dirnames, filenames in os.walk(base, followlinks=False, onerror=on_error):
        if time.monotonic() > deadline:
            doc["truncated"] = True
            break
        depth = 0 if current == base else current[len(base) + 1:].count(os.sep) + 1
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            doc["truncated"] = True
        # Sorted so two walks of the same tree answer in the same order; the
        # entry list is sorted by size below anyway, but the walk's own order
        # decides which entries the time budget reaches.
        dirnames.sort()
        prefix = "" if current == base else current[len(base) + 1:] + os.sep
        top = prefix.split(os.sep)[0] if prefix else ""
        for name_ in sorted(dirnames + filenames):
            item = os.path.join(current, name_)
            try:
                item_st = os.lstat(item)
            except OSError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                if name_ in dirnames:
                    dirnames.remove(name_)
                doc["truncated"] = True
                continue
            if _is_link(item_st):
                # Not followed and not descended into; os.walk enters a
                # Windows junction unless it is pruned here.
                if name_ in dirnames:
                    dirnames.remove(name_)
                continue
            if stat.S_ISDIR(item_st.st_mode):
                doc["dirs"] += 1
            elif stat.S_ISREG(item_st.st_mode):
                doc["files"] += 1
                doc["bytes"] += item_st.st_size
                key = top or name_
                row = totals.get(key)
                if row is None:
                    row = totals[key] = {"path": key, "bytes": 0, "files": 0}
                row["bytes"] += item_st.st_size
                row["files"] += 1
            # Special files contribute nothing but are not an error here: a
            # socket in a scratch tree has no size to report.

    doc["entries"] = sorted(totals.values(), key=lambda r: (-r["bytes"], r["path"]))
    if errors:
        doc["error"] = errors[0]
        doc["truncated"] = True
    return doc


class Storage:
    """Per-session measurements, kept for :data:`TTL` seconds.

    One instance lives for the daemon's lifetime (``STORAGE`` below). The
    fields are plain dicts so the session-list worker can read a number
    without awaiting anything.
    """

    def __init__(self, ttl: float = TTL) -> None:
        self.ttl = ttl
        self._rows: dict[str, dict] = {}
        self._at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._queue: list[str] = []
        self._worker: asyncio.Task | None = None

    # -- reads -------------------------------------------------------------

    def cached(self, name: str) -> dict | None:
        return self._rows.get(name)

    def cached_bytes(self, name: str) -> int | None:
        row = self._rows.get(name)
        return None if row is None else int(row.get("bytes") or 0)

    def age(self, name: str) -> float | None:
        at = self._at.get(name)
        return None if at is None else time.monotonic() - at

    def fresh(self, name: str) -> bool:
        age = self.age(name)
        return age is not None and age <= self.ttl

    def reset(self) -> None:
        """Forget every measurement. Tests and nothing else."""
        self._rows.clear()
        self._at.clear()
        self._locks.clear()
        self._queue.clear()

    # -- measurements ------------------------------------------------------

    async def measure(self, name: str, *, refresh: bool = False) -> dict:
        """The measurement for ``name``, walking only when it is needed.

        Single-flight: a caller that arrives while another walk for the same
        session is running waits for that walk and takes its answer, so N
        simultaneous asks cost one traversal. ``refresh`` only means "do not
        accept the standing value" -- a walk that finished while this caller
        waited is still this caller's answer.
        """
        started = time.monotonic()
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        async with lock:
            row = self._rows.get(name)
            if row is not None:
                at = self._at.get(name, 0.0)
                if at >= started:
                    return row
                if not refresh and (time.monotonic() - at) <= self.ttl:
                    return row
            doc = await asyncio.to_thread(scan, name)
            self._rows[name] = doc
            self._at[name] = time.monotonic()
            return doc

    async def read(self, name: str, *, refresh: bool = False) -> dict:
        """What ``/api/sessions/{name}/storage`` answers.

        A standing measurement is returned at once with ``stale`` saying
        whether it has outlived :data:`TTL`; the walk that refreshes it is
        queued behind the answer rather than in front of it. ``refresh``
        waits for a new walk.
        """
        if refresh:
            doc = await self.measure(name, refresh=True)
            return dict(doc, stale=False)
        row = self._rows.get(name)
        if row is None:
            doc = await self.measure(name)
            return dict(doc, stale=False)
        stale = (time.monotonic() - self._at.get(name, 0.0)) > self.ttl
        if stale:
            self.prefetch([name])
        return dict(row, stale=stale)

    # -- background refresh ------------------------------------------------

    def prefetch(self, names) -> None:
        """Queue a background walk for names with no standing measurement.

        Called from the session list, which polls: it must never wait for a
        walk, and it must not queue one per poll either. A name already queued
        or already being walked is skipped, so the queue holds each session at
        most once regardless of how often the rail asks.
        """
        for name in names:
            walking = name in self._locks and self._locks[name].locked()
            if not name or name in self._queue or walking:
                continue
            if self.fresh(name):
                continue
            self._queue.append(name)
        if self._queue and (self._worker is None or self._worker.done()):
            try:
                self._worker = asyncio.create_task(self._drain())
            except RuntimeError:  # no running loop: nothing to schedule on
                self._worker = None

    async def _drain(self) -> None:
        """Walk queued sessions one at a time.

        One at a time on purpose: this is a background chore, and several
        parallel walks would each hold a thread and a slice of the disk while
        the daemon's real work waits on both.
        """
        try:
            while self._queue:
                name = self._queue.pop(0)
                try:
                    await self.measure(name)
                except Exception:  # noqa: BLE001 - one session's tree is not the daemon's
                    log.warning("storage: cannot measure %s", name, exc_info=True)
        finally:
            self._worker = None


#: The daemon's single measurement cache. Module scope so ``api`` can attach a
#: number to a session row in a worker thread without passing anything round.
STORAGE = Storage()


def attach(row: dict, name: str) -> bool:
    """Put the cached measurement on a session row; say whether one is missing.

    Returns True when the row needs a walk -- the caller collects those and
    hands them to :meth:`Storage.prefetch` once the poll's own work is done.
    Only the cached values ride the poll: this function never walks.
    """
    doc = STORAGE.cached(name)
    if doc is None:
        return True
    row["storage"] = {
        "bytes": int(doc.get("bytes") or 0),
        "files": int(doc.get("files") or 0),
        "scanned_at": doc.get("scanned_at"),
    }
    return not STORAGE.fresh(name)
