"""SQLite-backed session registry — the durable store behind restore-on-restart.

This replaces the single ``sessions.json`` file that :meth:`SessionManager.persist`
rewrote whole on every registry change. That file was the one daemon record
written without the atomic rename every other record already uses (see
:mod:`claude_launcher.daemon.atomic`): a plain ``write_text`` truncates the file
and then writes the new bytes, so a reader — or a second daemon that overlapped
the first because a restart raced the port — could see the file mid-write, and a
crash or an empty in-memory set in that window left it ``[]``. The whole fleet,
gone with one truncating write. That is exactly how the 2026-09-07 loss happened.

A SQLite database closes that window. Every write is a transaction, so a reader
sees the whole old state or the whole new one and never a torn file; WAL keeps
the committed rows durable across a crash; and a change touches one row instead
of rewriting the lot. Two daemons that briefly overlap serialize on the database
lock rather than clobbering a shared file.

The record shape is unchanged. Each session's :meth:`persist` entry — its
``def`` plus the lifecycle fields (``was_running``, ``paused_at``,
``exited_at`` …) — is stored as a JSON blob in one row keyed by the session
name, so :meth:`SessionDef.from_dict` and the restore path read exactly what
they read out of the JSON file before. Only the container changed.

The one behaviour that is *new* is the empty-snapshot guard in :meth:`save`: a
prune that would delete every row for an empty write is refused unless the caller
says an explicit clear asked for it. A session only leaves the manager's set on a
user action (``remove``/``clear``) or while a restore is still filling it; a
persist that finds the set unexpectedly empty is a bug, not an instruction to
erase the fleet, and this store will not act on it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional

from . import paths

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    name       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

#: Marker in ``meta`` recording that the one-time ``sessions.json`` import has
#: run. Set the first time a store is opened, whether or not there was a file to
#: import, so a legacy file left on disk is never read a second time — not even
#: if the table is later emptied by a genuine clear.
_MIGRATED_KEY = "migrated_sessions_json"


class SessionStore:
    """The durable session registry, one row per session keyed by name."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- connection ---------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per operation: writes are a few per second at most,
        # WAL keeps committed rows visible across connections, and per-op open
        # sidesteps sharing one sqlite3 connection across the event loop's
        # callbacks. ``busy_timeout`` is what makes two overlapping daemons wait
        # for each other's write instead of raising.
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # -- reads --------------------------------------------------------- #

    def load_all(self) -> List[dict]:
        """Every session record, oldest row first (insertion order)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT data FROM sessions ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
        out: List[dict] = []
        for (data,) in rows:
            try:
                entry = json.loads(data)
            except (ValueError, TypeError):
                log.warning("skipping an unreadable session row")
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        finally:
            conn.close()

    # -- writes -------------------------------------------------------- #

    def save(
        self,
        entries: List[dict],
        *,
        prune: bool = True,
        allow_empty: bool = False,
    ) -> None:
        """Write the current fleet in one transaction.

        Upserts every entry, then — when ``prune`` — deletes the rows for
        sessions no longer present, so a forgotten session's row goes with it.

        The prune is refused, and a warning logged, when ``entries`` is empty
        while the table is not, unless ``allow_empty`` says an explicit clear
        asked for it. That is the guard against the empty-snapshot clobber:
        upserts still land, but nothing is erased on an empty write nobody asked
        for. ``prune`` itself is passed False while a restore is still filling
        the set (each per-session persist would otherwise delete the records not
        loaded yet) and left True in steady state.
        """
        rows = [(self._name_of(e), e) for e in entries]
        rows = [(n, e) for (n, e) in rows if n]
        names = {n for (n, _e) in rows}
        conn = self._connect()
        try:
            existing = int(
                conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            )
            if prune and not names and existing and not allow_empty:
                log.warning(
                    "refusing to clear %d session record(s) for an empty "
                    "snapshot nobody asked for; keeping them",
                    existing,
                )
                prune = False
            for name, entry in rows:
                blob = json.dumps(entry, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO sessions(name, data, updated_at) "
                    "VALUES(?,?,datetime('now')) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "data=excluded.data, updated_at=excluded.updated_at",
                    (name, blob),
                )
            if prune:
                have = [r[0] for r in conn.execute("SELECT name FROM sessions")]
                for n in have:
                    if n not in names:
                        conn.execute("DELETE FROM sessions WHERE name=?", (n,))
            conn.commit()
        finally:
            conn.close()

    def delete(self, names: Iterable[str]) -> None:
        """Remove the named rows (a no-op for names not present)."""
        conn = self._connect()
        try:
            for name in names:
                conn.execute("DELETE FROM sessions WHERE name=?", (name,))
            conn.commit()
        finally:
            conn.close()

    # -- migration ----------------------------------------------------- #

    def migrate_from_json(self, json_path: Path) -> int:
        """Import a legacy ``sessions.json`` once, into an empty store.

        Returns the number of records imported (0 when there is nothing to do).
        Guarded by a marker in ``meta`` so it runs at most once for the life of
        the database: a legacy file left on disk is never read again, not even
        after a genuine clear empties the table. The file is left in place as a
        backup; the marker, not its absence, is what stops the re-read.
        """
        if self._meta_get(_MIGRATED_KEY):
            return 0
        imported = 0
        if self.count() == 0 and Path(json_path).is_file():
            try:
                entries = json.loads(Path(json_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                entries = None
            if isinstance(entries, list) and entries:
                self.save(entries, prune=False)
                imported = len(entries)
        self._meta_set(_MIGRATED_KEY, "1")
        return imported

    # -- meta ---------------------------------------------------------- #

    def _meta_get(self, key: str) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    # -- helpers ------------------------------------------------------- #

    @staticmethod
    def _name_of(entry: dict) -> Optional[str]:
        if not isinstance(entry, dict):
            return None
        sdef = entry.get("def")
        if isinstance(sdef, dict):
            name = sdef.get("name")
            return str(name) if name else None
        return None


def open_default() -> SessionStore:
    """The store at this daemon instance's :func:`paths.sessions_db`."""
    return SessionStore(paths.sessions_db())
