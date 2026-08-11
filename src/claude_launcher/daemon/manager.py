"""Session registry: create/kill/list plus definition persistence and restore.

Sessions die with the daemon (the tmux model), but their *definitions* are
persisted to ``sessions.json`` so a restarting daemon can relaunch the ones
marked ``restore`` — the claude harness comes back with ``--resume`` of the
conversation id pinned at creation, recovering its own conversation.

Everything it does *not* relaunch is kept as a :class:`DeadSession` record
rather than forgotten, so a session that exited (or opted out of restore) can
still be respawned days later. The daemon never drops a record on its own:
that is :meth:`SessionManager.kill` for one and :meth:`SessionManager.clear`
for all of them, both reachable only from the CLI and the web UI.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from typing import Dict, List, Optional, Union

from . import harness as harness_mod
from . import paths
from .harness import SessionDef
from .session import DeadSession, Session

#: Either a live session or the record left behind by one that ended.
AnySession = Union[Session, DeadSession]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ManagerError(Exception):
    """Raised for bad session names, duplicates, or unknown sessions."""


class SessionManager:
    def __init__(self, *, idle_threshold: float, scrollback: int, restore_default: bool) -> None:
        self.idle_threshold = idle_threshold
        self.scrollback = scrollback
        self.restore_default = restore_default
        self._sessions: Dict[str, AnySession] = {}

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
            if self._sessions[name].exited:
                raise ManagerError(
                    f"session {name!r} already exists as an exited record — "
                    f"respawn it to reuse its conversation, or drop it first "
                    f"with 'claunch kill-session {name}'"
                )
            raise ManagerError(f"session {name!r} already exists")
        sdef = self._resolve_resume(
            SessionDef.from_dict({**sdef.to_dict(), "name": name})
        )
        sdef = harness_mod.normalize(sdef, restoring=restoring)
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

    def _resolve_resume(self, sdef: SessionDef) -> SessionDef:
        """Turn a ``resume`` that names a session into that session's
        conversation id.

        The registry is the only place that mapping exists, which is why it
        happens here rather than in :mod:`harness`. Callers name a *session*
        because that is what they can see (in the web UI's picker, in
        ``claunch sessions``); a raw conversation uuid passes straight
        through, so the stored definition — and every later restore of it —
        only ever deals in ids.
        """
        if not sdef.resume:
            return sdef
        source = self._sessions.get(sdef.resume)
        if source is None:
            return sdef  # a conversation uuid (or claude's own history)
        cid = source.sdef.conversation_id
        if not cid:
            raise ManagerError(
                f"session {sdef.resume!r} has no pinned conversation to resume "
                f"(it was started with its own --resume/--continue args)"
            )
        return replace(sdef, resume=cid)

    def _auto_name(self) -> str:
        """The first free ``sN``.

        Exited records count as taken (they are still respawnable), and so do
        the session directories left on disk — a recycled name would append to
        another session's output log and make two histories look like one.
        ``clear-sessions --logs`` is what frees the numbers again.
        """
        i = 0
        while f"s{i}" in self._sessions or paths.session_dir(f"s{i}").is_dir():
            i += 1
        return f"s{i}"

    def get(self, name: str) -> AnySession:
        try:
            return self._sessions[name]
        except KeyError:
            raise ManagerError(f"no session named {name!r}") from None

    def list(self) -> List[AnySession]:
        return [self._sessions[k] for k in sorted(self._sessions)]

    def kill(self, name: str, *, force: bool = False) -> AnySession:
        """Kill a running session; deregister an already-exited one."""
        session = self.get(name)
        if session.exited:
            del self._sessions[name]
        else:
            session.kill(force=force)
        self.persist()
        return session

    def clear(self, *, logs: bool = False) -> List[str]:
        """Drop the record of every session that is no longer running.

        Running sessions are untouched. This is the *only* thing that makes a
        session unresumable, which is why the daemon never does it on its own —
        it happens when a human asks (``claunch clear-sessions``, or the web
        UI's clear button). ``logs`` also deletes their captured output,
        freeing their auto-generated names for reuse.
        """
        names = [name for name, s in self._sessions.items() if s.exited]
        for name in names:
            del self._sessions[name]
            if logs:
                shutil.rmtree(paths.session_dir(name), ignore_errors=True)
        self.persist()
        return names

    def respawn(self, name: str) -> Session:
        """Relaunch an exited session under its original definition.

        Restore semantics apply: the claude harness comes back with
        ``--resume`` of the conversation id pinned at creation, so quitting
        the program by accident (double ``Ctrl+C``) is recoverable — same
        conversation, same session name. Works just as well on a record that
        outlived the daemon that spawned it.
        """
        session = self.get(name)
        if not session.exited:
            raise ManagerError(
                f"session {name!r} is still running (attach to it, or kill it first)"
            )
        del self._sessions[name]
        try:
            return self.create(session.sdef, restoring=True)
        except Exception:
            self._sessions[name] = session  # keep the exited record on failure
            raise

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
                    # carried across restarts so a retired record keeps
                    # answering like the session it was
                    "pid": session.pid,
                    "created_at": session.created_at,
                    "last_output_at": session.last_output_at,
                    "exited_at": session.exited_at,
                }
            )
        path = paths.sessions_json()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except OSError:
            pass

    def restore_all(self) -> List[str]:
        """Bring back everything the previous daemon knew about.

        Sessions it recorded as running are relaunched when they opted into
        ``restore``. Everything else is *kept, not dropped*: an exited session,
        one created ``--no-restore``, and a relaunch that failed all come back
        as exited records the user can respawn (or clear) later.

        Returns the names whose relaunch failed — they are listed as exited
        records, so nothing is lost by retrying or dropping them.
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
            if sdef.name in self._sessions:
                continue  # a duplicated record must not clobber a live session
            if sdef.restore and entry.get("was_running"):
                try:
                    self.create(sdef, restoring=True)
                    continue
                except Exception:
                    failed.append(sdef.name)
            self._retire(sdef, entry)
        self.persist()
        return failed

    def _retire(self, sdef: SessionDef, entry: dict) -> None:
        """Register a definition as an exited record (nothing is running)."""
        self._sessions[sdef.name] = DeadSession(
            sdef,
            exit_code=entry.get("exit_code"),
            pid=entry.get("pid"),
            created_at=entry.get("created_at"),
            last_output_at=entry.get("last_output_at"),
            exited_at=entry.get("exited_at"),
            scrollback=self.scrollback,
            idle_threshold=self.idle_threshold,
        )
