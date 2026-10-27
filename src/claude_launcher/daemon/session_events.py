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
                from . import search_records
                search_records.remember(session.sdef.name, [event])
            except OSError:
                # A successful control operation must not appear to fail because
                # its history could not be persisted (which would invite retries).
                log.exception("could not persist session event for %s", session.sdef.name)
        return event
