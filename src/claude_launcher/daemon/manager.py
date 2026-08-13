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

from .. import spawn as spawn_mod
from . import harness as harness_mod
from . import mesh_roles
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
    def stage(self, sdef: SessionDef, *, restoring: bool = False) -> Session:
        """Register a session without starting it.

        The first half of :meth:`create`, separated because onboarding has to
        happen in between: a mesh join and a cflow run both key on the session
        (the join refuses a name that is not a live session here), while the
        opening message they compose has to be known *before* the harness is
        spawned to be passed on its command line. So the session is real from
        this point — named, registered, joinable — and not yet running.

        Every staged session must be either :meth:`launch`ed or
        :meth:`discard`ed; nothing else should be handed one.
        """
        name = (sdef.name or "").strip() or self._auto_name()
        self._check_name(name)
        sdef = self._resolve_resume(
            SessionDef.from_dict({**sdef.to_dict(), "name": name})
        )
        session = Session(
            harness_mod.normalize(sdef, restoring=restoring),
            idle_threshold=self.idle_threshold,
            scrollback=self.scrollback,
        )
        self._sessions[name] = session
        return session

    def launch(
        self, session: Session, *, restoring: bool = False, opening: str = ""
    ) -> Session:
        """Start a staged session. ``opening`` is a first user message for the
        harnesses that take one on their command line (see
        :func:`harness.takes_opening_argv`)."""
        argv, env, cwd = harness_mod.build_command(
            session.sdef, restoring=restoring, opening=opening
        )
        session.start(argv, env, cwd)
        self.persist()
        return session

    def discard(self, name: str) -> None:
        """Drop a staged session that will never start."""
        session = self._sessions.get(name)
        if session is not None and getattr(session, "pty", None) is None:
            self._sessions.pop(name, None)

    def _check_name(self, name: str) -> None:
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

    def create(
        self, sdef: SessionDef, *, restoring: bool = False, opening: str = ""
    ) -> Session:
        """Build and start a session.

        ``opening`` is a first user message for harnesses that take one on
        their command line; see :func:`harness.takes_opening_argv`.
        """
        session = self.stage(sdef, restoring=restoring)
        try:
            return self.launch(session, restoring=restoring, opening=opening)
        except Exception:
            self.discard(session.sdef.name)
            raise

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

    def assign_identity(self, session: Session, identity: str) -> None:
        """Fix who a staged session is, before its command line is built.

        Identity goes into the system prompt, which is settled when the harness
        is spawned — so it can be decided any time before :meth:`launch`, and
        not one moment after.
        """
        if identity:
            session.sdef = replace(session.sdef, identity=identity)

    def spawn(
        self, parent: str, request: dict, *, identity: str = "", opening: str = ""
    ) -> Session:
        """Create and start a child of ``parent`` under the spawn policy.

        :meth:`stage_child` plus :meth:`launch`, for callers with nothing to
        arrange in between.
        """
        session = self.stage_child(parent, request, identity=identity)
        try:
            return self.launch(session, opening=opening)
        except Exception:
            self.discard(session.sdef.name)
            raise

    def stage_child(
        self, parent: str, request: dict, *, identity: str = ""
    ) -> Session:
        """Register a child of ``parent`` under the spawn policy, unstarted.

        The agent-facing counterpart of :meth:`create`: the child is built
        from the parent's own definition, with only the fields
        :mod:`claude_launcher.spawn` permits taken from ``request``. Raises
        :class:`~claude_launcher.spawn.SpawnDenied` when the policy refuses,
        which callers map to 403 rather than 400 — the request was well
        formed, it was simply not allowed.

        A child inherits its parent's ``restore``, because its lifetime is
        bound to the work it was spawned for and not to the daemon process.
        The state a child accumulates outlives the process holding it — its
        pinned conversation, its mesh membership, the cflow run keyed by its
        session name — and only a session of that same name can ever pick
        that up again. A child that stayed dead across a restart therefore
        strands every one of them, silently and for as long as nobody looks.
        ``--no-restore`` on a root still marks the whole subtree ephemeral,
        which is how a throwaway worker says so.
        """
        session = self.get(parent)
        if session.exited:
            raise ManagerError(
                f"session {parent!r} has exited — an exited session cannot "
                "spawn children"
            )
        policy = spawn_mod.SpawnPolicy.load()
        child = spawn_mod.check(
            policy,
            request,
            parent=session.sdef.to_dict(),
            depth=self.depth(parent),
            children=len(self.live_children(parent)),
        )
        return self.stage(
            SessionDef.from_dict(
                {
                    **child,
                    "name": str(request.get("name") or "").strip(),
                    "cols": int(request.get("cols") or session.sdef.cols),
                    "rows": int(request.get("rows") or session.sdef.rows),
                    "role": self._spawn_role(
                        request.get("role"), child.get("harness") or ""
                    ),
                    "parent": parent,
                    # Set explicitly: spawn.check() hands back the inherited
                    # subset and ``restore`` is not in it, so leaving it out
                    # would take from_dict's default and ignore a parent
                    # created --no-restore.
                    "restore": session.sdef.restore,
                    # Who the child IS. Optional here: a caller that needs the
                    # child's resolved cwd to work it out can stage first and
                    # :meth:`assign_identity` before launching.
                    "identity": identity,
                }
            )
        )

    @staticmethod
    def _spawn_role(raw, harness: str) -> Optional[str]:
        """The requested role, but only where the *session* layer takes one.

        ``role`` means two things at once on a spawn, and they answer to
        different authorities: a session's role comes from the packaged
        vocabulary and injects a stance into a **claude** system prompt,
        while a member's role comes from whatever vocabulary that mesh
        declared, applies to any harness, and is set by the mesh join.

        Two cases therefore carry a legal mesh role and no session stance —
        a mesh that replaced the vocabulary wholesale, and a child running a
        harness with no system prompt to inject into. Both are dropped here
        rather than raised, because the caller asked for something coherent
        and failing the whole spawn over the half we cannot honour would
        make custom vocabularies and non-claude harnesses un-spawnable.
        """
        name = str(raw or "").strip()
        if not name or harness != harness_mod.CLAUDE_HARNESS:
            return None
        return name if mesh_roles.resolve().canonical(name) else None

    def spawn_capabilities(self, parent: str) -> dict:
        """What ``parent`` may spawn right now (policy + its current counts)."""
        return spawn_mod.capabilities(
            spawn_mod.SpawnPolicy.load(),
            depth=self.depth(parent),
            children=len(self.live_children(parent)),
        )

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

    # ------------------------------------------------------------------ #
    # hierarchy
    #
    # The tree is derived from ``SessionDef.parent`` on every call rather than
    # kept as a second structure. There are tens of sessions, not thousands,
    # and a cached tree would need invalidating on create, kill, clear and
    # restore — four chances for the list and the tree to disagree about who
    # exists, which is exactly the bug a spawn limit must not have.
    #
    # Every walk is cycle-guarded. A cycle cannot be built through
    # :meth:`spawn` (the parent must already exist, so an edge always points
    # at an older session), but ``parent`` is a plain field on a definition
    # the API accepts and ``sessions.json`` persists, so a hand-edited file or
    # a direct POST can still produce one. Guarding costs a ``set`` and turns
    # a daemon hang into a wrong-but-finite answer.
    # ------------------------------------------------------------------ #
    def children(self, name: str) -> List[str]:
        """Direct children of ``name``, live or exited, in name order."""
        return sorted(
            n for n, s in self._sessions.items() if s.sdef.parent == name and n != name
        )

    def live_children(self, name: str) -> List[str]:
        """Direct children of ``name`` that are still running.

        What the spawn budget counts, and the only place the distinction is
        drawn: the tree, :meth:`descendants` and :meth:`commands` all need the
        exited record, or an agent could neither see what it built nor drop
        the record of a child it had just ended.

        A budget that counted the exited would make ``kill`` half a tool —
        the terminal is gone, the slot is not, and the agent has to end the
        same child twice to spawn again. It is a cap on how many agents are
        running at once, so it counts the ones that are.

        The trade is that ``claunch respawn`` can now put a parent one over
        the cap, since the slot it left is gone by the time it comes back.
        That is a human's explicit call on a session that already existed,
        and the plain create path has never been budget-checked either; the
        report clamps ``children_remaining`` at zero and says the limit is
        reached, which is true.
        """
        return sorted(
            n for n, s in self._sessions.items()
            if s.sdef.parent == name and n != name and not s.exited
        )

    def ancestors(self, name: str) -> List[str]:
        """``name``'s ancestors, nearest first, stopping at the first one that
        no longer exists — a dangling parent makes its child a root."""
        out: List[str] = []
        seen = {name}
        current = self._sessions[name].sdef.parent if name in self._sessions else None
        while current and current in self._sessions and current not in seen:
            out.append(current)
            seen.add(current)
            current = self._sessions[current].sdef.parent
        return out

    def depth(self, name: str) -> int:
        """How deep ``name`` sits: a session with no (resolvable) parent is 0."""
        return len(self.ancestors(name))

    def descendants(self, name: str) -> List[str]:
        """Every session under ``name``, breadth-first."""
        out: List[str] = []
        queue = self.children(name)
        seen = {name}
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            out.append(current)
            queue.extend(self.children(current))
        return out

    def commands(self, actor: str, target: str) -> bool:
        """Whether session ``actor`` may act on session ``target``.

        Authority runs down the tree only: a session commands its own
        descendants and nothing else. Siblings are explicitly excluded — two
        workers spawned by the same lead are peers, and a peer that can kill
        its peer turns a coordination bug into a lost session.

        Humans are not subject to this at all; it gates the agent-facing
        surface, which is the only caller that has an ``actor``.
        """
        return bool(actor) and actor != target and actor in self.ancestors(target)

    def require_commands(self, actor: str, target: str) -> None:
        """:meth:`commands`, as a raise — the check every agent path shares."""
        if actor == target:
            return
        if not self.commands(actor, target):
            raise ManagerError(
                f"session {actor!r} does not command {target!r}: a session may "
                "act on the sessions it spawned (and their descendants), not "
                "on its siblings or its parent"
            )

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
