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
    def __init__(self, root):
        self.root = root / "session-events"
        self.cache = {}

    def _key(self, session):
        return (session.sdef.name, getattr(session, "created_at", None))

    def _path(self, key):
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        return self.root / (digest + ".json")

    def rows(self, session):
        key = self._key(session)
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
