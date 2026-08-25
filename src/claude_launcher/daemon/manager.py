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
import os
import re
import shutil
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Tuple, Union

from .. import profile as profile_mod
from .. import spawn as spawn_mod
from .. import transcripts
from . import harness as harness_mod
from . import mesh_roles
from . import paths
from .harness import SessionDef
from .session import STATUS_BUSY, DeadSession, Session

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
        #: Names :meth:`restore_all` relaunched that the previous daemon
        #: recorded as *working* — the audience for the resume nudge
        #: (:mod:`claude_launcher.daemon.resume`). Written once per process,
        #: at restore; empty on a daemon that restored nothing.
        self.resumed_busy: List[str] = []

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
        # After the policy and before the record: a checkout is a thing on
        # disk, so it is made only once nothing left can refuse the request.
        child = spawn_mod.make_worktree(child, request)
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
                    # Recorded, not (re)played: the live copy goes in once via
                    # the opening block, and this record is what lets a
                    # re-briefing restate it after a compaction (see rebrief).
                    "task": str(request.get("task") or ""),
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
        session = self._sessions.get(parent)
        return spawn_mod.capabilities(
            spawn_mod.SpawnPolicy.load(),
            depth=self.depth(parent),
            children=len(self.live_children(parent)),
            # For 'fork' alone: whether there is a conversation to copy is a
            # fact about this session, not about the policy.
            parent=session.sdef.to_dict() if session else None,
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

    def reparent(self, child: str, parent: str, *, actor: str = "") -> dict:
        """Move ``child`` — and everything under it — under ``parent``.

        The tree is nothing but ``SessionDef.parent`` (see above), so the move
        itself is one field and a persist; the guards are the substance:

        * no cycle: ``parent`` may not be ``child`` or anything under it;
        * an exited ``parent`` is refused, as it is refused a spawn — a
          subtree hung from a dead session is a subtree nobody commands;
        * ``spawn.max_depth`` still holds for the DEEPEST session moved, so a
          re-parent cannot carry a subtree past the limit a spawn respects;
        * with an ``actor`` (an agent; an operator passes none) authority runs
          down the tree as it does everywhere else: the actor must command
          ``child``, and ``parent`` must be the actor itself or a session it
          commands. That is exactly what lets a lead hand its own workers to a
          nested worker it spawned for their domain — and nothing else: a
          worker cannot adopt a sibling, a session cannot move itself, and
          nobody can push a session under a stranger.

        What does not change: the child's terminal, conversation, mesh
        handle and cflow run. Only who it answers to — and the mesh edge to
        that new parent is the caller's to open (:meth:`MeshManager.link_lineage`),
        since the tree does not know which meshes the pair share.
        """
        if child == parent:
            raise ManagerError(f"session {child!r} cannot be its own parent")
        if actor and actor == child:
            raise ManagerError(
                f"session {child!r} cannot move itself: a re-parent is done by "
                "the session that commands it, or by an operator"
            )
        target = self.get(child)
        new_parent = self.get(parent)
        if new_parent.exited:
            raise ManagerError(
                f"session {parent!r} has exited — an exited session cannot "
                "take children"
            )
        if parent in self.descendants(child):
            raise ManagerError(
                f"session {parent!r} is under {child!r}: re-parenting would "
                "make a cycle"
            )
        if actor:
            self.require_commands(actor, child)
            if parent != actor:
                self.require_commands(actor, parent)
        policy = spawn_mod.SpawnPolicy.load()
        below = max(
            (self.depth(d) - self.depth(child) for d in self.descendants(child)),
            default=0,
        )
        deepest = self.depth(parent) + 1 + below
        if deepest > policy.max_depth:
            raise ManagerError(
                f"re-parenting {child!r} under {parent!r} would put a session "
                f"{deepest} level(s) deep and the limit is {policy.max_depth} "
                "(spawn.max_depth) — move fewer or shallower sessions"
            )
        previous = target.sdef.parent
        target.sdef = replace(target.sdef, parent=parent)
        self.persist()
        return {
            "session": child,
            "parent": parent,
            "previous": previous,
            "depth": self.depth(child),
        }

    def kill(self, name: str, *, force: bool = False) -> AnySession:
        """Kill a running session; deregister an already-exited one.

        The deregistering half is the operator's alone, and its callers guard
        it: a record a mesh row still names must not be dropped, or the row is
        left naming a session that cannot be respawned or reached. The check
        lives at the route (``_mesh_holds``) because it needs the mesh service
        and this class deliberately does not know about it. The agent-facing
        route does not reach this half at all — its second call is a no-op, so
        a retry cannot destroy anything.
        """
        session = self.get(name)
        if session.exited:
            del self._sessions[name]
        else:
            session.kill(force=force)
        self.persist()
        return session

    def clear(
        self, *, logs: bool = False, keep: Iterable[str] = ()
    ) -> List[str]:
        """Drop the record of every session that is no longer running.

        Running sessions are untouched. This is the *only* thing that makes a
        session unresumable, which is why the daemon never does it on its own —
        it happens when a human asks (``claunch clear-sessions``, or the web
        UI's clear button). ``logs`` also deletes their captured output,
        freeing their auto-generated names for reuse.

        ``keep`` spares named records. The caller decides what goes in it: the
        manager knows nothing of meshes, and a record a mesh row still names is
        one whose deletion strands that row (see ``_mesh_holds`` in the API,
        the only caller that passes this).
        """
        spared = set(keep)
        names = [
            name
            for name, s in self._sessions.items()
            if s.exited and name not in spared
        ]
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

    async def redefine(self, name: str, **changes) -> Session:
        """Stop a session and relaunch it under a changed definition.

        The skeleton every restart-with-changes shares: shut the session down
        (an exited one is already down), recreate it under ``replace(old,
        **changes)`` with restore semantics, and put the old record back if
        the relaunch fails — the failure mode is "stopped, still what it
        was", never half-changed. The name, the pinned conversation, the mesh
        memberships and the parent edge survive by not being touched.

        What may be *refused* is each caller's own check, done before this
        stops anything: a migrate refuses a missing directory, a reborrow an
        unknown lender. The one refusal made here is the no-op — a restart
        that would change nothing must not cost the session its process.
        """
        session = self.get(name)
        old = session.sdef
        new = replace(old, **changes)
        if new == old:
            raise ManagerError(
                f"session {name!r} already runs exactly that definition — "
                "nothing to restart for"
            )
        if not session.exited:
            await session.shutdown()
        return self._recreate(name, session, new)

    def _recreate(
        self, name: str, session: AnySession, new_def: SessionDef
    ) -> Session:
        """Create over a stopped session's slot, keeping its record on failure.

        The record swap every relaunch-under-a-definition ends with: the old
        record leaves the registry for the create, and goes back — stopped,
        and honest about it — if the create fails, so a failed relaunch
        strands nothing.
        """
        del self._sessions[name]
        try:
            return self.create(new_def, restoring=True)
        except Exception:
            self._sessions[name] = session  # keep the record, as it was
            self.persist()
            raise

    async def reborrow(
        self, name: str, borrow: Optional[str], *, null_token: bool = False
    ) -> Session:
        """Restart a session on another answer to "whose token".

        :meth:`migrate` for the auth half of a definition instead of the
        location half, and simpler for it: the directory does not move, so
        the conversation stays filed where it always was and there is no
        transcript to carry. The answers are the same three creation offers
        — borrow a profile's token (and provider), run on the session's own
        profile's, or run with none (``--null``) — and they are *one*
        choice, so picking any of them clears the others: a borrow set on a
        ``--null`` session turns the token back on, and a token asked back
        on a borrowed session turns the borrow off. The new choice is the
        definition's, so it holds across daemon restarts exactly like one
        made at creation — and the token is still looked up fresh at every
        relaunch.

        Refused while nothing has been stopped: a non-claude session (auth
        is spelled in claude's own env), a borrow paired with ``--null``
        (creation's own refusal — the two flags answer "whose token" with
        opposite answers), an unknown lender, and a no-op.
        """
        session = self.get(name)
        old = session.sdef
        if old.harness != harness_mod.CLAUDE_HARNESS:
            raise ManagerError(
                f"--borrow only applies to the claude harness, "
                f"not {old.harness!r}"
            )
        lender = (borrow or "").strip() or None
        if lender is not None and null_token:
            raise ManagerError(
                "--null launches without any OAuth token; it cannot be "
                f"combined with --borrow {lender}"
            )
        if lender is not None:
            # Checked now rather than left for the relaunch to discover: a
            # typo must cost a 400, not a stopped session. (The token itself
            # is *not* required — same terms as creation, where a tokenless
            # lender simply starts unauthenticated.)
            try:
                profile_mod.require(lender)
            except profile_mod.ProfileError as exc:
                raise ManagerError(str(exc)) from exc
        if lender == old.borrow and null_token == old.null_token:
            if lender:
                raise ManagerError(
                    f"session {name!r} already borrows {lender!r}"
                )
            raise ManagerError(
                f"session {name!r} already starts with no token (--null)"
                if old.null_token
                else f"session {name!r} already runs on profile "
                     f"{old.profile!r}'s own token — nothing to clear"
            )
        return await self.redefine(
            name, borrow=lender, null_token=null_token
        )

    async def skip_permissions(self, name: str, skip: bool) -> Session:
        """Restart a session with permission prompts off — or back on.

        The third answer :meth:`redefine` restarts for, after migrate's
        where and reborrow's whose-token: whether claude asks before it
        acts. The flag lives in the definition's args, so the toggle is an
        args edit — appended once, or taken out — and everything else about
        the session (conversation, directory, auth) is untouched.

        Refused while nothing has been stopped: a non-claude session (the
        flag is claude's own), and a no-op. Only the one flag is ever
        touched — a session started with its own extra args keeps them.
        """
        session = self.get(name)
        old = session.sdef
        if old.harness != harness_mod.CLAUDE_HARNESS:
            raise ManagerError(
                "--dangerously-skip-permissions only applies to the claude "
                f"harness, not {old.harness!r}"
            )
        flag = "--dangerously-skip-permissions"
        has = flag in old.args
        if has == skip:
            raise ManagerError(
                f"session {name!r} already skips permission prompts"
                if skip
                else f"session {name!r} is not skipping permission prompts "
                     "— nothing to turn back on"
            )
        args = (
            tuple(a for a in old.args if a != flag)
            if has
            else (*old.args, flag)
        )
        return await self.redefine(name, args=args)

    async def migrate(self, name: str, new_cwd: str) -> Tuple[Session, bool]:
        """Move a session to another directory: stop it, carry its claude
        conversation's transcript, and relaunch it there.

        The one thing a plain kill-and-recreate cannot do. Claude keeps
        transcripts per working directory (see :mod:`claude_launcher.transcripts`),
        so a session relaunched somewhere else resumes nothing — unless its
        transcript is re-filed under the new directory first, which is exactly
        the step this method adds inside :meth:`redefine`'s stop-and-relaunch
        skeleton. Everything else about the session survives by not being
        touched: the name, the pinned conversation id, the mesh memberships
        keyed on the name, the parent edge.

        Refused while nothing has been stopped or moved: a target that is not
        a directory, a session already there, a claude session with no pinned
        conversation (its transcript cannot be identified, so moving it would
        silently lose the conversation), and a profile that no longer exists.
        A relaunch that fails afterwards puts everything back — the transcript
        returns to the old slug and the record keeps its old definition — so
        the failure mode is "still where it was", not "half-moved".

        Returns the relaunched session and whether a transcript was actually
        carried (``False`` for other harnesses, and for a conversation that
        never wrote one — resuming that was already broken, and is reported
        rather than refused because the move changes nothing about it).
        """
        session = self.get(name)
        old = session.sdef
        new_cwd = os.path.abspath(new_cwd)
        if not os.path.isdir(new_cwd):
            raise ManagerError(f"target directory does not exist: {new_cwd}")
        if os.path.normcase(new_cwd) == os.path.normcase(
            os.path.abspath(old.cwd or "")
        ):
            raise ManagerError(f"session {name!r} is already in {new_cwd}")
        config_dir = None
        if old.harness == harness_mod.CLAUDE_HARNESS:
            if not old.conversation_id:
                raise ManagerError(
                    f"session {name!r} has no pinned conversation to carry "
                    "(it was started with its own --resume/--continue/"
                    "--session-id args), so its transcript cannot be "
                    "identified and migrating would lose the conversation"
                )
            try:
                config_dir = profile_mod.require(old.profile).config_dir
            except profile_mod.ProfileError as exc:
                raise ManagerError(str(exc)) from exc
        if not session.exited:
            await session.shutdown()
        moved = None
        if config_dir is not None:
            moved = transcripts.relocate(
                config_dir, old.conversation_id, old.cwd, new_cwd
            )
        try:
            relaunched = self._recreate(name, session, replace(old, cwd=new_cwd))
        except Exception:
            if moved is not None:
                # the transcript returns to the old slug, as the record
                # _recreate restored already keeps its old definition
                transcripts.relocate(
                    config_dir, old.conversation_id, new_cwd, old.cwd
                )
            raise
        return relaunched, moved is not None

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
                    # Whether an agent was mid-turn here. persist() runs
                    # first in shutdown_all, before anything is torn down,
                    # so at a restart this is what the session was doing in
                    # the last moment before the daemon went down — the one
                    # question a restored-but-idle terminal cannot answer
                    # about itself (see daemon/resume.py).
                    "was_busy": session.status() == STATUS_BUSY,
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
        records, so nothing is lost by retrying or dropping them. The ones
        that came back *and* were mid-turn when the previous daemon went down
        are recorded in :attr:`resumed_busy`: a restored session is alive but
        nothing is driving it, and that list is who should be told to carry
        on (:mod:`claude_launcher.daemon.resume`).
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
                    if entry.get("was_busy"):
                        self.resumed_busy.append(sdef.name)
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
