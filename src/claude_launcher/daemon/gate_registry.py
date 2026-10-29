"""The daemon's record of the cflow v2 gates that wait on a person
(claunch-6vq2m.1).

A gate is one thing only a person releases in a v2 run: a ``user`` choice,
an ask that fell to a human, a loop or visit limit, a halt, an acceptance
hold, the driver's move request -- the items :func:`.cflow2_web._rail_answers`
lists. Before this module the dashboard recomputed them from every registered
session's run files on each rail poll and kept the result in memory only, so
a gate lived exactly as long as its run file said so: an archived session's
run left ``waiting_approval`` raised its card forever, and nothing on disk
said when a gate opened or why it closed.

Here every gate is a row in ``<daemon dir>/gates.db`` that is opened once and
closed once:

* **opened** when the run's state first lists it (``opened_at`` is the run's
  own stamp when it carries one, else the moment this registry saw it);
* **closed** with a reason -- ``resolved`` (answered, or the run moved past
  it), ``run ended`` (the run finished, aborted, was archived or another run
  took its slot), ``session archived`` / ``session removed`` (the session's
  record was filed away or dropped).

The daemon is the only writer. The run's ``state.json`` stays the source of
what is owed -- the runtime writes it from the agent's MCP server, the CLI and
the daemon alike -- and the daemon compares (:meth:`GateRegistry.sync`) only
when a slot's files changed (``cflow2_web.rail_entries`` recomputes an entry
on a new stamp) or a session's lifecycle closes its gates
(:meth:`GateRegistry.close_session`). A closed gate that the run lists again
(a respawned archived session whose run still waits) opens as a new row; the
closed one stays as history.

One table plus ``meta``. Connections are per operation, WAL with a busy
timeout, as in :mod:`.db`: writes are a few per minute and come from the
event loop's worker threads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths

log = logging.getLogger(__name__)

#: Why a gate closed.
CLOSE_RESOLVED = "resolved"
CLOSE_RUN_ENDED = "run ended"
CLOSE_ARCHIVED = "session archived"
CLOSE_REMOVED = "session removed"

#: ``meta`` keys of the one-time backfill (:func:`backfill`).
META_BACKFILLED_AT = "backfilled_at"
META_BACKFILLED = "backfilled_count"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gates (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    session      TEXT NOT NULL,
    cwd          TEXT NOT NULL,
    scope        TEXT NOT NULL,
    run_id       TEXT,
    workflow     TEXT,
    kind         TEXT NOT NULL,
    item         TEXT NOT NULL,
    label        TEXT,
    card         TEXT NOT NULL,
    opened_at    REAL NOT NULL,
    recorded_at  REAL NOT NULL,
    closed_at    REAL,
    close_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS gates_open_key ON gates(key) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS gates_session ON gates(session, closed_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = ("seq", "key", "session", "cwd", "scope", "run_id", "workflow", "kind",
            "item", "label", "card", "opened_at", "recorded_at", "closed_at",
            "close_reason")


def gate_item(answer: Dict[str, Any]) -> str:
    """What names one gate within its run: the move request's id, the ask's
    id, the instance of a choice or a limit, the monitor of a halt."""
    for field in ("request", "ask", "instance"):
        value = answer.get(field)
        if value:
            return str(value)
    return str(answer.get("label") or answer.get("kind") or "")


def gate_key(session: str, run_id: Optional[str], answer: Dict[str, Any]) -> str:
    return f"{session}:{run_id or ''}:{answer.get('kind') or ''}:{gate_item(answer)}"


class GateRegistry:
    """``gates.db``: open and closed gates, one row per opening."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    # -- writes ---------------------------------------------------------- #

    def sync(self, session: str, cwd: str, scope: str, entry: Optional[Dict[str, Any]],
             *, now: Optional[float] = None) -> Tuple[int, int]:
        """Make the session's open gates what its run's rail ``entry`` lists
        now: open the ones not recorded, close the recorded ones it no longer
        lists. ``entry`` None is a slot with no run (archived, never started).
        Returns ``(opened, closed)``."""
        from ..cflow2 import runtime

        at = time.time() if now is None else float(now)
        entry = entry or {}
        run_id = entry.get("run")
        live = entry.get("status") in runtime.LIVE_STATUSES
        answers = [a for a in entry.get("answers") or [] if isinstance(a, dict)] if live else []
        wanted = {gate_key(session, run_id, a): a for a in answers}
        opened = closed = 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT seq, key, run_id FROM gates WHERE session = ? AND closed_at IS NULL",
                    (session,)).fetchall()
                have = {r["key"] for r in rows}
                for r in rows:
                    if r["key"] in wanted:
                        continue
                    reason = (CLOSE_RESOLVED if live and r["run_id"] == run_id
                              else CLOSE_RUN_ENDED)
                    conn.execute("UPDATE gates SET closed_at = ?, close_reason = ? WHERE seq = ?",
                                 (at, reason, r["seq"]))
                    closed += 1
                for key, a in wanted.items():
                    if key in have:
                        continue
                    stamp = a.get("opened_at")
                    opened_at = (float(stamp) if isinstance(stamp, (int, float))
                                 and not isinstance(stamp, bool) else at)
                    conn.execute(
                        "INSERT INTO gates (key, session, cwd, scope, run_id, workflow, kind, "
                        "item, label, card, opened_at, recorded_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, session, str(entry.get("cwd") or cwd),
                         str(entry.get("scope") or scope), run_id, entry.get("workflow"),
                         str(a.get("kind") or ""), gate_item(a),
                         str(a.get("label") or "") or None,
                         json.dumps(a, ensure_ascii=False, default=str), opened_at, at))
                    opened += 1
                conn.commit()
            finally:
                conn.close()
        if opened or closed:
            log.info("gate registry: %s opened %d, closed %d", session, opened, closed)
        return opened, closed

    def close_session(self, session: str, reason: str, *,
                      now: Optional[float] = None) -> int:
        """Close every open gate of ``session`` with ``reason``. Returns how many."""
        at = time.time() if now is None else float(now)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE gates SET closed_at = ?, close_reason = ? "
                    "WHERE session = ? AND closed_at IS NULL", (at, reason, session))
                conn.commit()
                n = cur.rowcount
            finally:
                conn.close()
        if n:
            log.info("gate registry: %s closed %d (%s)", session, n, reason)
        return n

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (key, json.dumps(value)))
                conn.commit()
            finally:
                conn.close()

    # -- reads ----------------------------------------------------------- #

    def meta(self, key: str) -> Any:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return None if row is None else json.loads(row["value"])

    def open_sessions(self) -> Dict[str, int]:
        """``{session: open gate count}``."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT session, COUNT(*) AS n FROM gates "
                                "WHERE closed_at IS NULL GROUP BY session").fetchall()
        finally:
            conn.close()
        return {r["session"]: r["n"] for r in rows}

    def open_gates(self) -> Dict[str, Dict[str, Any]]:
        """``{key: row}`` of every open gate."""
        return {r["key"]: r for r in self.rows(state="open", limit=None)}

    def rows(self, *, state: str = "open", session: Optional[str] = None,
             limit: Optional[int] = 500) -> List[Dict[str, Any]]:
        """Gates newest first: ``state`` ``open``, ``closed`` or ``all``."""
        where, args = [], []  # type: List[str], List[Any]
        if state == "open":
            where.append("closed_at IS NULL")
        elif state == "closed":
            where.append("closed_at IS NOT NULL")
        if session:
            where.append("session = ?")
            args.append(session)
        sql = "SELECT * FROM gates"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        conn = self._connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            row = {c: r[c] for c in _COLUMNS}
            try:
                row["card"] = json.loads(row["card"])
            except (TypeError, ValueError):
                row["card"] = {}
            out.append(row)
        return out


#: The registry this daemon writes (:func:`install`); None outside a daemon
#: (a test building no app, the CLI), where reading the rail records nothing.
_ACTIVE: Optional[GateRegistry] = None


def install(registry: Optional[GateRegistry]) -> None:
    """Make ``registry`` the one every rail read compares against
    (``cflow2_web.rail_entries``). One per process: the rail's entry cache is
    process-wide, so a read that recomputed an entry without comparing would
    hide that change from the registry for good."""
    global _ACTIVE
    _ACTIVE = registry


def active() -> Optional[GateRegistry]:
    return _ACTIVE


def default_path() -> Path:
    """This daemon instance's ``gates.db``."""
    return paths.daemon_dir() / "gates.db"


def annotate(rows: Dict[str, Dict[str, Any]], registry: GateRegistry) -> None:
    """Stamp each rail answer with its ``gate_id`` (the registry row's
    ``seq``) and, where the run carries no stamp, the registry's
    ``opened_at`` -- the moment the daemon first recorded the gate, which
    outlives a browser's own first sight."""
    gates = registry.open_gates()
    if not gates:
        return
    for session, row in rows.items():
        answers = row.get("answers")
        if not isinstance(answers, list):
            continue
        out = []
        for a in answers:
            g = gates.get(gate_key(session, row.get("run"), a)) if isinstance(a, dict) else None
            if g is not None:
                a = dict(a)
                a["gate_id"] = g["seq"]
                a.setdefault("opened_at", g["opened_at"])
            out.append(a)
        row["answers"] = out


def backfill(registry: GateRegistry, sessions: Iterable[Any], *,
             now: Optional[float] = None, force: bool = False) -> Optional[Dict[str, Any]]:
    """Record, once, the gates that were already open when the registry came
    in: every registered session that is not archived, its run read as the
    rail reads it. Sessions the registry holds open gates for but that are
    archived or no longer registered are closed. Idempotent -- a second run
    opens nothing new -- and done once per registry (``meta``) unless
    ``force``. Returns the counts, or None when it had been done."""
    from . import cflow2_web
    from .session import CATEGORY_ARCHIVED, session_category

    if not force and registry.meta(META_BACKFILLED_AT) is not None:
        return None
    at = time.time() if now is None else float(now)
    pairs: List[Tuple[str, str]] = []
    archived: List[str] = []
    known = set()
    for session in sessions:
        sdef = getattr(session, "sdef", None)
        name = str(getattr(sdef, "name", "") or "")
        if not name:
            continue
        known.add(name)
        if session_category(session) == CATEGORY_ARCHIVED:
            archived.append(name)
            continue
        cwd = str(getattr(sdef, "cwd", "") or "")
        if cwd:
            pairs.append((name, cwd))
    before = sum(registry.open_sessions().values())
    rows = cflow2_web.rail_entries(pairs, registry=registry, force=True, now=at)
    closed = 0
    for name in archived:
        closed += registry.close_session(name, CLOSE_ARCHIVED, now=at)
    for name in set(registry.open_sessions()) - known:
        closed += registry.close_session(name, CLOSE_REMOVED, now=at)
    open_now = registry.open_sessions()
    result = {
        "at": at,
        "sessions_read": len(pairs),
        "runs": len(rows),
        "open_before": before,
        "open": sum(open_now.values()),
        "sessions_with_open_gates": len(open_now),
        "closed": closed,
    }
    registry.set_meta(META_BACKFILLED_AT, at)
    registry.set_meta(META_BACKFILLED, result)
    log.info("gate registry backfill: %s", result)
    return result


def lifecycle_closer(registry: GateRegistry):
    """The manager's ``filed_hooks`` callback: an archived or removed
    session's open gates close with it."""

    def closed(session: Any, how: str) -> None:
        name = str(getattr(getattr(session, "sdef", None), "name", "") or "")
        if not name:
            return
        from . import cflow2_web

        reason = CLOSE_REMOVED if how == "removed" else CLOSE_ARCHIVED
        try:
            registry.close_session(name, reason)
            # A respawned record reads its run afresh and reopens what is
            # still owed.
            cflow2_web.forget_rail(name)
        except Exception:  # noqa: BLE001 - a hook never breaks the archive
            log.exception("gate registry could not close the gates of %r", name)

    return closed


def sync_pairs(registry: GateRegistry, pairs: Sequence[Tuple[str, str]]) -> None:
    """Compare the given ``(session, cwd)`` slots against the registry, each
    only when its files changed since this process last read them."""
    from . import cflow2_web

    cflow2_web.rail_entries(pairs, registry=registry)
