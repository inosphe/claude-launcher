"""Bounded mechanical event history, independent of observer/model state.

Only completed control operations are recorded. Kill/pause describe the stop
request; exit describes process termination. Names reused after removal get a
new history through the session's creation timestamp. No credentials or full
session definitions belong in this store.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from .. import atomic

log = logging.getLogger(__name__)
LIMIT = 200


def timestamp(value):
    """Compare ISO timestamps by instant, including differing UTC offsets."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


class Events:
    """Event rows per session, cached in memory and mirrored to one file each.

    **Two threads reach this in the daemon, and the first write to a session is
    where they collide** (claunch-tn2wo). The loop records control events
    through :mod:`manager`, while ``spawn_events`` reports a refused session
    creation from the ``pools.background`` worker (``cflow2_exec.spawn_session``
    and ``_spawn_fork``). Both call :meth:`record` with the same session.

    The hazard is the cache miss, not the append. :meth:`rows` reads the file
    and installs a *new* list when the key is absent; two threads missing at
    once each read the file, each get their own list, and whichever writes
    second writes a list that does not contain the other's row -- so the
    earlier event is dropped from the file. Once both hold the same cached
    list, ``list.append`` is atomic under the GIL and needs no lock of its own.

    ``_lock`` therefore spans **the miss and the write together**. The miss is
    what the test below pins: with the lock replaced by a no-op the second
    writer's file drops the first writer's row, and with the lock back both
    survive. Holding it across the write as well is for a second, untested
    hazard -- ``json.dumps(rows)`` walks the list, so an append from another
    thread during the dump would serialize a list that is changing underneath
    it. That one is not reachable deterministically from here, so no test
    claims it: guarding *only* the lookup leaves the tests green, and saying
    otherwise would be reading more into them than they measure. It is a
    reentrant lock because :meth:`record` reads through :meth:`rows` -- the
    same thread takes it twice, and a plain ``Lock`` would deadlock there.

    The lock **ends before the index mirror** (claunch-tsmqd).
    :func:`search_records.remember` opens SQLite and commits, and that is the
    dominant cost of a ``record`` call -- on this host the index write alone
    measured 21 ms against 43 ms for the whole call, and 230 ms against 227 ms
    when the write was slowed to 200 ms. :meth:`rows` is what the event loop
    calls for its observer snapshot, so holding the lock across that write
    parks the loop behind a worker thread's disk I/O. By the time the index
    write starts the row is already in the cache and in the file, so a reader
    sees it immediately.

    Losing an index write costs more than "the next event re-delivers it": the
    next :meth:`record` for that session sends only its *own* row, so a dropped
    row stays out of the index until something rewrites it. That is why the
    index guard logs (``could not index session event``) rather than passing
    silently, and why the guard is narrower than the row's: the row is the
    record of what happened, the index is a searchable copy of it.

    ``rows`` still returns the cached list itself. Callers read it; one that
    appended to it outside this class would bypass both the bound and the
    write, which is why :meth:`record` is the only supported writer.
    """

    def __init__(self, root):
        self.root = root / "session-events"
        self.cache = {}
        self._lock = threading.RLock()

    def _key(self, session):
        return (session.sdef.name, getattr(session, "created_at", None))

    def _path(self, key):
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return self.root / (digest + ".json")

    def rows(self, session):
        key = self._key(session)
        with self._lock:
            if key not in self.cache:
                try:
                    rows = json.loads(self._path(key).read_text(encoding="utf-8"))
                    if not isinstance(rows, list):
                        raise ValueError("invalid event history")
                    self.cache[key] = [r for r in rows if isinstance(r, dict)][-LIMIT:]
                except (OSError, ValueError):
                    self.cache[key] = []
            return self.cache[key]

    def record(self, session, action, text, **details):
        event = {"id": uuid.uuid4().hex, "origin": "daemon", "kind": action,
                 "text": text, "at": datetime.now(timezone.utc).isoformat(),
                 "needs_action": False, "source": "session:" + session.sdef.name,
                 "details": details}
        with self._lock:
            rows = self.rows(session)
            rows.append(event)
            del rows[:-LIMIT]
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                path = self._path(self._key(session))
                with atomic.scratch(path) as tmp:
                    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                    atomic.replace(tmp, path)
            except OSError:
                # A successful control operation must not appear to fail because
                # its history could not be persisted (which would invite retries).
                log.exception("could not persist session event for %s", session.sdef.name)
        # Mirrored outside the lock: the row is in the cache and the file by now,
        # and this call is the slow part (SQLite open + commit). The event loop
        # reads through ``rows``, which takes the same lock, so doing this inside
        # would park the loop behind this worker's disk write (claunch-tsmqd).
        try:
            from . import search_records
            search_records.remember(session.sdef.name, [event])
        except OSError:
            # The index is a mirror, so losing one write costs only an entry
            # until the next event for this session re-delivers it -- and it
            # must not turn a completed control operation into a failure.
            log.exception("could not index session event for %s", session.sdef.name)
        return event
