"""Reclaim a session's pytest basetemps once the session has ended.

The gate tools root every run's ``--basetemp`` under one directory
(:mod:`claude_launcher.pytest_basetemp`, ``C:/t`` on Windows) and name it
after the session that ran it:

* ``<session>c<MMDDHHMMSS>`` -- ``tools/changed_tests.py`` (one per run),
* ``<session>w`` -- ``tools/sweep.py``,
* ``<session>-<anything>`` -- a basetemp a session pinned by hand.

``changed_tests.prune_basetemps`` keeps the newest eight of a session's own
while the session is still running. Nothing reclaimed them after it ended,
and a fleet of sessions each leaving eight 200MB trees behind filled C: to
264MB free (claunch-4zc51). This module is that reclaim: the manager's exit
funnel calls :meth:`BasetempReaper.session_exited`, and the boot calls
:meth:`BasetempReaper.reap_ended` once for the sessions that ended while no
daemon was there to see it.

Before a directory goes, one line describing it is appended to
``basetemp-history.jsonl`` in the daemon directory: which session, when the
run started and ended (from file mtimes), how many xdist workers it used
(``popen-gw*``), its size, and -- when the run left one -- the
``claunch-run.json`` summary ``changed_tests`` writes (tree, exit code,
counts). Lines older than :data:`HISTORY_RETENTION_DAYS` are dropped.

Ownership is decided by name, and names nest: ``s99`` is a prefix of
``s995``, and ``s995-*`` also matches ``s995-rev2c1011...``. A directory
therefore belongs to the *longest* known session name that matches it, and a
session reaps only what belongs to it -- never what belongs to a session
that is still running.

A daemon shutdown is not an ending: the manager does not run exit hooks for
it, and :meth:`reap_ended` skips every session that is still running or held,
so a restart deletes nothing a live session is using.

Names are only unique within one daemon instance -- each named instance
(``<base home>/daemons/<name>``) hands out its own ``sN`` -- while the
Windows root is one ``C:/t`` for the whole machine. So before anything goes,
:func:`foreign_live` reads every *other* instance's registry, and a name that
is running or held there is skipped: an ``s1`` that ended here must not take
the trees of the ``s1`` still running under ``local2``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .. import pytest_basetemp
from . import paths

log = logging.getLogger(__name__)

HISTORY_NAME = "basetemp-history.jsonl"
HISTORY_RETENTION_DAYS = 30
HISTORY_RETENTION = HISTORY_RETENTION_DAYS * 86400.0

#: The summary ``tools/changed_tests.py`` leaves in a basetemp it ran in.
RUN_SUMMARY = "claunch-run.json"

_STAMP = re.compile(r"c\d{10}")


def default_root() -> Path:
    """The directory reaped when none is given: the gate tools' root."""
    return pytest_basetemp.root()


def matches(name: str, session: str) -> bool:
    """Whether directory ``name`` has one of ``session``'s basetemp shapes."""
    if not session or not name.startswith(session):
        return False
    rest = name[len(session):]
    return rest == "w" or bool(_STAMP.fullmatch(rest)) or (
        rest.startswith("-") and len(rest) > 1
    )


def owner(name: str, sessions: Iterable[str]) -> Optional[str]:
    """The longest of ``sessions`` whose shape ``name`` has, or ``None``."""
    best: Optional[str] = None
    for session in sessions:
        if matches(name, session) and (best is None or len(session) > len(best)):
            best = session
    return best


def _live(entry: dict) -> Optional[str]:
    """The name of a registry record that is running or held, else ``None``."""
    sdef = entry.get("def") if isinstance(entry.get("def"), dict) else {}
    name = sdef.get("name")
    if name and (entry.get("was_running") or entry.get("held_at")):
        return str(name)
    return None


def _registry(directory: Path) -> List[dict]:
    """The session records an instance directory holds, read-only.

    ``sessions.db`` when there is one (opened ``mode=ro`` so a reader never
    creates or migrates another daemon's database), else the legacy
    ``sessions.json`` an instance that predates the database still has.
    """
    db = directory / "sessions.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                rows = conn.execute("SELECT data FROM sessions").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        out = []
        for (data,) in rows:
            try:
                entry = json.loads(data)
            except (ValueError, TypeError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
        return out
    try:
        entries = json.loads((directory / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def foreign_live() -> set:
    """Names running or held in any daemon instance other than this one."""
    own = paths.daemon_dir().resolve()
    base = paths.base_home()
    dirs = [base / "daemon"]
    try:
        dirs += [p for p in (base / "daemons").iterdir() if p.is_dir()]
    except OSError:
        pass
    names: set = set()
    for directory in dirs:
        try:
            if not directory.is_dir() or directory.resolve() == own:
                continue
        except OSError:
            continue
        names.update(n for n in map(_live, _registry(directory)) if n)
    return names


def describe(path: Path) -> dict:
    """Start, end, worker count, file count and size of one basetemp."""
    times: List[float] = []
    files = size = 0
    workers = set()
    for root, dirs, names in os.walk(path):
        workers.update(d for d in dirs if d.startswith("popen-gw"))
        for name in names:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            times.append(st.st_mtime)
            files += 1
            size += st.st_size
    if not times:
        try:
            times = [path.stat().st_mtime]
        except OSError:
            times = []
    row: dict = {"workers": len(workers), "files": files, "bytes": size}
    if times:
        row["started_at"] = _iso(min(times))
        row["ended_at"] = _iso(max(times))
        row["seconds"] = round(max(times) - min(times), 1)
    try:
        summary = json.loads((path / RUN_SUMMARY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if isinstance(summary, dict):
        row["run"] = summary
    return row


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class BasetempReaper:
    """Deletes ended sessions' basetemps and keeps one line per deletion.

    ``sessions`` returns ``(name, ended)`` for every session the registry
    knows. ``background=False`` runs deletions inline (tests); the daemon
    runs them on a thread, because a 25,000-file tree takes seconds to
    remove and the exit funnel is called on the event loop.
    """

    def __init__(
        self,
        sessions: Callable[[], List[tuple]],
        *,
        root: Optional[Path] = None,
        history: Optional[Path] = None,
        background: bool = True,
        foreign: Callable[[], set] = foreign_live,
    ) -> None:
        self._sessions = sessions
        self._foreign = foreign
        self._root = root
        self._history = history
        self._background = background
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_root()

    @property
    def history_path(self) -> Path:
        return self._history if self._history is not None else paths.daemon_dir() / HISTORY_NAME

    # ---- what belongs to whom -------------------------------------------- #

    def owned(self, session: str) -> List[Path]:
        """``session``'s basetemps under the root, attributed by longest name."""
        names = [name for name, _ in self._sessions()]
        if session not in names:
            names.append(session)
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return []
        return sorted(
            p for p in entries
            if p.is_dir() and owner(p.name, names) == session
        )

    # ---- the two entry points -------------------------------------------- #

    def session_exited(self, session) -> None:
        """The manager's exit hook: reap that session's basetemps."""
        self._submit([session.sdef.name])

    def reap_ended(self) -> None:
        """At boot: reap every session the registry holds as ended."""
        self._submit([name for name, ended in self._sessions() if ended])

    def _submit(self, names: List[str]) -> None:
        if not names:
            return
        if not self._background:
            self.reap(names)
            return
        threading.Thread(
            target=self.reap, args=(names,), name="basetemp-reaper", daemon=True
        ).start()

    # ---- the work --------------------------------------------------------- #

    def reap(self, names: List[str]) -> List[dict]:
        """Delete each named session's basetemps; return the rows written."""
        rows: List[dict] = []
        with self._lock:
            elsewhere = self._foreign()
            for name in names:
                if name in elsewhere:
                    log.info("basetemp: %r is live in another daemon instance; kept", name)
                    continue
                for path in self.owned(name):
                    rows.append(self._reap_one(name, path))
            if rows:
                self._append(rows)
        return rows

    def _reap_one(self, session: str, path: Path) -> dict:
        row = {"session": session, "path": path.as_posix(), **describe(path)}
        row["reaped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        failed: List[str] = []
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=lambda _f, p, _e: failed.append(p))
        else:
            shutil.rmtree(path, onerror=lambda _f, p, _e: failed.append(p))
        if path.exists():
            # A file another process still holds open (Windows refuses to
            # delete it). Left for the next ending or boot, and said so.
            row["outcome"] = "locked"
            row["locked"] = [Path(p).as_posix() for p in failed[:5]]
            log.warning("basetemp %s: %d entries could not be removed", path, len(failed))
        else:
            row["outcome"] = "removed"
        return row

    def _append(self, rows: List[dict]) -> None:
        cutoff = time.time() - HISTORY_RETENTION
        dest = self.history_path
        try:
            kept = [
                line for line in dest.read_text(encoding="utf-8").splitlines()
                if _fresh(line, cutoff)
            ]
        except OSError:
            kept = []
        kept += [json.dumps(r, ensure_ascii=False) for r in rows]
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            os.replace(tmp, dest)
        except OSError:
            log.exception("basetemp history: could not write %s", dest)


def _fresh(line: str, cutoff: float) -> bool:
    try:
        stamp = json.loads(line).get("reaped_at")
        return datetime.fromisoformat(stamp).timestamp() >= cutoff
    except (ValueError, TypeError, AttributeError):
        return False


def registry_view(manager) -> Callable[[], List[tuple]]:
    """``(name, ended)`` for every session in ``manager``.

    A held session's program has ended but the session has not
    (``Session.held_at``), so it reads as running.
    """
    def view() -> List[tuple]:
        out = []
        for session in manager.list():
            ended = bool(getattr(session, "exited", False)) and not getattr(
                session, "held_at", None
            ) and not getattr(session, "ended_by_os", False)
            out.append((session.sdef.name, ended))
        return out

    return view
