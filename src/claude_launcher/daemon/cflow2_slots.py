"""The daemon's start-time move of its old cflow2 run slots (claunch-9jmdz).

A run slot names its daemon now (``runs/s15@main``, :mod:`..cflow2.store`).
Slots written before that (``runs/s15``) are moved by the daemon that owns
them, once, when it starts -- that is, when the user restarts it. The rules
are :mod:`..cflow2.slot_migration`'s; this module gathers what they need:

- the directories to look in: every directory this daemon's sessions stand in
  (exited ones included -- their runs still need a home) and the ones its
  cflow2 clock last scanned (:data:`..cflow2.executors.HEARTBEAT_FILE`);
- every known daemon's sessions (name, created, exited), read-only from each
  instance's ``sessions.db``, so a slot of the same name on another daemon is
  told apart rather than taken.

The result is written to :data:`MARKER` in this daemon's directory. Its
presence is what "once" means: a later start reads it and moves nothing.
Delete it to run the move again (an old slot is then re-examined; one already
moved is a link and is skipped).

The default daemon's id is ``main`` (:data:`.paths.DEFAULT_INSTANCE_ID`),
which no named instance may take any more. A named instance called ``main``
that was started before that has a directory ``daemons/main``. The default
daemon removes it at this same start when it holds no session and no server
runs from it; otherwise it is left and logged -- it cannot be started again
under that name. ``daemons/default`` is a valid named instance and is not
touched.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import atomic
from ..cflow2 import executors, slot_migration
from . import paths

log = logging.getLogger(__name__)

MARKER = "cflow2-slots.json"
VERSION = 1


def marker_path() -> Path:
    return paths.daemon_dir() / MARKER


def instance_dir(name: str) -> Path:
    """The state directory of instance ``name`` ('' = the default one), seen
    from any instance."""
    if name:
        return paths.base_home() / "daemons" / name
    return paths.base_home() / "daemon"


def read_sessions(db: Path) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """``{name: (created_at, exited_at)}`` from one instance's sessions.db,
    read-only; empty when it is missing or unreadable."""
    if not db.is_file():
        return {}
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT name, data FROM sessions").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("cflow2 slots: cannot read %s: %s", db, exc)
        return {}
    for name, data in rows:
        try:
            rec = json.loads(data)
        except (TypeError, ValueError):
            rec = {}
        out[str(name)] = (rec.get("created_at"), rec.get("exited_at"))
    return out


def known_sessions() -> Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]:
    """Every known daemon's sessions, keyed by daemon id."""
    return {paths.instance_id(name): read_sessions(instance_dir(name) / "sessions.db")
            for name in paths.known_instances()}


def directories(manager: Any) -> List[str]:
    """Where this daemon's runs can be: its sessions' directories (all of
    them) and the ones its cflow2 clock last scanned."""
    out = []
    try:
        sessions = list(manager.list())
    except Exception:  # noqa: BLE001 - a manager without a listing
        sessions = []
    for session in sessions:
        cwd = str(getattr(getattr(session, "sdef", None), "cwd", "") or "")
        if cwd:
            out.append(cwd)
    try:
        beat = json.loads(executors.heartbeat_path().read_text(encoding="utf-8"))
        out.extend(str(c) for c in beat.get("cwds") or [])
    except (OSError, ValueError, AttributeError):
        pass
    return list(dict.fromkeys(out))


def clear_reserved() -> str:
    """Remove ``daemons/main`` (a named instance the reserved id used to
    allow) when it is empty of sessions and no server runs from it. Only the
    default daemon calls this. Returns what happened, for the marker."""
    d = instance_dir(paths.DEFAULT_INSTANCE_ID)
    if not d.is_dir():
        return "absent"
    if read_sessions(d / "sessions.db"):
        log.warning("daemons/%s holds sessions; left in place -- that name is the default "
                    "daemon's id and cannot be started as a named instance",
                    paths.DEFAULT_INSTANCE_ID)
        return "kept: holds sessions"
    try:
        pid = int(json.loads((d / "daemon.json").read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        pid = 0
    if pid:
        from .window import pid_alive

        if pid_alive(pid):
            return f"kept: a server runs from it (pid {pid})"
    shutil.rmtree(d, ignore_errors=True)
    return "removed" if not d.exists() else "kept: could not remove"


def run_at_start(manager: Any) -> Optional[Dict[str, Any]]:
    """Move this daemon's old slots once (no-op once :data:`MARKER` exists).
    Returns the report written, or None when it had already run."""
    marker = marker_path()
    if marker.exists():
        return None
    me = paths.instance_id()
    report = slot_migration.migrate(directories(manager), me, known_sessions(), apply=True)
    doc: Dict[str, Any] = {"version": VERSION, "at": time.time(), **report.as_dict()}
    if not paths.instance():
        doc["reserved"] = clear_reserved()
    marker.parent.mkdir(parents=True, exist_ok=True)
    with atomic.scratch(marker) as tmp:
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        atomic.replace(tmp, marker)
    log.info("cflow2 slots: %d moved to @%s, %d left in place, %d another daemon's (%s)",
             len(doc["moved"]), me, len(doc["left"]), len(doc["other"]), marker)
    for entry in doc["left"]:
        log.warning("cflow2 slot %s/%s left in place: %s",
                    entry.get("cwd"), entry.get("slot"), entry.get("why"))
    return doc
