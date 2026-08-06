"""Session registry: create/kill/list plus definition persistence and restore.

Sessions die with the daemon (the tmux model), but their *definitions* are
persisted to ``sessions.json`` so a restarting daemon can relaunch the ones
marked ``restore`` — the claude harness comes back with ``--continue`` and
recovers its conversation.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from . import harness as harness_mod
from . import paths
from .harness import SessionDef
from .session import Session

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ManagerError(Exception):
    """Raised for bad session names, duplicates, or unknown sessions."""


class SessionManager:
    def __init__(self, *, idle_threshold: float, scrollback: int, restore_default: bool) -> None:
        self.idle_threshold = idle_threshold
        self.scrollback = scrollback
        self.restore_default = restore_default
        self._sessions: Dict[str, Session] = {}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def create(self, sdef: SessionDef, *, restoring: bool = False) -> Session:
        name = (sdef.name or "").strip() or self._auto_name()
        if not _NAME_RE.match(name):
            raise ManagerError(
                f"invalid session name {name!r}: use letters, digits, '.', '_' or '-'"
            )
        if name in self._sessions:
            raise ManagerError(f"session {name!r} already exists")
        sdef = harness_mod.normalize(
            SessionDef.from_dict({**sdef.to_dict(), "name": name})
        )
        argv, env, cwd = harness_mod.build_command(sdef, restoring=restoring)
        session = Session(
            sdef,
            argv,
            env,
            cwd,
            idle_threshold=self.idle_threshold,
            scrollback=self.scrollback,
        )
        self._sessions[name] = session
        self.persist()
        return session

    def _auto_name(self) -> str:
        i = 0
        while f"s{i}" in self._sessions:
            i += 1
        return f"s{i}"

    def get(self, name: str) -> Session:
        try:
            return self._sessions[name]
        except KeyError:
            raise ManagerError(f"no session named {name!r}") from None

    def list(self) -> List[Session]:
        return [self._sessions[k] for k in sorted(self._sessions)]

    def kill(self, name: str, *, force: bool = False) -> Session:
        """Kill a running session; deregister an already-exited one."""
        session = self.get(name)
        if session.exited:
            del self._sessions[name]
        else:
            session.kill(force=force)
        self.persist()
        return session

    async def shutdown_all(self) -> None:
        self.persist()  # record which sessions were alive, for restore
        for session in list(self._sessions.values()):
            await session.shutdown()

    # ------------------------------------------------------------------ #
    # persistence / restore
    # ------------------------------------------------------------------ #
    def persist(self) -> None:
        entries = []
        for session in self._sessions.values():
            entries.append(
                {
                    "def": session.sdef.to_dict(),
                    "was_running": not session.exited,
                    "exit_code": session.exit_code,
                }
            )
        path = paths.sessions_json()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            pass

    def restore_all(self) -> List[str]:
        """Relaunch sessions the previous daemon recorded as running.

        Returns the names that failed to restore (they stay deregistered but
        their logs remain on disk).
        """
        path = paths.sessions_json()
        if not path.is_file():
            return []
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        failed: List[str] = []
        for entry in entries if isinstance(entries, list) else []:
            try:
                sdef = SessionDef.from_dict(entry.get("def") or {})
            except (KeyError, ValueError, TypeError):
                continue
            if not (sdef.restore and entry.get("was_running")):
                continue
            try:
                self.create(sdef, restoring=True)
            except Exception:
                failed.append(sdef.name)
        return failed
