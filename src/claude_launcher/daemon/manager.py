"""Session registry: create/kill/list plus definition persistence and restore.

Sessions die with the daemon (the tmux model), but their *definitions* are
persisted to ``sessions.json`` so a restarting daemon can relaunch the ones
marked ``restore``. Claude pins an id at creation; Codex reports its chosen id
through its rollout metadata immediately after spawn. Both are stored in the
definition so a relaunch recovers that session's own conversation.

Everything it does *not* relaunch is kept as a :class:`DeadSession` record,
so a session that exited (or opted out of restore) can still be respawned days
later. Archive is the normal retirement path and retains that record. Explicit
remove/clear calls are the exceptional paths that permanently drop it.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
# Bound apart from the module: tests stand a stub in for `time` (monotonic
# only), and the restore timings must not depend on what that stub carries.
from time import perf_counter as _perf_counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

from .. import borrowing, harnesses as harness_registry, profile as profile_mod
from .. import spawn as spawn_mod
from .. import transcripts
from .. import auto_compact as auto_compact_mod
from .. import codex_mode
from . import actors, codex_sessions, ctxsize, db, harness as harness_mod, pi_sessions
from . import progress as progress_mod
from . import line_sync, paths, pty_backend, search_records, session_events, session_input
from .harness import SessionDef
from .screen import BACKGROUND_RENDER_BUDGET, RenderBudget
from .session import (
    STATUS_BUSY, DeadSession, Session, _utcnow, delivery_wake, lost_output_note,
)

#: Either a live session or the record left behind by one that ended.
AnySession = Union[Session, DeadSession]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
#: Longest user note a session may carry. The note is stored in the session
#: record and copied into the search corpus, so it is bounded rather than
#: free: a paste that lands here by accident must not grow either without
#: limit. The web UI's own field carries the same cap.
MAX_NOTE = 2000
#: Exit codes Windows stamps on a console process it ends itself, at a logoff
#: or a system shutdown/restart: DBG_TERMINATE_PROCESS (0x40010004). An exit
#: with this code is the machine going down around the session, not the
#: session ending — the daemon usually goes down a moment later, and until it
#: does it would otherwise record every such exit as final. 2026-09-23 11:09
#: KST: a Windows restart ended 23 sessions this way while the daemon was
#: still alive to persist them as exited, and the next boot's restore_all
#: skipped every one (claunch-wnwa5). POSIX exit statuses are 0-255, so the
#: value cannot come from anywhere else.
OS_ENDED_EXIT_CODES = frozenset({0x40010004})
log = logging.getLogger(__name__)

#: The ``by`` the restore queue wakes a delayed record with.
RESTORE_QUEUE = "restore-queue"

#: Wakes the daemon starts on its own, with no person asking for this
#: incarnation: the restore queue running late, a mesh delivery, a cflow
#: notice or reminder, a status-check refresh (and the cflow and cflow2
#: clocks' ``-checks`` requests to a held driver), the CLI's cflow nudge
#: (``/deliver``), and ``notice`` -- a delivery the daemon makes on its own
#: (a session reminder, a window or QA nudge, a beads wind-down, a restart
#: notice) that names itself with :func:`~.session.delivery_wake`. A
#: session woken by one of these does not get
#: :attr:`Session.resumed_by_human`, so a finished one-shot run's kill-on-end
#: still ends it (claunch-u9m6z: s122 was woken by a mesh delivery and the
#: kill was called off as if a person had respawned it). Any other ``by`` --
#: an attach, typed input, a peer's typed line, the API's wake, a QA
#: answer, a thread message -- is a person and keeps the deference; a
#: ``by`` missing from this set is read as a person, so a new wake source
#: errs toward keeping a session. ``message`` -- what a
#: :meth:`Session.deliver` that reaches a held session wakes it with when
#: its caller does not say otherwise -- stays out: that caller may be a
#: person's door (a forced QA answer, a rebrief, a retrospective), so only
#: a delivery made inside ``delivery_wake(...)`` is read as the daemon's
#: (claunch-2ri1b).
AUTOMATIC_WAKES = frozenset({
    RESTORE_QUEUE, "mesh", "cflow", "cflow2", "status-checks", "deliver",
    "cflow-checks", "cflow2-checks", "notice",
})


def _on_running_loop() -> bool:
    """Whether the caller is on a thread with a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _now_iso() -> str:
    """The lifecycle-marker timestamp format (``archived_at`` and the rest)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _known(**details) -> dict:
    """Event details minus the ones nobody knew: an actor is added to an
    event only when there is one (claunch-o6xws), so the events of a path
    that passes none read as they always did."""
    return {k: v for k, v in details.items() if v is not None}


def _append_transcript(name: str, data: bytes, gap: int) -> None:
    """Append output a session made while no daemon was attached to its
    transcript (claunch-xnnct.1), for a record no live Session writes."""
    if not data and not gap:
        return
    try:
        with open(paths.session_log(name), "ab") as fh:
            if gap:
                fh.write(lost_output_note(gap))
            fh.write(data)
    except OSError:
        log.warning("could not log %r's output from the PTY host", name, exc_info=True)


def ended_by_os(exit_code: Optional[int]) -> bool:
    """Whether ``exit_code`` says Windows ended the process at a logoff or
    shutdown (see :data:`OS_ENDED_EXIT_CODES`)."""
    return exit_code in OS_ENDED_EXIT_CODES


class ManagerError(Exception):
    """Raised for bad session names, duplicates, or unknown sessions."""


@dataclass
class _PendingClaim:
    """A conversation the daemon is still waiting for a harness to name.

    ``claim(timeout)`` scans for it and returns the id or ``None``;
    ``previous`` is the pinned id it will replace (``None`` when the launch
    pinned nothing yet), and the claim is dropped the moment the pin moves
    some other way. ``since`` and ``give_up`` bound the wait; ``what`` and
    ``cwd`` are for the log line when it is abandoned.
    """

    session: Session
    claim: Callable[[float], Optional[str]]
    previous: Optional[str]
    since: float
    give_up: float
    what: str
    cwd: str


def _brings_back(entry) -> bool:
    """Whether a registry record is one a boot actually brings back.

    The registry keeps every session it has ever known -- on this machine
    1551 records for ~48 live ones -- so a boot's progress and its total
    have to be counted over *this* population, not over the file. One
    predicate for both, or the banner runs past its own total
    (claunch-9akgh review).
    """
    return bool(
        isinstance(entry, dict)
        and (entry.get("was_running") or entry.get("delayed_at"))
    )


class SessionManager:
    #: Filesystem discovery for a rollout that Codex has not written yet is
    #: retried from ordinary manager reads.  One dashboard request can perform
    #: hundreds of those reads, so retries are rate-limited per manager rather
    #: than repeated once per ``get()`` call.
    #: How long the launch-time wait (off the loop) gives Codex to write
    #: its rollout before the listing-poll retries take over.
    _CODEX_CLAIM_LAUNCH_TIMEOUT = 2.0
    #: How long :meth:`persist_soon` waits, so the relaunches that land
    #: together after a restart share one registry write (claunch-7l7b7).
    PERSIST_COALESCE_SECONDS = 1.0
    _CODEX_CLAIM_RETRY_INTERVAL = 1.0
    #: The interval grows (doubling, up to this) while a claim keeps
    #: failing, and the claim is abandoned after ``_CODEX_CLAIM_GIVE_UP``
    #: seconds. A rollout that never matches — the session's cwd is not the
    #: one Codex recorded (a workspace/cwd mismatch on restore) — otherwise
    #: means one filesystem scan per second on the event loop for the life
    #: of the session; on 2026-09-11 that scan, slowed to seconds each by a
    #: bloated heap, was what kept the daemon from answering at all.
    _CODEX_CLAIM_RETRY_MAX = 30.0
    _CODEX_CLAIM_GIVE_UP = 600.0
    #: How long the off-loop wait after a codex ``/new`` lasts before the
    #: claim is left to the listing polls. Codex writes the rollout at the
    #: command, so this usually settles it; it is a head start, not a limit.
    _CODEX_SWITCH_WAIT = 3.0
    #: How long a pi ``/new`` claim stays pending. pi writes the file with
    #: the first assistant answer after the command, and a session can sit
    #: unanswered for a long time; the scan behind it is one directory
    #: listing plus the first line of each new file, so waiting is cheap.
    _PI_SWITCH_GIVE_UP = 6 * 3600.0

    def __init__(
        self,
        *,
        idle_threshold: float,
        scrollback: int,
        restore_default: bool,
        focused_session_scheduling: bool = True,
        background_render_delay: float = 0.05,
        background_render_budget: float = BACKGROUND_RENDER_BUDGET,
        restore_stagger: float = 0.0,
    ) -> None:
        self.idle_threshold = idle_threshold
        #: Seconds between the restore queue's relaunches
        #: (``restore_stagger_seconds``, claunch-0j6gb.2). 0 — the default
        #: here, so a manager built without the daemon's config behaves as it
        #: always did — relaunches everything inside :meth:`restore_all`.
        self.restore_stagger = max(0.0, float(restore_stagger or 0.0))
        self.scrollback = scrollback
        self.restore_default = restore_default
        self.focused_session_scheduling = focused_session_scheduling
        self.background_render_delay = background_render_delay
        #: One bucket for every background feeder in this daemon (see
        #: ``screen.RenderBudget``): the sum of unattended rendering, not the
        #: per-session pace, is what saturated the loop on 2026-09-11.
        self.render_budget = RenderBudget(background_render_budget)
        self._sessions: Dict[str, AnySession] = {}
        self.events = session_events.Events(paths.daemon_dir())
        #: What each session last published about its own progress (see
        #: :mod:`progress`): fed by the OSC hook set in :meth:`stage` and by
        #: ``POST /api/sessions/{name}/progress``, read by the list routes.
        self.progress = progress_mod.ProgressStore(paths.session_progress_json())
        #: The durable session registry (:mod:`claude_launcher.daemon.db`). One
        #: row per session; :meth:`persist` writes it, :meth:`restore_all` reads
        #: it. A daemon that predates the database hands its ``sessions.json``
        #: over here, once.
        self._store = db.SessionStore(paths.sessions_db())
        imported = self._store.migrate_from_json(paths.sessions_json())
        if imported:
            log.info(
                "migrated %d session record(s) from sessions.json into "
                "sessions.db", imported,
            )
        #: True only while :meth:`restore_all` is still filling the set. Any
        #: persist made meanwhile must upsert without pruning, or it would
        #: delete every record not loaded yet; relaunches skip theirs.
        self._loading = False
        #: Seconds the current :meth:`restore_all` spent per launch step.
        self._restore_clock = {
            "prepare": 0.0, "spawn": 0.0, "attach": 0.0, "adopt": 0.0,
        }
        #: Names whose relaunch failed in the current :meth:`restore_all`.
        #: The pass is a generator now, so the return value cannot carry
        #: them out of it; the two callers read this instead.
        self._restore_failed: List[str] = []
        #: Set by :meth:`clear` and :meth:`remove` for the one persist that
        #: follows, so a *deliberate* empty write is allowed to prune the store
        #: to empty — the one case the empty-snapshot guard must let through.
        self._allow_empty_persist = False
        #: Names :meth:`restore_all` relaunched that the previous daemon
        #: recorded as *working* — the audience for the resume nudge
        #: (:mod:`claude_launcher.daemon.resume`). Written once per process,
        #: at restore; empty on a daemon that restored nothing.
        self.resumed_busy: List[str] = []
        #: Names :meth:`restore_all` relaunched that were *idle* at the
        #: shutdown, with the PTY stamp (``last_output_at``) the previous
        #: daemon recorded for them. An idle agent may have ended its turn
        #: to wait for a background command, which the restart killed, so
        #: nothing will start its next turn either; the resume nudge reads
        #: the stamp to tell such a session from one that finished long ago
        #: (claunch-ccoof).
        self.resumed_idle: Dict[str, Optional[str]] = {}
        #: Names :meth:`restore_all` relaunched whose conversation was *not*
        #: on disk yet, so the restore opened an empty one
        #: (:func:`harness.restores_blank`). Kept apart from
        #: :attr:`resumed_busy` because the two ask different questions: that
        #: one is "was a turn cut off", this one is "did the session come back
        #: knowing anything at all". A session can be on both lists, on either,
        #: or on neither, and an *idle* blank restore still lost everything it
        #: knew — so this list is not filtered by what the session was doing.
        self.resumed_blank: List[str] = []
        #: Names :meth:`restore_all` took over from the PTY host still
        #: running (claunch-xnnct.1): same process, nothing resumed, so they
        #: are on none of the lists above.
        self.adopted: List[str] = []
        #: Names :meth:`restore_all` filed as *delayed* instead of relaunching
        #: (claunch-0j6gb.2), in registry order; the restore queue
        #: (:mod:`claude_launcher.daemon.restore_queue`) ranks and wakes
        #: them. They are also on :attr:`resumed_busy` / :attr:`resumed_idle`
        #: as their state said, so the resume nudge reaches them once they
        #: are up.
        self.restore_queue: List[str] = []
        #: The delayed names that were mid-turn when the previous daemon went
        #: down. A delayed record reads ``exited``, so :meth:`persist` would
        #: otherwise write ``was_busy: False`` for it and a second restart
        #: before its turn would forget it was working.
        self._delayed_busy: Set[str] = set()
        #: Called with the new incarnation each time a delayed record comes
        #: back, whatever woke it (the queue, a viewer, a message). The resume
        #: nudge registers here: whether a codex restore is blank is only
        #: known once the launch has run.
        self.delayed_wake_hooks: List[Callable[[Session], None]] = []
        #: Called with a *failed* record when a person settles it (kill,
        #: archive, remove — :meth:`unhold`): the part of its ending the
        #: failure put off. The board registers its sweep here as well as on
        #: :attr:`exit_hooks`, and skips a failed session on the latter
        #: (claunch-0j6gb.1); every other exit hook already ran at the exit.
        self.settle_hooks: List[Callable[[AnySession], None]] = []
        #: Called with a session once its child is gone for good — whatever
        #: ended it. Registered by whoever has a stake in an ending (the
        #: board sweep, :mod:`claude_launcher.daemon.beads`); not called for
        #: the endings a daemon shutdown causes, which are not endings at
        #: all — those sessions come back with the next daemon.
        self.exit_hooks: List[Callable[[Session], None]] = []
        #: Called with a session as it comes up again — a launch (respawn,
        #: restore, resume) and the adopt of a surviving PTY. Registered by
        #: whoever has something queued for a session that was ended and has
        #: no terminal to type into (:mod:`claude_launcher.daemon.thread_notices`
        #: is typed in from here).
        self.launch_hooks: List[Callable[[Session], None]] = []
        # Old process objects, not names: the replacement must still report
        # its own exit normally. Values record an exit received during restart.
        self._restarting: Dict[int, bool] = {}
        #: Called, with nothing, after every :meth:`persist` — the one funnel
        #: every registry change goes through (a session created, exited,
        #: re-parented, re-labelled). The search index's fleet corpus follows
        #: the registry from here (:meth:`daemon.rag.RagService.enqueue`);
        #: not called while the daemon is shutting down.
        self.change_hooks: List[Callable[[], None]] = []
        self.shutting_down = False
        #: The sessions :meth:`shutdown_all` found running and is ending,
        #: by name, with no ending already asked for. Their exit is the
        #: daemon's, so :meth:`persist` keeps them ``was_running`` after it:
        #: a persist that lands mid-shutdown -- a board sweep finishing, a
        #: kill request still being served -- otherwise read ``exited`` off
        #: the live objects and filed them as plain kills, and the next boot
        #: retired every one of them (claunch-5aysr.1: 52 of 92 sessions at
        #: the 2026-10-15T12:00:46Z restart). A verb that asks for one of
        #: them to end meanwhile takes it out (:meth:`_asked_to_end`), and an
        #: archive files it away for good (an archived record never restores).
        self._ended_by_shutdown: Dict[str, AnySession] = {}
        #: Records :meth:`restore_all` retired because they did not come back
        #: at a restart — the ``--no-restore`` sessions that were running when
        #: the previous daemon went down, and the relaunches that failed. Their
        #: exit never reaches :attr:`exit_hooks`: the hook is only wired in
        #: :meth:`create`, and by the time :meth:`restore_all` runs the board
        #: does not even exist yet. The board sweep their ending calls for is
        #: owed at boot, and :meth:`take_retired_for_sweep` is who collects it.
        self._retired_for_sweep: List[DeadSession] = []
        #: One lock per name for :meth:`wake_async`, and the tasks
        #: :meth:`wake_soon` scheduled (kept so they are not collected).
        self._wake_locks: Dict[str, asyncio.Lock] = {}
        self._wake_tasks: Set[asyncio.Task] = set()
        # Notifications belong to a name while its replacement is staged.
        # A staged Session is registered before launch_async finishes.
        self._wake_messages: Dict[str, List[str]] = {}
        self._wake_message_tasks: Dict[str, asyncio.Task] = {}
        #: Names whose pending message wake a person's message joined after
        #: a notice started it (:meth:`queue_message`).
        self._wake_message_people: Set[str] = set()
        #: Names mid-wake. The respawn takes the record out of the registry
        #: while the new child spawns, and a reader in that gap must not
        #: take the session for removed (:meth:`is_waking`).
        self._waking: Set[str] = set()
        # A harness can write the file that names its conversation well after
        # the moment claunch would like to know it: codex writes its rollout
        # after the launch wait expires, pi writes the file ``/new`` creates
        # only with the first assistant answer. Every such claim is kept here
        # with the snapshot it was armed from, so ordinary dashboard polling
        # can settle it later (:meth:`_recover_claims`) without blocking the
        # session or falling back to whichever conversation is newest.
        self._pending_claims: Dict[str, _PendingClaim] = {}
        self._next_claim_retry = 0.0
        self._claim_interval = self._CODEX_CLAIM_RETRY_INTERVAL
        #: A TUI keeps one process alive across ``/new`` while changing the
        #: conversation underneath it (codex its rollout UUID, pi its session
        #: file). Each watcher is armed from that exact terminal command, with
        #: a pre-command snapshot, so concurrent sessions in the same cwd
        #: cannot be confused by a cwd-wide "latest" lookup.
        self._codex_switch_tasks: Dict[str, asyncio.Task] = {}
        self._codex_launch_tasks: Dict[str, asyncio.Task] = {}
        #: Background flushes of operator lines queued while a session was
        #: exited (see :meth:`flush_queued_input`); held so none is collected.
        self._input_flush_tasks: Set[asyncio.Task] = set()
        #: The write :meth:`persist_soon` scheduled and has not run yet;
        #: any :meth:`persist` takes its place.
        self._persist_handle: Optional[asyncio.TimerHandle] = None

    @property
    def _pending_codex_claims(self) -> Dict[str, "_PendingClaim"]:
        """The pending claims, under the name the codex-only version had."""
        return self._pending_claims

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def stage(
        self,
        sdef: SessionDef,
        *,
        restoring: bool = False,
        created_at: Optional[str] = None,
        last_visited_at: Optional[str] = None,
        last_input_at: Optional[str] = None,
        delivery_hold: bool = False,
    ) -> Session:
        """Register a session without starting it.

        The first half of :meth:`create`, separated because onboarding has to
        happen in between: a mesh join and a cflow run both key on the session
        (the join refuses a name that is not a live session here), while the
        opening message they compose has to be known *before* the harness is
        spawned to be passed on its command line. So the session is real from
        this point — named, registered, joinable — and not yet running.

        Every staged session must be either :meth:`launch`ed or
        :meth:`discard`ed; nothing else should be handed one.

        ``created_at`` carries an existing session's creation time into the
        object replacing it. Only the relaunch paths pass it (restore,
        respawn, redefine): those keep the name, the conversation, the mesh
        memberships and the parent edge, so the creation time is one more
        thing that must survive them — see :meth:`list`.
        ``last_visited_at``/``last_input_at`` ride along on the same argument
        and for the same reason: when a person last looked in on this session
        and last typed into it are facts about the session, not about the
        process, and a relaunch that dropped them would tell the rail that
        nobody has ever been near a session somebody was reading a minute ago.
        ``delivery_hold`` rides in on the same terms and for the same reason:
        a person pinned this session shut, and the relaunch is that session
        continuing, so the pin continues with it (see
        :attr:`Session._delivery_hold`).
        """
        name = (sdef.name or "").strip() or self._auto_name()
        self._check_name(name)
        sdef = self._resolve_resume(
            SessionDef.from_dict({**sdef.to_dict(), "name": name})
        )
        if created_at is None:
            created_at = self._fresh_created_at()
        session = Session(
            harness_mod.normalize(sdef, restoring=restoring),
            idle_threshold=self.idle_threshold,
            scrollback=self.scrollback,
            created_at=created_at,
            last_visited_at=last_visited_at,
            last_input_at=last_input_at,
            delivery_hold=delivery_hold,
            focused_session_scheduling=self.focused_session_scheduling,
            background_render_delay=self.background_render_delay,
            render_budget=self.render_budget,
        )
        session.on_exit = self._session_exited
        session.on_command_submitted = self._session_command_submitted
        session.on_progress = self._session_progress
        session.wake_on_message = self.wake_async
        session.queue_on_message = self.queue_message
        session.message_restore_pending = self.message_restore_pending
        self._sessions[name] = session
        return session

    def _session_progress(self, session: Session, body: bytes) -> None:
        """Store a progress sequence a session wrote to its terminal.

        One the store refuses is dropped with a debug line: the program that
        wrote it has no channel to hear an error on, and its output must go
        on regardless.
        """
        try:
            self.progress.set(session.sdef.name, progress_mod.parse_osc(body))
        except ValueError as exc:
            log.debug("progress from %r refused: %s", session.sdef.name, exc)

    def _session_command_submitted(self, session: Session, command: str) -> None:
        """Follow the conversation a TUI's ``/new`` replaces underneath it.

        Codex keeps one process alive across ``/new`` while changing the
        rollout UUID; pi does the same while changing its session file. Both
        leave the definition's pinned conversation pointing at the one the
        session just left, so a restore would reopen that one and the
        transcript page would read it. The watcher is armed from the exact
        command, with a snapshot taken before it reaches the child, so
        concurrent sessions in one cwd cannot be confused by a cwd-wide
        "latest" lookup. Other submitted lines are ignored here.
        """
        words = command.split()
        if not words or words[0] != "/new":
            return
        if self._sessions.get(session.sdef.name) is not session:
            return
        if session.sdef.harness == "codex":
            self._watch_codex_new(session)
        elif session.sdef.harness == harness_mod.PI_HARNESS:
            self._watch_pi_new(session)

    def _watch_codex_new(self, session: Session) -> None:
        """Arm the claim for the rollout codex is about to write for ``/new``.

        The claim is a *pending* one (:meth:`_arm_claim`), retried by the
        listing polls, and only additionally waited for a few seconds off the
        loop. The one-shot wait this replaced gave up after three seconds and
        left the pin on the superseded rollout: measured on this machine
        2026-09-14, session ``s536`` ran ``/new`` at 09:46:39Z, codex wrote
        rollout ``01a09f4f`` for that cwd the same second, and the daemon
        logged "could not discover Codex /new conversation id" at 09:46:42 --
        after which every restore reopened the conversation from before the
        command. Why the scan inside the window missed a file that a scan
        today finds is not known; what is known is that a claim with no
        second chance is the wrong shape for a file another process writes.
        """
        name = session.sdef.name
        try:
            prof = profile_mod.require_selector(session.sdef.profile or "")
            entry = harness_registry.get("codex")
            if entry is None:
                return
            codex_home = entry.profile_home(prof.config_dir)
            known = codex_sessions.snapshot(codex_home)
        except Exception:
            log.exception(
                "could not snapshot Codex rollouts before /new in session %r",
                name,
            )
            return
        cwd = session.sdef.cwd
        self._arm_claim(
            session,
            what="codex /new",
            cwd=cwd,
            claim=lambda timeout: codex_sessions.claim_new(
                codex_home, cwd, known, timeout=timeout
            ),
            previous=session.sdef.conversation_id,
            give_up=self._CODEX_CLAIM_GIVE_UP,
        )
        previous = self._codex_switch_tasks.pop(name, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(
            self._claim_in_thread(session, timeout=self._CODEX_SWITCH_WAIT)
        )
        self._codex_switch_tasks[name] = task
        task.add_done_callback(functools.partial(self._codex_switch_finished, name))

    def _watch_pi_new(self, session: Session) -> None:
        """Arm the claim for the session file pi creates for ``/new``.

        pi names that file itself (``<timestamp>_<id>.jsonl``, beside the one
        claunch pinned at launch) and writes it only with the first assistant
        answer, which may be minutes away -- so there is no short wait here at
        all, only the pending claim the listing polls retry. The header pi
        stamps into that file carries the moment of the command, and the
        claim uses it to tell this session's ``/new`` from another session's
        in the same directory (see :mod:`pi_sessions`). The pinned id becomes
        the new file's stem, which is exactly what :func:`harness.pi_session_file`
        turns back into the path a restore reopens.
        """
        name = session.sdef.name
        if not session.sdef.conversation_id:
            return  # nothing pinned to follow from (a pre-pin definition)
        if harness_mod.steers_pi_conversation(session.sdef.args):
            return  # the caller chose the conversation; it is not ours to move
        cwd = os.path.abspath(session.sdef.cwd or os.getcwd())
        try:
            prof = profile_mod.require_selector(session.sdef.profile or "")
            entry = harness_registry.get(harness_mod.PI_HARNESS)
            if entry is None:
                return
            session_dir = Path(harness_mod.pi_session_file(
                str(entry.profile_home(prof.config_dir)),
                cwd,
                session.sdef.conversation_id,
            )).parent
            known = pi_sessions.snapshot(session_dir)
        except Exception:
            log.exception(
                "could not snapshot pi session files before /new in session %r",
                name,
            )
            return
        since = time.time()
        self._arm_claim(
            session,
            what="pi /new",
            cwd=cwd,
            claim=lambda timeout: pi_sessions.claim_new(
                session_dir, cwd, known, since=since, timeout=timeout
            ),
            previous=session.sdef.conversation_id,
            give_up=self._PI_SWITCH_GIVE_UP,
        )

    def _codex_switch_finished(self, name: str, task: asyncio.Task) -> None:
        """Forget a completed watcher without deleting a newer replacement."""
        if self._codex_switch_tasks.get(name) is task:
            self._codex_switch_tasks.pop(name, None)

    def _arm_claim(
        self,
        session: Session,
        *,
        what: str,
        cwd: str,
        claim: Callable[[float], Optional[str]],
        previous: Optional[str],
        give_up: float,
    ) -> None:
        """Register a conversation claim for the listing polls to settle.

        ``claim(timeout)`` scans for the conversation and returns its id or
        ``None``; ``previous`` is the id it replaces (``None`` for a launch
        that pinned nothing yet). A newer claim for the same session replaces
        an older one: the pin it would have set is already superseded.
        """
        self._pending_claims[session.sdef.name] = _PendingClaim(
            session=session,
            claim=claim,
            previous=previous,
            since=time.monotonic(),
            give_up=give_up,
            what=what,
            cwd=cwd,
        )
        self._claim_interval = self._CODEX_CLAIM_RETRY_INTERVAL
        self._next_claim_retry = 0.0

    def _settle_claim(self, name: str, conversation_id: str) -> bool:
        """Apply a found conversation id to its pending claim, if still due."""
        pending = self._pending_claims.get(name)
        if pending is None:
            return False
        session = self._sessions.get(name)
        if session is not pending.session:
            self._drop_claim(name)
            return False
        if session.sdef.conversation_id != pending.previous:
            # Something else moved the pin while this claim waited (a newer
            # /new re-armed it, a hook re-pinned it): this answer is stale.
            self._drop_claim(name)
            return False
        session.sdef = replace(session.sdef, conversation_id=conversation_id)
        self._drop_claim(name)
        self.persist()
        log.info(
            "%s: conversation id for session %r is now %s (was %s)",
            pending.what, name, conversation_id, pending.previous,
        )
        return True

    def repin_conversation(
        self, name: str, conversation_id: str, *, source: str = ""
    ) -> Dict[str, object]:
        """Move a session's pinned conversation to the one the harness reports.

        The claude path: its SessionStart hook (``claunch rebrief``, armed on
        ``compact`` and ``clear``) is handed the session id claude is now on,
        and ``/clear`` mints a new one -- measured on this machine 2026-09-15:
        a session pinned at ``5867d547`` fired the hook with ``source: clear``
        and ``session_id: d545fc0c``, and its transcript moved to
        ``d545fc0c.jsonl``. Until the hook reported it, the definition kept
        the old id, so a restore reopened the conversation from before the
        ``/clear`` and the transcript page read it. ``/compact`` keeps the id
        (1822 records of one compacted transcript carry one ``sessionId``),
        and reporting the same id changes nothing.

        Returns what happened; raises :class:`ManagerError` for an unknown
        session or an empty id.
        """
        session = self.get(name)
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ManagerError("conversation id is empty")
        previous = session.sdef.conversation_id
        if previous == conversation_id:
            return {
                "changed": False,
                "conversation_id": conversation_id,
                "previous": previous,
            }
        session.sdef = replace(session.sdef, conversation_id=conversation_id)
        # A pending claim was about the conversation this session just left.
        self._drop_claim(name)
        self.persist()
        log.info(
            "session %r moved to conversation %s (was %s; reported by %s)",
            name, conversation_id, previous, source or "harness",
        )
        return {
            "changed": True,
            "conversation_id": conversation_id,
            "previous": previous,
        }

    def _session_exited(self, session: Session) -> None:
        """Fan a session's exit out to :attr:`exit_hooks` — unless the daemon
        is going down, in which case nothing has ended.

        The exit reaches the store *here*, and not at whatever the next
        :meth:`persist` for another reason happens to be. ``was_running`` is
        read off the live object, and every ending that goes through a verb
        persists in the same breath as the signal it sends — while the child
        is still on its way out, so what lands on disk is ``True``. A daemon
        that then goes down without a graceful :meth:`shutdown_all` (an
        abrupt exit, a successor that takes the lock while the predecessor is
        still draining) leaves that stale ``True`` behind, and the next boot's
        :meth:`restore_all` relaunches a session whose child is already gone:
        the kill is undone by the restart, and a run the daemon ends comes
        back only to be ended again. Writing it here means the record is right
        from the moment it becomes right, whatever happens to the daemon
        after.

        :attr:`change_hooks` has documented this as the one funnel a session's
        *exit* goes through since it was written; until now it was the one
        registry change that did not.
        """
        if self.shutting_down:
            return
        self._attribute_ending(session)
        if ended_by_os(session.exit_code):
            # Windows is logging off or shutting down, and got to the child
            # before it got to the daemon. That is the daemon's shutdown
            # arriving out of order, and it is treated like one: persist()
            # keeps the record running so the next boot restores it, and the
            # exit hooks — the board sweep that would return its issue to
            # open above all — do not run, because nothing has ended.
            log.warning(
                "session %r was ended by Windows (logoff/shutdown, code %#x); "
                "kept for restore at the next boot",
                session.sdef.name, session.exit_code,
            )
            session.ended_by_os = True
            self.events.record(session, "exit", "OS 종료로 세션 프로세스 종료",
                               exit_code=session.exit_code,
                               **_known(ended_by=getattr(session, "ended_by", None)))
            self.persist()
            return
        if getattr(session, "held_at", None):
            # A hold ends the program, not the session (Session.held_at): the
            # board keeps its in_progress issue, side runs keep running, and
            # nobody is told the session is gone. The hooks run if and when
            # the hold is turned into a real ending (unhold).
            self.events.record(session, "hold", "유휴 세션 hold: 프로세스 종료",
                               exit_code=session.exit_code,
                               **_known(ended_by=getattr(session, "ended_by", None)))
            self.persist()
            return
        if self._failed_on_its_own(session):
            # Nobody asked for this ending (claunch-0j6gb.1): the program
            # died under the session — a harness that could not start, a
            # crash. Filed apart from a kill. The exit hooks run as for any
            # ending (the parent hears of the crash, the window is given
            # back), except the board sweep: the issue stays in_progress for
            # the respawn that is the usual answer, and is swept when a
            # person settles the record (kill, archive or remove → unhold →
            # settle_hooks).
            session.failed_at = _now_iso()
            self.events.record(session, "fail", "요청 없이 비정상 종료",
                               exit_code=session.exit_code,
                               **_known(ended_by=getattr(session, "ended_by", None)))
            log.warning(
                "session %r exited on its own with code %s; filed as failed",
                session.sdef.name, session.exit_code,
            )
        else:
            self.events.record(session, "exit", "세션 프로세스 종료",
                               exit_code=session.exit_code,
                               **_known(ended_by=getattr(session, "ended_by", None)))
        self.persist()
        if id(session) in self._restarting:
            self._restarting[id(session)] = True
        else:
            self._run_exit_hooks(session)

    def _asked_to_end(self, session: AnySession) -> None:
        """A verb asked for ``session`` to end: if a shutdown in progress
        counted it among its own endings, that ending is this verb's now, and
        the record reads as it does (see :attr:`_ended_by_shutdown`)."""
        if self._ended_by_shutdown.get(session.sdef.name) is session:
            del self._ended_by_shutdown[session.sdef.name]

    def _attribute_ending(self, session: Session) -> None:
        """Fill in who ended ``session`` when no verb said so (claunch-o6xws).

        A verb that knows its caller writes :attr:`Session.ended_by` before
        it signals; what is left here is what only the exit can tell: the
        machine went down (``os``), or nobody asked at all -- no kill, pause,
        hold or relaunch -- so the session's own program ended (``self``:
        ``/exit``, a crash). An asked-for ending whose asker is not known
        (a path that does not pass one) stays None, which the rail reads as
        "unknown" rather than guessing.
        """
        if getattr(session, "ended_by", None) is not None:
            return
        if ended_by_os(session.exit_code):
            session.ended_by = actors.make(actors.OS)
            return
        if id(session) in self._restarting:
            return
        if getattr(session, "kill_requested", False):
            return
        if getattr(session, "paused_at", None) or getattr(session, "held_at", None):
            return
        session.ended_by = actors.make(actors.SELF)

    def _failed_on_its_own(self, session: Session) -> bool:
        """Whether an exit is a failure: a nonzero code, and no kill, pause,
        hold or relaunch asked for it. A clean exit 0 the agent's harness
        made by itself (``/exit``) is an ending somebody chose, and stays
        killed."""
        if getattr(session, "kill_requested", False):
            return False
        if getattr(session, "paused_at", None) or id(session) in self._restarting:
            return False
        code = session.exit_code
        return code is not None and code != 0

    def _run_exit_hooks(self, session: AnySession) -> None:
        for hook in list(self.exit_hooks):
            try:
                hook(session)
            except Exception:  # one hook must not silence the next
                log.exception("exit hook %r failed for %r", hook, session.sdef.name)

    def launch(
        self, session: Session, *, restoring: bool = False, opening: str = ""
    ) -> Session:
        """Start a staged session. ``opening`` is a first user message for the
        harnesses that take one on their command line (see
        :func:`harness.takes_opening_argv`)."""
        clock = self._restore_clock if restoring and self._loading else None
        t0 = _perf_counter()
        prepared = self._prepare_launch(session, restoring=restoring, opening=opening)
        if restoring:
            # Before the spawn: the first bytes of the new program land on
            # a blank grid with the old lines behind it.
            session.seed_screen_from_log()
        t1 = _perf_counter()
        session.start(*prepared[:3])
        if clock is not None:
            clock["prepare"] += t1 - t0
            clock["spawn"] += _perf_counter() - t1
        return self._launched(session, restoring, *prepared[2:])

    async def launch_async(
        self, session: Session, *, restoring: bool = False, opening: str = ""
    ) -> Session:
        """:meth:`launch` with the process spawn off the event loop (see
        :meth:`Session.start_async`); what the HTTP handlers call."""
        prepared = self._prepare_launch(session, restoring=restoring, opening=opening)
        if restoring:
            # The replay off the loop (claunch-7l7b7), still before the spawn.
            await session.seed_screen_from_log_async()
        await session.start_async(*prepared[:3])
        return self._launched(session, restoring, *prepared[2:])

    def _prepare_launch(self, session: Session, *, restoring: bool, opening: str):
        """Everything :meth:`launch` does before the spawn: returns
        ``(argv, env, cwd, codex_home, known_codex_sessions)``."""
        codex_home = None
        known_codex_sessions = None
        if session.sdef.harness == "codex":
            prof = profile_mod.require_selector(session.sdef.profile or "")
            entry = harness_registry.get("codex")
            if entry is not None:
                codex_home = entry.profile_home(prof.config_dir)
                # Three answers to "which conversation is this session's",
                # strongest first: the args say so outright, a pre-pin
                # definition is matched by cwd, or the rollout codex is about
                # to write is claimed after the spawn.
                if not session.sdef.conversation_id:
                    named = codex_sessions.names_conversation(session.sdef.args)
                    if named:
                        session.sdef = replace(
                            session.sdef, conversation_id=named
                        )
                if restoring and not session.sdef.conversation_id:
                    conversation_id = codex_sessions.latest(
                        codex_home,
                        session.sdef.cwd,
                        taken=self._pinned_conversations(
                            except_for=session.sdef.name
                        ),
                    )
                    if conversation_id:
                        session.sdef = replace(
                            session.sdef, conversation_id=conversation_id
                        )
                if not session.sdef.conversation_id:
                    if codex_sessions.resumes_existing(session.sdef.args):
                        # A resumed conversation writes no new session_meta
                        # record, so a claim could only time out and then retry
                        # forever (see codex_sessions.resumes_existing).
                        log.info(
                            "session %r resumes a codex conversation it does not "
                            "name; leaving its id unpinned rather than waiting "
                            "for a rollout codex will not write",
                            session.sdef.name,
                        )
                    else:
                        known_codex_sessions = codex_sessions.snapshot(codex_home)
        argv, env, cwd = harness_mod.build_command(
            session.sdef, restoring=restoring, opening=opening
        )
        # A restoring launch replays the previous run's log next (launch,
        # launch_async): the relaunched program gets a brand-new screen, and
        # the daemon's pyte history is the only scrollback the web terminal
        # has, so without it the restart silently costs every viewer their
        # wheel.
        return argv, env, cwd, codex_home, known_codex_sessions

    def _launched(
        self, session: Session, restoring: bool, cwd: str, codex_home, known_codex_sessions
    ) -> Session:
        """Everything :meth:`launch` does after the spawn."""
        if not restoring:
            self.events.record(session, "create", "세션 생성", cwd=cwd,
                               borrow=session.sdef.borrow)
            # The opening task is archived here rather than left to the
            # search corpus to pick up, because that corpus only runs once
            # semantic search is configured: on a daemon without it, a
            # session created and later cleared would take its task with
            # it. Best effort for the same reason the event above is --
            # a session must not fail to start because an archive write
            # did.
            try:
                search_records.capture_task(session.sdef.name, session.sdef.task,
                                            session.created_at or "")
            except (OSError, sqlite3.Error):
                log.exception("could not archive the opening task for %s", session.sdef.name)
        if codex_home is not None and known_codex_sessions is not None:
            # The rollout appears a moment after the child starts, and the
            # scan that waits for it (``claim_new``) polls the whole rollout
            # directory. Waited for here, on the loop, that is the daemon —
            # every socket, every keystroke — stopped for the wait: 2.03 s
            # measured with 162 rollouts (claunch-wpd0). So the wait goes
            # to a thread, and the pending entry is registered first so the
            # listing-poll retry (:meth:`_recover_claims`) covers a
            # rollout that outlasts it.
            known = known_codex_sessions
            self._arm_claim(
                session,
                what="codex launch",
                cwd=cwd,
                claim=lambda timeout: codex_sessions.claim_new(
                    codex_home, cwd, known, timeout=timeout
                ),
                previous=None,
                give_up=self._CODEX_CLAIM_GIVE_UP,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                task = loop.create_task(
                    self._claim_in_thread(
                        session, timeout=self._CODEX_CLAIM_LAUNCH_TIMEOUT
                    )
                )
                self._codex_launch_tasks[session.sdef.name] = task
                task.add_done_callback(
                    functools.partial(self._codex_launch_finished, session.sdef.name)
                )
        self.flush_queued_input(session)
        self._run_launch_hooks(session)
        if restoring and not self._loading:
            # A respawn or a wake: after a restart the restore queue and
            # every use-wake (a mesh message, a viewer) land here in bursts,
            # each of which used to write the whole registry on the loop
            # (claunch-7l7b7). They share one write instead.
            self.persist_soon()
        elif not restoring:
            # A restore persists once, after the last relaunch (restore_all).
            # Persisting here as well rewrote the whole registry -- every
            # record, exited ones included -- and re-read every live session's
            # transcript for the model once per relaunched session: work that
            # grew with the square of the fleet, on the path that runs before
            # the port is bound (claunch-t8bgj).
            self.persist()
        return session

    def flush_queued_input(self, session: Session) -> bool:
        """Type the operator lines queued while ``session`` was exited.

        Scheduled, not awaited: the flush waits for the program to take
        input, which is seconds after launch. Returns whether a flush was
        scheduled (there was something queued and a loop to run it on).
        """
        if not session_input.pending(session.sdef.name):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(session_input.flush(session))
        self._input_flush_tasks.add(task)
        task.add_done_callback(self._input_flush_tasks.discard)
        return True

    def _run_launch_hooks(self, session: Session) -> None:
        """Tell :attr:`launch_hooks` a session is up: whatever was queued
        while it had no terminal is delivered from here."""
        for hook in list(self.launch_hooks):
            try:
                hook(session)
            except Exception:  # noqa: BLE001 -- one hook must not silence the next
                log.exception("launch hook %r failed for %r", hook, session.sdef.name)

    async def _claim_in_thread(self, session: Session, *, timeout: float) -> None:
        """Wait off-loop, up to ``timeout``, for a pending claim to settle.

        The scan polls a whole rollout directory; waited for on the loop,
        that is the daemon -- every socket, every keystroke -- stopped for
        the wait (2.03 s measured with 162 rollouts, claunch-wpd0). A miss
        here is not a failure: the claim stays pending and the listing polls
        keep looking (:meth:`_recover_claims`).
        """
        name = session.sdef.name
        pending = self._pending_claims.get(name)
        if pending is None or pending.session is not session:
            return
        claim = functools.partial(pending.claim, timeout)
        try:
            conversation_id = await asyncio.get_running_loop().run_in_executor(
                None, claim
            )
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception(
                "could not discover the %s conversation id for session %r",
                pending.what, name,
            )
            return
        if self._sessions.get(name) is not session:
            return
        if not conversation_id:
            if self._pending_claims.get(name) is pending:
                log.info(
                    "%s conversation id for session %r not written within "
                    "%.0fs; listing polls keep looking",
                    pending.what, name, timeout,
                )
            return
        self._settle_claim(name, conversation_id)

    def _codex_launch_finished(self, name: str, task: asyncio.Task) -> None:
        if self._codex_launch_tasks.get(name) is task:
            self._codex_launch_tasks.pop(name, None)

    def _pinned_conversations(self, *, except_for: str = "") -> Set[str]:
        """Every conversation id another session definition already holds.

        What :func:`codex_sessions.latest` must not hand out a second time. A
        cwd is shared by as many sessions as the user puts in one checkout, so
        the unpinned restore's "most recent rollout in this cwd" is only this
        session's conversation when it is the only session there; otherwise it
        is somebody else's, and two sessions appending to one transcript is the
        failure this excludes. Exited records count: their conversation is
        still the one a respawn reopens.
        """
        return {
            other.sdef.conversation_id
            for name, other in self._sessions.items()
            if name != except_for and other.sdef.conversation_id
        }

    def _recover_claims(self) -> None:
        """Settle claims whose file appeared after their launch-time wait.

        Session-list and metadata requests already poll the manager, so they
        are a reliable retry point.  Every retry is a single filesystem scan
        (``timeout=0``); a slow harness therefore does not delay the API.
        The pre-command snapshot remains the ownership boundary, and the
        session object identity prevents a stale claim from attaching to a
        later process that reused the same name.
        """
        if not self._pending_claims:
            return
        now = time.monotonic()
        if now < self._next_claim_retry:
            return
        self._next_claim_retry = now + self._claim_interval

        changed = False
        found_any = False
        for name, pending in list(self._pending_claims.items()):
            session = self._sessions.get(name)
            if (
                session is not pending.session
                or session.sdef.conversation_id != pending.previous
            ):
                self._drop_claim(name)
                continue
            try:
                conversation_id = pending.claim(0)
            except Exception:
                log.exception(
                    "%s conversation scan failed for session %r", pending.what, name
                )
                conversation_id = None
            if not conversation_id:
                if now - pending.since >= pending.give_up:
                    self._abandon_claim(pending, now - pending.since)
                continue
            session.sdef = replace(
                session.sdef, conversation_id=conversation_id
            )
            self._drop_claim(name)
            changed = found_any = True
            log.info(
                "discovered delayed %s conversation id for session %r: %s",
                pending.what, name, conversation_id,
            )
        if changed:
            self.persist()
        if not self._pending_claims:
            self._next_claim_retry = 0.0
            self._claim_interval = self._CODEX_CLAIM_RETRY_INTERVAL
        elif not found_any:
            self._claim_interval = min(
                self._claim_interval * 2, self._CODEX_CLAIM_RETRY_MAX
            )

    def _drop_claim(self, name: str) -> None:
        self._pending_claims.pop(name, None)

    def _abandon_claim(self, pending: "_PendingClaim", waited: float) -> None:
        """Stop looking for a conversation file that has not appeared in the
        grace window.

        The session keeps running; only the pin is wrong or missing. For a
        launch claim (nothing pinned yet) a restore falls back to ``--last``
        for its cwd; for a ``/new`` claim the pin stays on the conversation
        the session left, and a restore reopens that one. Said where it will
        be seen: the daemon log names the session and cwd, and the person
        watching the session is told on screen.
        """
        session = pending.session
        name = session.sdef.name
        self._drop_claim(name)
        if pending.previous is None:
            log.warning(
                "gave up discovering the Codex conversation id for session %r "
                "after %.0fs: no rollout records cwd %r (a workspace/cwd "
                "mismatch?); the session keeps running unpinned and a restore "
                "will resume the newest conversation for that cwd",
                name, waited, pending.cwd,
            )
            text = (
                "Codex conversation id not found after "
                f"{int(waited // 60)} min (no rollout for {pending.cwd}); this "
                "session is unpinned — a restore resumes the cwd's newest "
                "conversation"
            )
        else:
            log.warning(
                "gave up discovering the %s conversation for session %r after "
                "%.0fs; the pin stays on %s, the conversation it left, and a "
                "restore reopens that one",
                pending.what, name, waited, pending.previous,
            )
            text = (
                f"the conversation {pending.what} created was not found after "
                f"{int(waited // 60)} min; a restore reopens the previous one"
            )
        try:
            session.notify(text, ttl=60, level="warn")
        except Exception:  # a notice must never break the manager
            log.debug("claim give-up notice for %r failed", name, exc_info=True)

    def discard(self, name: str) -> None:
        """Drop a staged session that will never start."""
        session = self._sessions.get(name)
        if session is not None and getattr(session, "pty", None) is None:
            self._sessions.pop(name, None)
            line_sync.forget(name)

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
        self,
        sdef: SessionDef,
        *,
        restoring: bool = False,
        opening: str = "",
        created_at: Optional[str] = None,
        last_visited_at: Optional[str] = None,
        last_input_at: Optional[str] = None,
        delivery_hold: bool = False,
    ) -> Session:
        """Build and start a session.

        ``opening`` is a first user message for harnesses that take one on
        their command line; see :func:`harness.takes_opening_argv`.
        ``created_at`` is :meth:`stage`'s: the creation time of the session
        this one is continuing, on the relaunch paths — as are the two
        attention stamps beside it.
        """
        session = self.stage(
            sdef,
            restoring=restoring,
            created_at=created_at,
            last_visited_at=last_visited_at,
            last_input_at=last_input_at,
            delivery_hold=delivery_hold,
        )
        try:
            return self.launch(session, restoring=restoring, opening=opening)
        except Exception:
            self.discard(session.sdef.name)
            raise

    async def create_async(
        self, sdef: SessionDef, *, restoring: bool = False, opening: str = "", **stamps
    ) -> Session:
        """:meth:`create` with the process spawn off the event loop;
        ``stamps`` are :meth:`stage`'s keywords."""
        session = self.stage(sdef, restoring=restoring, **stamps)
        try:
            return await self.launch_async(session, restoring=restoring, opening=opening)
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
        self,
        parent: str,
        request: dict,
        *,
        identity: str = "",
        warnings: Optional[List[str]] = None,
        exempt_depth: bool = False,
        place_under: Optional[str] = None,
        created_by: Optional[dict] = None,
    ) -> Session:
        """Register a child of ``parent`` under the spawn policy, unstarted.

        ``place_under`` separates the session the child is BUILT from (always
        ``parent``: its definition, directory and -- for ``fork`` -- its
        conversation) from the one it is RECORDED under. ``None`` keeps them
        the same; a session name hangs the child there instead, and ``""``
        makes it a root. The quick-fork's sibling placement is the one user:
        a copy of ``parent``'s conversation that answers to ``parent``'s own
        parent. The spawn counts (depth, children) are read where the child
        lands, because that is the subtree it grows.

        ``exempt_depth`` is handed to :func:`claude_launcher.spawn.check` as
        the keyword of the same name -- the daemon's own say-so that this one
        child is not counted against ``max_depth``/``max_children``. Never
        read from ``request``; see the policy function for why.

        ``warnings`` is passed straight down to
        :func:`claude_launcher.spawn.check`, which appends to it what the
        policy *allowed but wants said* — a crossed child cap, today. The
        caller owns the list because it is the caller that has somewhere to
        put it: the API turns it into a field of the 201 body.

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
        placed = parent if place_under is None else place_under
        if placed and placed != parent and self.get(placed).exited:
            raise ManagerError(
                f"session {placed!r} has exited — an exited session cannot "
                "take children"
            )
        policy = spawn_mod.SpawnPolicy.load()
        child = spawn_mod.check(
            policy,
            request,
            parent=session.sdef.to_dict(),
            # A root has no parent whose depth or brood it adds to.
            depth=self.depth(placed) if placed else 0,
            children=len(self.live_children(placed)) if placed else 0,
            warnings=warnings,
            exempt_depth=exempt_depth,
        )
        # Settled here rather than left to :meth:`stage`, because the child's
        # worktree is named after the child (``<parent>-<child>-<stamp>``) and
        # the checkout is cut before the record exists. Checked here too: a
        # name already taken must refuse BEFORE a directory is on disk, which
        # is the same rule the worktree's own placement below keeps.
        name = self.settle_name(str(request.get("name") or ""))
        # After the policy and before the record: a checkout is a thing on
        # disk, so it is made only once nothing left can refuse the request.
        child = spawn_mod.make_worktree(child, request, parent=parent, name=name)
        # Same moment, same reason: a fork onto another profile needs the
        # parent's conversation copied into that profile, and only once
        # nothing can refuse the request (claunch-yl0to).
        spawn_mod.carry_forked_conversation(child, request, parent=session.sdef.to_dict())
        return self.stage(
            SessionDef.from_dict(
                {
                    **child,
                    "name": name,
                    "cols": int(request.get("cols") or session.sdef.cols),
                    "rows": int(request.get("rows") or session.sdef.rows),
                    # Role belongs to the mesh membership arranged from the
                    # original request. The session definition keeps no second
                    # copy whose vocabulary or delivery differs by harness.
                    "role": None,
                    "parent": placed or None,
                    # The caller's (SessionDef.created_by), never inherited
                    # from the parent's definition nor read off the request.
                    "created_by": created_by,
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
                    # The board link, when the parent names one outright;
                    # otherwise settled at onboarding from the task text or
                    # minted there (see daemon.beads.Board.ensure_issue).
                    "issue": str(request.get("issue") or ""),
                    # Only a quick-fork carries this, and only with the copy
                    # it names: the field is what unlocks 'merge' back to the
                    # origin, so a child that did not copy that conversation
                    # must not be able to claim it (see daemon.handoff).
                    "quick_fork_of": (
                        str(request.get("quick_fork_of") or "")
                        if request.get("fork") else ""
                    ),
                }
            )
        )

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

    def settle_name(self, name: str = "") -> str:
        """The name a session about to be created will have, checked now.

        For a caller that must name something after the session before its
        record exists -- the worktree cut for it -- so a name already taken
        is refused before anything is on disk. Empty means the next free
        ``sN`` (:meth:`_auto_name`).
        """
        name = (name or "").strip() or self._auto_name()
        self._check_name(name)
        return name

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
        self._recover_claims()
        try:
            return self._sessions[name]
        except KeyError:
            raise ManagerError(f"no session named {name!r}") from None

    def _fresh_created_at(self) -> str:
        """The creation stamp for a session made now, never older than the
        newest one already in the registry.

        The wall clock can step back (a time sync, a VM resuming), and
        :meth:`_by_creation` orders by the stamp, so a session made after
        another would otherwise list ahead of it. Clamping keeps the stamp
        non-decreasing, which leaves registry insertion order to settle ties.
        """
        newest = max((s.created_at or "" for s in self._sessions.values()), default="")
        return max(_utcnow(), newest)

    def _by_creation(self) -> List[Tuple[str, AnySession]]:
        """The registry, oldest session first.

        The one ordering every listing surface takes: ``claunch ls``, the web
        dashboard, the agent-facing tree. Names cannot carry it — they are
        auto-generated with a counter (:meth:`_auto_name`), so a string sort
        files ``s100`` between ``s10`` and ``s11`` and a fleet past nine reads
        as no order at all.

        ``created_at`` is a fixed-width UTC ISO stamp (``timespec="seconds"``,
        always a ``+00:00`` offset), so comparing the strings *is* comparing
        the times — no parsing, and a missing one sorts first as the oldest
        thing the daemon can say about a record it has no stamp for. Second
        resolution means sessions made in the same second tie; ``sorted`` is
        stable, so they keep registry insertion order, which is creation
        order as well.
        """
        return sorted(self._sessions.items(), key=lambda kv: kv[1].created_at or "")

    def list(self) -> List[AnySession]:
        """Every session, live or exited, oldest first (see :meth:`_by_creation`)."""
        self._recover_claims()
        return [session for _, session in self._by_creation()]

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
        """Direct children of ``name``, live or exited, oldest first.

        Siblings are ordered like the top level (:meth:`_by_creation`): a
        listing indents them under their parent, so name order here would
        put the tenth child of a lead ahead of its second just as visibly.
        """
        return [
            n for n, s in self._by_creation() if s.sdef.parent == name and n != name
        ]

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
        return [
            n for n, s in self._by_creation()
            if s.sdef.parent == name and n != name and not s.exited
        ]

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

    def direct_kin(self, a: str, b: str) -> bool:
        """Whether sessions ``a`` and ``b`` are a direct parent/child pair,
        either way round (claunch-mz09h).

        One generation only: a grandparent is not kin here. This is the pair
        the mesh lets talk regardless of mesh wiring, membership or project
        (:meth:`MeshManager._kin_pair`), so it is read from the live tree on
        every ask — a :meth:`detach` ends it at once.
        """
        if not a or not b or a == b:
            return False
        sa, sb = self._sessions.get(a), self._sessions.get(b)
        if sa is None or sb is None:
            return False
        return sa.sdef.parent == b or sb.sdef.parent == a

    def detach(self, child: str, *, actor: str = "") -> dict:
        """Cut ``child`` loose from its parent: it becomes a root.

        The inverse of nothing — there is no re-attach verb beyond
        :meth:`reparent`, which an operator may still use, but nothing undoes
        a detach by itself. The child's own subtree stays under it.

        With an ``actor`` (an agent) the actor must command ``child`` — its
        parent or an ancestor above it — exactly as for :meth:`reparent`;
        a session cannot detach itself. An operator passes none.
        """
        target = self.get(child)
        if actor and actor == child:
            raise ManagerError(
                f"session {child!r} cannot detach itself: a detach is done by "
                "the session that commands it, or by an operator"
            )
        previous = target.sdef.parent
        if not previous:
            raise ManagerError(f"session {child!r} has no parent to detach from")
        if actor:
            self.require_commands(actor, child)
        target.sdef = replace(target.sdef, parent=None)
        self.events.record(target, "detach", "부모-자식 관계 해제",
                           previous=previous, actor=actor or "operator")
        self.persist()
        return {"session": child, "previous": previous}

    def kill(self, name: str, *, force: bool = False,
             by: Optional[dict] = None) -> AnySession:
        """Kill a running session; leave an already-exited one alone.

        Idempotent for every caller, operator included: a second kill of a
        session that has already ended changes nothing and returns the same
        record the first one left. Ending and forgetting are separate verbs —
        this one only ends. :meth:`remove` and :meth:`clear` are the only
        things that drop a record.

        It used to deregister on the second call, which made *repeating* the
        command the destructive act, and a caller repeats a command precisely
        when the first one looked like it had not worked. That cost a record
        that was supposed to stay respawnable once already (via the agent
        route, which has been guarded ever since); the guard is now here,
        where every caller gets it, instead of at each route.
        """
        session = self.get(name)
        if not session.exited:
            actors.note(session, by)
            self._asked_to_end(session)
            session.kill(force=force)
            self.events.record(session, "kill", "세션 종료 요청", force=force,
                               **_known(by=by))
            self.persist()
        return session

    def pause(self, name: str, *, force: bool = False,
              by: Optional[dict] = None) -> AnySession:
        """Pause a running session: the same ending as :meth:`kill`, with the
        record marked ``paused_at`` so it reads as a pause rather than a kill.

        A pause is the operator's temporary stop — a session looping, or two
        of them racing — and the whole of its difference from a kill is in
        the record: the process is terminated the same way, the record stays
        respawnable the same way, and :meth:`respawn` clears the marker by
        constructing a fresh Session. Idempotent like ``kill``: a session
        that has already exited is left as it is, killed or paused.
        """
        session = self.get(name)
        if not session.exited:
            actors.note(session, by)
            self._asked_to_end(session)
            session.pause(force=force)
            self.events.record(session, "pause", "세션 일시 중지 요청", force=force,
                               **_known(by=by))
            self.persist()
        return session

    def unpause(self, name: str, *, by: Optional[dict] = None) -> bool:
        """File a paused record as killed: the operator's kill of a pause.

        The process ended at the pause, so the only thing left for a kill to
        change is the record. Clearing ``paused_at`` moves it from the rail's
        Paused filter to Killed, and out of the bulk "resume the paused" set —
        a session the operator has decided is finished must not come back from
        a button that said *resume the paused*. Anything else is left alone: a
        running session (that is :meth:`kill`'s job) and an exited one that was
        never paused. Returns whether the marker was cleared.

        Only the operator's kill route calls this. :meth:`kill` itself stays a
        no-op on every exited record, because the daemon's own callers (the
        kill-all pass, a handoff's source) reach it without meaning to refile
        a pause.
        """
        session = self.get(name)
        if not session.exited or not getattr(session, "paused_at", None):
            return False
        session.paused_at = None
        # The record now reads as this kill, so it names this kill's asker.
        if by:
            session.ended_by = dict(by)
        self.events.record(session, "kill", "일시 중지된 세션을 종료로 전환", **_known(by=by))
        self.persist()
        return True

    # ---- hold (claunch-5l35i) ------------------------------------------------ #

    @staticmethod
    def resumable(session: AnySession) -> bool:
        """Whether a respawn brings this session back *with its conversation*.

        The precondition of a hold: freeing the process is only harmless when
        the wake reopens the same conversation. Claude with a pinned id and a
        transcript behind it, codex with a resolved rollout; anything else (a
        shell, pi, a claude that has not written its first line yet) would
        come back blank, so it is never held.
        """
        return SessionManager._resumable_def(session.sdef)

    @staticmethod
    def _resumable_def(sdef: SessionDef) -> bool:
        """:meth:`resumable` asked of a definition (no session object yet)."""
        if not sdef.conversation_id:
            return False
        if sdef.harness == harness_mod.CLAUDE_HARNESS:
            return not harness_mod.restores_blank(sdef)
        return sdef.harness == "codex"

    def hold(self, name: str, *, force: bool = False, reason: str = "",
             by: Optional[dict] = None) -> AnySession:
        """Free a running session's program and file the record as held.

        The session stays what it was everywhere else — its issue, its mesh
        row, its runs — and comes back through :meth:`wake_async`. Refuses a
        session :meth:`resumable` says would come back blank.
        """
        session = self.get(name)
        if session.exited:
            return session
        if not self.resumable(session):
            raise ManagerError(
                f"session {name!r} cannot be held: a respawn would not reopen "
                "its conversation"
            )
        actors.note(session, actors.with_reason(by, reason))
        self._asked_to_end(session)
        session.hold(force=force)
        self.events.record(session, "hold", "세션 hold 요청", force=force,
                           reason=reason, **_known(by=by))
        self.persist()
        return session

    def unhold(self, name: str, *, by: Optional[dict] = None) -> bool:
        """Turn a hold into a real ending: the operator killed (or archived)
        a held record. The exit hooks the hold skipped run now — the board
        sweep returns its issue, side runs end — and the record reads killed.
        Returns whether the marker was cleared.

        A delayed record (a hold the restore queue owns) is settled the same
        way. So is a failed one (claunch-0j6gb.1), except that its exit hooks
        already ran at the exit and only the board sweep it put off is owed:
        that is :attr:`settle_hooks`."""
        session = self.get(name)
        if not session.exited:
            return False
        held = getattr(session, "held_at", None)
        failed = getattr(session, "failed_at", None)
        if not held and not failed:
            return False
        delayed = getattr(session, "delayed_at", None)
        session.held_at = None
        session.failed_at = None
        if hasattr(session, "delayed_at"):
            session.delayed_at = None
        self._delayed_busy.discard(name)
        # A hold freed the program and the asker now ends the session, so
        # the record names them. A failure's ending stays its own: the
        # program died by itself, and settling the record does not change
        # who ended it.
        if held and by:
            session.ended_by = dict(by)
        self.events.record(
            session, "kill",
            "실패한 세션을 종료로 전환" if failed and not held
            else "대기 중인 세션을 종료로 전환" if delayed
            else "hold된 세션을 종료로 전환",
            **_known(by=by),
        )
        self.persist()
        if held:
            self._run_exit_hooks(session)
        else:
            for hook in list(self.settle_hooks):
                try:
                    hook(session)
                except Exception:  # one hook must not silence the next
                    log.exception("settle hook %r failed for %r", hook, name)
        return True

    async def wake_async(self, name: str, *, by: str = "") -> AnySession:
        """Bring a held session back (respawn with its conversation).

        Anything else is returned as it is: a live session is already awake,
        and a killed or paused record is not this verb's to revive. Concurrent
        wakes of one name (a viewer and a mesh delivery at once) share one
        respawn. A delayed record whose relaunch raises is filed failed
        (:meth:`fail_delayed`) before the error is re-raised.
        """
        lock = self._wake_locks.setdefault(name, asyncio.Lock())
        async with lock:
            session = self.get(name)
            if not getattr(session, "held_at", None):
                return session
            # hold marks the record before the asynchronous process exit.
            # Messages in that interval must wait rather than enter the dying PTY.
            if not session.exited:
                await session.wait_for("exited", timeout=10.0, threshold=self.idle_threshold)
                session = self.get(name)
                if not session.exited or not getattr(session, "held_at", None):
                    return session
            delayed = bool(getattr(session, "delayed_at", None))
            self._waking.add(name)
            try:
                relaunched = await self.respawn_async(name)
            except Exception as exc:
                # A delayed record whose relaunch raised is failed, whoever
                # woke it (claunch-f9ene). Left delayed, nothing would try it
                # again: the queue passes a record it finds mid-wake (it is
                # out of the registry then), and a use-wake does not retry.
                if delayed:
                    self.fail_delayed(name, reason=str(exc))
                raise
            finally:
                self._waking.discard(name)
            if by in AUTOMATIC_WAKES:
                # _respawned marked a person's respawn; nobody asked for this.
                relaunched.resumed_by_human = False
            if delayed:
                self._delayed_woken(relaunched, by)
            else:
                log.info("held session %r woken (%s)", name, by or "respawn")
            return relaunched

    def _delayed_woken(self, session: Session, by: str) -> None:
        """Book-keeping for a delayed record that just came back."""
        name = session.sdef.name
        self._delayed_busy.discard(name)
        log.info("delayed session %r started (%s)", name, by or "respawn")
        for hook in list(self.delayed_wake_hooks):
            try:
                hook(session)
            except Exception:  # one hook must not silence the next
                log.exception("delayed wake hook %r failed for %r", hook, name)

    def fail_delayed(self, name: str, reason: str = "") -> bool:
        """File a delayed record whose relaunch raised as *failed*
        (claunch-0j6gb.1): the queue will not try it again, and a person
        reads why instead of a record that stays "delayed" forever. Its
        exit hooks run now, as a failure's do — all but the board sweep,
        which waits for the person settling it. Returns whether the record
        was delayed."""
        session = self.get(name)
        if not session.exited or not getattr(session, "delayed_at", None):
            return False
        session.held_at = None
        session.delayed_at = None
        session.failed_at = _now_iso()
        self._delayed_busy.discard(name)
        self.events.record(session, "fail", "대기 중이던 세션 복원 실패",
                           reason=reason)
        self.persist()
        # A delayed record is a hold, so none of its ending ran; now it has
        # ended, as a failure — everything but the board sweep.
        self._run_exit_hooks(session)
        return True

    def is_waking(self, name: str) -> bool:
        """Whether :meth:`wake_async` is relaunching ``name`` right now."""
        return name in self._waking

    def message_restore_pending(self, name: str) -> bool:
        """Keep staged recipients on the same FIFO as the first wake message."""
        return self.is_waking(name) or name in self._wake_messages

    def queue_message(self, name: str, text: str, *, by: str = "message") -> bool:
        """Keep notifications in arrival order across replacement staging."""
        session = self.get(name)
        if not getattr(session, "held_at", None) and not self.message_restore_pending(name):
            if session.exited:
                return False
            with delivery_wake(by):
                return session.queue_delivery(text)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._wake_messages.setdefault(name, []).append(text)
        if name in self._wake_message_tasks:
            if by not in AUTOMATIC_WAKES:
                # A notice started the wake under way; a person's message
                # joining it still makes this a person's incarnation.
                self._wake_message_people.add(name)
            return True

        async def run() -> None:
            try:
                held = self.get(name)
                current = await self.wake_async(name, by=by)
                # Only a relaunch is anyone's incarnation: a record that was
                # merely restoring comes back as the same object.
                if name in self._wake_message_people and current is not held:
                    current.resumed_by_human = True
                if current.exited:
                    return
                # No await between removing the restore FIFO and transferring
                # it to the live FIFO: incoming calls cannot overtake it.
                for message in self._wake_messages.pop(name, []):
                    current.queue_delivery(message)
            except Exception:  # noqa: BLE001 -- acceptance is not a delivery receipt
                log.exception("message wake of held session %r failed", name)
            finally:
                self._wake_messages.pop(name, None)
                self._wake_message_tasks.pop(name, None)
                self._wake_message_people.discard(name)

        task = loop.create_task(run())
        self._wake_message_tasks[name] = task
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)
        return True

    def wake_soon(self, name: str, *, by: str = "", deliver: str = "") -> bool:
        """Schedule :meth:`wake_async` from synchronous code on the loop, and
        queue ``deliver`` into the woken session. Returns whether a wake was
        scheduled (False when the record is not held or no loop is running)."""
        try:
            session = self.get(name)
        except ManagerError:
            return False
        if deliver:
            if getattr(session, "held_at", None) or self.message_restore_pending(name):
                return self.queue_message(name, deliver, by=by)
            return False
        if not getattr(session, "held_at", None):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        async def run() -> None:
            try:
                await self.wake_async(name, by=by)
            except Exception:  # noqa: BLE001 -- held stays held; delayed is now failed
                log.exception("wake of held session %r failed", name)
                return

        task = loop.create_task(run())
        self._wake_tasks.add(task)
        task.add_done_callback(self._wake_tasks.discard)
        return True

    def escalate_children(self, name: str) -> List[str]:
        """Move ``name``'s direct children up to ``name``'s own parent.

        The step every record-dropping path takes before the ``del``. The tree
        is derived from ``SessionDef.parent`` on every walk (see above), so a
        record that disappears leaves its children naming a session that is no
        longer there — :meth:`ancestors` stops at the first missing name, and
        the whole subtree silently becomes a set of roots. Nothing reports
        that: the grandchildren are still running, still hold conversations
        and worktrees and cflow runs, and the session that used to command
        them (the grandparent) no longer does.

        Promoting them one level up keeps the edge that existed in fact — a
        lead's worker's worker still answers to the lead — instead of leaving
        it to be re-drawn by hand. The move is always *shallower*, so no depth
        check is needed, and the new parent is an ancestor of the children, so
        no cycle can be made. A grandparent that is itself gone (or absent —
        ``name`` was a root) makes them roots, which is what they would have
        become anyway.

        Returns the children moved, oldest first. The mesh edge to the new
        parent is the caller's, exactly as it is for :meth:`reparent`
        (:meth:`MeshManager.link_lineage`); this class knows nothing of meshes.
        """
        session = self._sessions.get(name)
        above = session.sdef.parent if session else None
        if above not in self._sessions or above == name:
            above = None
        moved = self.children(name)
        for child in moved:
            target = self._sessions[child]
            target.sdef = replace(target.sdef, parent=above)
        return moved

    def remove(
        self, name: str, *, children: str = "escalate"
    ) -> Tuple[AnySession, List[str]]:
        """Drop one exited session's record — the per-session half of
        :meth:`clear`.

        A running session is refused outright: forgetting is not how anything
        ends, and a route that reached for this on a live session has the
        verbs confused. The mesh guard is the caller's, for the reason
        :meth:`clear` spells out — this class knows nothing of meshes.

        ``children`` decides what happens to the subtree below ``name``, and
        the two answers are the two things an operator can mean:

        * ``escalate`` (default) — the children move up to ``name``'s own
          parent (:meth:`escalate_children`) and keep running. This is the
          answer that loses nothing.
        * ``remove`` — every descendant's record is dropped along with it.
          Refused, with nothing dropped, if any of them is still running:
          the same rule as ``name`` itself, applied to the whole subtree, so
          a cascade cannot become an accidental mass end-of-session.

        Returns the removed record and the names of the sessions the choice
        touched: the children promoted, or the descendants dropped.
        """
        if children not in ("escalate", "remove"):
            raise ManagerError(
                f"unknown children policy {children!r} — 'escalate' (move them "
                "up to the removed session's own parent) or 'remove' (drop "
                "their records too)"
            )
        session = self.get(name)
        if not session.exited:
            raise ManagerError(
                f"session {name!r} is still running — kill it first"
            )
        # Forgetting a held record is its real ending: settle what it owed.
        self.unhold(name)
        if children == "remove":
            below = self.descendants(name)
            running = [n for n in below if not self._sessions[n].exited]
            if running:
                raise ManagerError(
                    f"{name!r} has {len(running)} running session(s) under it "
                    f"({', '.join(running)}) — kill them first, or remove "
                    f"{name!r} on its own and let them move up to its parent"
                )
            for child in below:
                del self._sessions[child]
                line_sync.forget(child)
            self.progress.forget(below)
            touched = below
        else:
            touched = self.escalate_children(name)
        del self._sessions[name]
        # The shared session line lives beside the record, not in it; a name
        # created again later must not open on the deleted session's line.
        line_sync.forget(name)
        # Same for the progress it published.
        self.progress.forget([name])
        # Removing the last record leaves the set empty on purpose, so let this
        # one persist prune the store to empty past the empty-snapshot guard.
        self._allow_empty_persist = True
        self.persist()
        return session, touched

    def set_keep_alive(self, name: str, on: bool) -> AnySession:
        """Set (or clear) a session's keep-alive flag.

        The flag is read by the run-event clock right beside its kill: a
        finished one-shot run whose driving session carries it gets the
        durable ending record but not the termination — a user asked for that
        session's context to stay. The session itself sets it on a user's
        "don't close me", and the operator clears it when that ends.
        """
        session = self.get(name)
        session.sdef = replace(session.sdef, keep_alive=bool(on))
        self.persist()
        return session

    def set_self_merge_allowed(self, name: str, on: bool, *, actor: str = "") -> AnySession:
        """Set (or clear) whether a quick-fork may merge on its own.

        Only a quick-fork carries the flag, and the fork itself may not set
        it: the flag stands in for the operator's merge press (see
        :mod:`claude_launcher.daemon.handoff`).
        """
        session = self.get(name)
        if not session.sdef.quick_fork_of:
            raise ManagerError(
                f"session {name!r} is not a quick-fork: only a quick-fork's merge "
                "waits for the operator, so there is nothing to allow"
            )
        if actor and actor == name:
            raise ManagerError(
                f"session {name!r} cannot allow its own merge: that is the "
                "operator's choice, made from the web UI"
            )
        session.sdef = replace(session.sdef, self_merge_allowed=bool(on))
        self.persist()
        return session

    def set_observe_pin(self, name: str, on: bool) -> AnySession:
        """Set (or clear) whether the observer's pinned-only scope covers this.

        Written to the definition rather than to the observer's own settings
        because two views read the same value — the observer card and the
        session rail — and the rail's poll is the session list (see
        :attr:`SessionDef.observe_pin`). The observer reads it live from the
        session it is already iterating, so flipping it takes effect on the
        next pass with no wake: a session that just left the scope is skipped,
        and one that just entered it is observed on the loop's own schedule.
        """
        session = self.get(name)
        session.sdef = replace(session.sdef, observe_pin=bool(on))
        self.persist()
        return session

    def set_note(self, name: str, note: str) -> AnySession:
        """Set (or, with an empty value, clear) a session's user note.

        The note is the *person's* annotation on a terminal, so it is written
        straight to the definition and persisted there; nothing reads it back
        into the session itself (see :attr:`SessionDef.note`). An empty or
        whitespace-only value clears it, which is what makes the same call the
        editor's save and its "remove" — the field is either there or absent,
        and there is no empty-string note to render.
        """
        session = self.get(name)
        text = str(note or "").strip()
        if len(text) > MAX_NOTE:
            raise ValueError(f"a note is at most {MAX_NOTE} characters")
        session.sdef = replace(session.sdef, note=text or None)
        self.persist()
        return session

    def set_reminder_note(self, name: str, note: str) -> AnySession:
        """Set (or, with an empty value, clear) a session's temporary
        instructions (:attr:`SessionDef.reminder_note`).

        The same shape as :meth:`set_note` -- trimmed, capped at
        :data:`MAX_NOTE`, empty clears -- but refused on an exited session:
        this text exists to be delivered, and an exited session receives no
        reminder.
        """
        session = self.get(name)
        if getattr(session, "exited", False):
            raise ValueError(f"session {name!r} has exited")
        text = str(note or "").strip()
        if len(text) > MAX_NOTE:
            raise ValueError(
                f"temporary instructions are at most {MAX_NOTE} characters"
            )
        session.sdef = replace(session.sdef, reminder_note=text or None)
        self.persist()
        return session

    def set_cflow_declined(
        self, name: str, *, workflow: str = "", reason: str = "", by: str = ""
    ) -> AnySession:
        """Record that this session's user declined a cflow run for it
        (:attr:`SessionDef.cflow_declined`). Recording again replaces the
        earlier answer; :meth:`clear_cflow_declined` withdraws it."""
        session = self.get(name)
        record = {
            "workflow": str(workflow or "").strip(),
            "reason": str(reason or "").strip()[:MAX_NOTE],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": str(by or "").strip(),
        }
        session.sdef = replace(session.sdef, cflow_declined=record)
        self.persist()
        return session

    def clear_cflow_declined(self, name: str) -> AnySession:
        """Withdraw a recorded refusal -- the user changed their mind. Not
        called on a run start: the session is told to run ``claunch cflow
        decline --clear`` before it starts one."""
        session = self.get(name)
        if session.sdef.cflow_declined is not None:
            session.sdef = replace(session.sdef, cflow_declined=None)
            self.persist()
        return session

    def set_model(self, name: str, model: str) -> AnySession:
        """Set the model the session's *next* launch will be started on.

        The running program is not touched -- a harness picks its model at
        startup, so this takes effect at the next restore or respawn, the same
        way the creation-time choice did.

        :meth:`reconcile_models` covers the ordinary case on its own by
        following what the harness answers on. This is the lever for what it
        cannot follow: a session that has not taken a turn yet (no reading to
        read), a model id the registry does not map, and a deliberate "bring
        it back on something else next time".

        The choice is checked against the harness's declared models, so an
        unknown one is refused here rather than at the relaunch that would
        otherwise fail long after the person typed it. An empty value clears
        the choice, which puts the session back on the harness default.
        """
        session = self.get(name)
        sdef = session.sdef
        chosen = str(model or "").strip()
        if chosen:
            entry = harness_registry.get(sdef.harness)
            if entry is None or not entry.models:
                raise harness_mod.HarnessError(
                    f"harness {sdef.harness!r} does not declare selectable models"
                )
            if chosen not in entry.models:
                raise harness_mod.HarnessError(
                    f"unknown model {chosen!r} for harness {sdef.harness!r} "
                    f"(known: {', '.join(entry.models)})"
                )
            if harness_mod.steers_model(sdef.args):
                raise harness_mod.HarnessError(
                    "this session's extra args already select a model; the "
                    "saved choice would not win"
                )
        session.sdef = replace(sdef, model=chosen or None)
        # A person naming the model outranks what the last reading said, so
        # the unmapped-id note it may have left stops being the open question.
        session.unmapped_model_id = None
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

        Children are escalated exactly as they are by :meth:`remove` — a live
        session whose exited parent is cleared here would otherwise be left
        naming a record nobody can respawn. The drop list is walked
        ancestors-first (shallowest depth first, measured before anything is
        deleted) so a chain of cleared records passes its live grandchildren
        all the way up to the first session that survives the clear.
        """
        spared = set(keep)
        names = [
            name
            for name, s in self._sessions.items()
            if s.exited and name not in spared
        ]
        for name in sorted(names, key=self.depth):
            self.escalate_children(name)
        self.progress.forget(names)
        for name in names:
            del self._sessions[name]
            line_sync.forget(name)
            if logs:
                shutil.rmtree(paths.session_dir(name), ignore_errors=True)
        # A clear that drops every exited record legitimately empties the set;
        # let this persist prune the store to empty past the empty-snapshot
        # guard, which otherwise keeps an unasked-for empty write from erasing.
        self._allow_empty_persist = True
        self.persist()
        return names

    def respawn(self, name: str) -> Session:
        """Relaunch an exited session under its original definition.

        Restore semantics apply: conversation-aware harnesses come back with
        the conversation id pinned at creation (Claude) or discovered just
        after it (Codex), so quitting the program by accident is recoverable
        — same conversation, same session name. Works just as well on a record
        that outlived the daemon that spawned it.
        """
        session = self._respawn_take(name)
        try:
            relaunched = self.create(**self._respawn_args(session))
        except Exception:
            self._sessions[name] = session  # keep the exited record on failure
            raise
        return self._respawned(session, relaunched)

    async def respawn_async(self, name: str) -> Session:
        """:meth:`respawn` with the process spawn off the event loop."""
        session = self._respawn_take(name)
        try:
            relaunched = await self.create_async(**self._respawn_args(session))
        except Exception:
            self._sessions[name] = session  # keep the exited record on failure
            raise
        return self._respawned(session, relaunched)

    def _respawn_take(self, name: str) -> AnySession:
        session = self.get(name)
        if not session.exited:
            raise ManagerError(
                f"session {name!r} is still running (attach to it, or kill it first)"
            )
        del self._sessions[name]
        return session

    @staticmethod
    def _respawn_args(session: AnySession) -> dict:
        return dict(
            sdef=session.sdef,
            restoring=True,
            created_at=session.created_at,
            last_visited_at=session.last_visited_at,
            last_input_at=session.last_input_at,
            delivery_hold=session.delivery_held(),
        )

    def _respawned(self, session: AnySession, relaunched: Session) -> Session:
        # A person asked for this incarnation, by name, right now -- unlike
        # restore_all's unattended relaunch after a daemon restart. See
        # Session.resumed_by_human for what this buys the session.
        relaunched.resumed_by_human = True
        action = "resume" if session.paused_at else "respawn"
        if getattr(session, "delayed_at", None):
            self.events.record(relaunched, "resume", "대기 중이던 세션 복원")
            return relaunched
        if getattr(session, "held_at", None):
            self.events.record(relaunched, "wake", "hold된 세션 복귀")
            return relaunched
        self.events.record(relaunched, action,
                           "일시 중지된 세션 재개" if action == "resume" else "세션 재실행")
        return relaunched

    def archive(self, name: str, *, reason: Optional[str] = None) -> AnySession:
        """Move an exited record out of the working fleet while retaining it.

        The definition, conversation id, output log and lineage remain in
        place, so archive is reversible through :meth:`respawn`. Repeating
        the operation is idempotent and preserves the original archive time.

        This is the filing half alone, so it refuses a session that is still
        running: a record written as archived while its program runs would
        describe something that is not true yet. :meth:`stop_and_archive` is
        the verb for a live session, and every operator route reaches for
        that one — this one is what the bulk "archive the exited ones" pass
        calls, where the refusal is the selection working.

        ``reason`` lands on the archive event (``auto-archive`` for the
        clock in :mod:`.auto_archive`), so the history tells it apart from a
        person's archive.
        """
        session = self.get(name)
        if not session.exited:
            raise ManagerError(
                f"session {name!r} is still running — "
                "archive it with stop_and_archive, which ends it first"
            )
        # Filing a held record away is its real ending: settle what it owed.
        self.unhold(name)
        if not session.archived_at:
            session.archived_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            self.persist()
            self.events.record(session, "archive", "세션 보관",
                               **({"reason": reason} if reason else {}))
        return session

    async def stop_and_archive(
        self, name: str, *, force: bool = False, by: Optional[dict] = None
    ) -> AnySession:
        """Archive a session whatever state it is in, ending it first if it
        is still running.

        :meth:`archive` files a record that has already ended, so archiving a
        live session used to be three operator steps: kill it, wait for the
        exit to land, archive it. The middle step is the one that does not
        work by hand — :meth:`Session.kill` only signals the child, and the
        record turns ``exited`` later, when the reader task sees EOF, so an
        archive pressed straight after a kill finds the session still running
        and is refused. That is the whole reason archive read as unavailable
        on a live session, and it is why this is one verb rather than a note
        in the documentation telling the operator to try again in a moment.

        The stop is :meth:`Session.shutdown`, the same primitive
        :meth:`redefine` stops a session with: it terminates the child, waits
        for the exit to land, escalates to SIGKILL, and marks the record
        itself if even that is ignored. So the call returns with the session
        actually ended and the archive time written in the same breath.
        ``force`` goes straight to the escalation instead of waiting out the
        grace period.

        The board wind-down is not consulted here, and that is deliberate: it
        belongs to the kill route, where a first press means "let it settle
        its issues, then stop". A session on its way to the archive has no
        turn left to settle anything in, so asking for one would leave the
        operator waiting for a reply to a request that has already been
        answered. Its issues are released by the exit path's own sweep,
        exactly as they are for any other ending.
        """
        session = self.get(name)
        if not session.exited:
            self.events.record(
                session, "kill", "보관을 위한 세션 종료",
                force=force, reason="archive", **_known(by=by),
            )
            actors.note(session, actors.with_reason(by, "archive"))
            await session.shutdown(grace=0.0 if force else 5.0)
            self.persist()
        return self.archive(name)

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
        with self._restart_exit_hooks(session):
            if not session.exited:
                await session.shutdown()
            return self._recreate(name, session, new)

    @contextmanager
    def _restart_exit_hooks(self, session: AnySession):
        """Defer permanent-exit effects until a restart's outcome is known.

        Success keeps the session's work; failure (including cancellation)
        releases it if the old process ended. An exit arriving after a failed
        shutdown is handled normally once this guard has been removed.
        """
        if id(session) in self._restarting:
            raise ManagerError(f"session {session.sdef.name!r} is already restarting")
        self._restarting[id(session)] = False
        try:
            yield
        except BaseException:
            if self._restarting.pop(id(session)):
                self._run_exit_hooks(session)
            raise
        finally:
            self._restarting.pop(id(session), None)

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
            return self.create(
                new_def,
                restoring=True,
                created_at=session.created_at,
                last_visited_at=session.last_visited_at,
                last_input_at=session.last_input_at,
                delivery_hold=session.delivery_held(),
            )
        except Exception:
            self._sessions[name] = session  # keep the record, as it was
            self.persist()
            raise

    async def reborrow(
        self, name: str, borrow: Optional[str], *, null_token: bool = False,
        disable_artifact_tool: Optional[bool] = None,
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
        made at creation. Claude's Artifact-tool setting is persisted in the
        same definition and can be changed in the same restart; the token is
        still looked up fresh at every relaunch.

        Refused while nothing has been stopped: a harness with no shared-token
        route, a borrow paired with ``--null`` (creation's own refusal — the
        two flags answer "whose token" with opposite answers), an unknown or
        qualified lender, and a no-op. ``--null`` itself remains Claude-only.
        """
        session = self.get(name)
        old = session.sdef
        entry = harness_registry.get(old.harness)
        if entry is None:
            raise ManagerError(f"unknown harness {old.harness!r}")
        artifact_tool = (
            old.disable_artifact_tool
            if disable_artifact_tool is None else disable_artifact_tool
        )
        if artifact_tool and old.harness != harness_mod.CLAUDE_HARNESS:
            raise ManagerError(
                "the Artifact tool setting only applies to the claude harness"
            )
        auth_changed = borrow != old.borrow or null_token != old.null_token
        artifact_changed = artifact_tool != old.disable_artifact_tool
        if auth_changed and not entry.borrowable:
            raise ManagerError(
                f"--borrow is not supported by harness {old.harness!r}; "
                "OAuth harnesses use their profile's own namespaced login"
            )
        lender = (borrow or "").strip() or None
        if lender is not None and null_token:
            raise ManagerError(
                "--null launches without any OAuth token; it cannot be "
                f"combined with --borrow {lender}"
            )
        if null_token and old.harness != harness_mod.CLAUDE_HARNESS:
            raise ManagerError(
                f"--null only applies to the claude harness, not {old.harness!r}"
            )
        if lender is not None:
            # Checked now rather than left for the relaunch to discover: a
            # typo must cost a 400, not a stopped session. (The token itself
            # is *not* required — same terms as creation, where a tokenless
            # lender simply starts unauthenticated.)
            try:
                runtime = profile_mod.require_selector(old.profile or "")
                resolved, _report = borrowing.require_allowed(
                    runtime, lender, entry=entry
                )
                lender = resolved.name
            except (profile_mod.ProfileError, borrowing.BorrowError) as exc:
                raise ManagerError(str(exc)) from exc
        if not auth_changed and not artifact_changed:
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
        relaunched = await self.redefine(
            name, borrow=lender, null_token=null_token,
            disable_artifact_tool=artifact_tool,
        )
        if auth_changed:
            details = {
                "previous": old.borrow, "current": lender,
                "previous_null": old.null_token, "null_token": null_token,
            }
            if artifact_changed:
                details.update({
                    "previous_disable_artifact_tool": old.disable_artifact_tool,
                    "disable_artifact_tool": artifact_tool,
                })
            self.events.record(
                relaunched, "borrow", "세션 인증 프로파일 변경", **details
            )
        if artifact_changed:
            self.events.record(
                relaunched, "restart-settings", "Artifact 도구 설정 변경",
                previous_disable_artifact_tool=old.disable_artifact_tool,
                disable_artifact_tool=artifact_tool,
            )
        return relaunched

    async def skip_permissions(self, name: str, skip: bool) -> Session:
        """Restart a session with permission prompts off — or back on.

        The third answer :meth:`redefine` restarts for, after migrate's
        where and reborrow's whose-token: whether the harness asks before it
        acts. The harness declaration supplies the argv, so the toggle is an
        args edit — appended once, or taken out — and everything else about
        the session (conversation, directory, auth) is untouched.

        Refused while nothing has been stopped: a harness without this
        capability, and a no-op. Only the declared argv group is ever
        touched — a session started with its own extra args keeps them.
        """
        session = self.get(name)
        old = session.sdef
        entry = harness_mod.harness_registry.get(old.harness)
        flags = tuple(entry.skip_permissions_args) if entry else ()
        if not flags:
            raise ManagerError(
                f"harness {old.harness!r} does not declare a skip-permissions mode"
            )
        width = len(flags)
        has = any(tuple(old.args[i:i + width]) == flags
                  for i in range(len(old.args) - width + 1))
        if has == skip:
            raise ManagerError(
                f"session {name!r} already skips permission prompts"
                if skip
                else f"session {name!r} is not skipping permission prompts "
                     "— nothing to turn back on"
            )
        if has:
            args_list = list(old.args)
            for i in range(len(args_list) - width + 1):
                if tuple(args_list[i:i + width]) == flags:
                    del args_list[i:i + width]
                    break
            args = tuple(args_list)
        else:
            args = (*old.args, *flags)
        return await self.redefine(name, args=args)

    async def runtime_mode(self, name: str, yolo: bool, sandbox: bool) -> Session:
        """Restart a session with its YOLO and Sandbox switches rewritten.

        The detail panel's pair for a harness that declares a sandbox (Codex):
        approvals and the sandbox are separate options there, and turning one
        while keeping the other needs both written at once — the skip group
        alone, appended by :meth:`skip_permissions`, would drop the packaged
        bypass default and leave the sandbox to whatever Codex defaults to.
        So the session's own args are rewritten by :func:`codex_mode.
        runtime_args`, the rule the create forms and the wizard use, and the
        session relaunches into its conversation like the other restarts.

        Refused while nothing has been stopped: a harness that declares no
        sandbox pair, and a no-op (the switches already read that way).
        """
        session = self.get(name)
        old = session.sdef
        entry = harness_mod.harness_registry.get(old.harness)
        capabilities = entry.to_dict() if entry else {}
        if not codex_mode.declares_runtime(capabilities):
            raise ManagerError(
                f"harness {old.harness!r} declares no sandbox switch"
            )
        if codex_mode.runtime_state(old.args, capabilities) == (yolo, sandbox):
            raise ManagerError(
                f"session {name!r} already runs with YOLO "
                f"{'on' if yolo else 'off'} and the sandbox "
                f"{'on' if sandbox else 'off'}"
            )
        args = tuple(codex_mode.runtime_args(
            old.args, capabilities, yolo=yolo, sandbox=sandbox
        ))
        return await self.redefine(name, args=args)

    async def auto_compact(
        self, name: str, enabled: bool, window: Optional[int]
    ) -> Session:
        """Restart a claude session with auto-compact switched and re-pinned.

        Claude reads both answers from its environment at launch — the
        on/off switch (``DISABLE_AUTO_COMPACT``) and the threshold
        (``CLAUDE_CODE_AUTO_COMPACT_WINDOW``) — so changing either is an edit
        of the definition's own env and a relaunch, like
        :meth:`skip_permissions` is of its args. ``window=None`` drops the
        session's pin and returns the threshold to the profile's
        ``auto_compact_at``. The env is the spawn's last layer, so the answer
        overrides the profile's and survives a daemon restart.

        Refused while nothing has been stopped: another harness (the switch
        is claude's), a window that is not a positive integer, and a no-op.
        """
        session = self.get(name)
        old = session.sdef
        if old.harness != harness_mod.CLAUDE_HARNESS:
            raise ManagerError(
                f"auto-compact is claude's switch; session {name!r} runs "
                f"{old.harness!r}"
            )
        try:
            enabled, window = auto_compact_mod.parse(
                {"enabled": enabled, "window": window}
            )
        except ValueError as exc:
            raise ManagerError(str(exc)) from exc
        env = auto_compact_mod.with_auto_compact(
            old.env, enabled=enabled, window=window
        )
        if env == dict(old.env):
            raise ManagerError(
                f"session {name!r} already runs with that auto-compact setting"
            )
        return await self.redefine(name, env=env)

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
                config_dir = profile_mod.require_selector(old.profile).config_dir
            except profile_mod.ProfileError as exc:
                raise ManagerError(str(exc)) from exc
        with self._restart_exit_hooks(session):
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
                    # Restore the transcript alongside the old definition.
                    transcripts.relocate(
                        config_dir, old.conversation_id, new_cwd, old.cwd
                    )
                raise
        self.events.record(relaunched, "worktree", "세션 작업 디렉터리 이동",
                           previous=old.cwd, current=new_cwd,
                           transcript_moved=moved is not None)
        return relaunched, moved is not None

    async def shutdown_all(self, mode: str = "stop", *, reason: str = "stop") -> None:
        """End every live session, the daemon going down.

        ``mode`` is the restart mode (claunch-xnnct.1): ``"keep"`` lets go of
        the sessions that run in the PTY host and leaves them running for
        the next daemon to adopt (:meth:`restore_all`), ending only the ones
        on a local PTY; ``"full"`` and ``"stop"`` end them all, as without a
        host. The host process itself is the caller's to close or shut down.

        ``reason`` (``"restart"`` or ``"stop"``) is what the sessions this
        ends are filed as having been ended for: their ``ended_by`` is the
        daemon with that reason. A session the next boot restores starts a
        new incarnation and does not show it; one it does not restore (a
        ``--no-restore`` session, a relaunch that failed) keeps it, and so
        says who ended it (claunch-5aysr.1).
        """
        self.shutting_down = True  # these exits are the daemon's, not the sessions'
        self._ended_by_shutdown = {
            name: s for name, s in self._sessions.items()
            if not s.exited and not getattr(s, "kill_requested", False)
        }
        # A restart immediately after /new must not persist the superseded
        # UUID while its short filesystem watcher is still in flight.
        pending = list(self._codex_switch_tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # ...and a claim the listing polls have not been asked about since the
        # file appeared (a pi /new answered while nobody had the dashboard
        # open) gets one last scan, so the restore reopens the right one.
        self._next_claim_retry = 0.0
        self._recover_claims()
        self.persist()  # record which sessions were alive, for restore
        # Concurrently, not one after another: each shutdown waits up to its
        # grace window for the child to go, so a serial loop over N live
        # sessions held the singleton lock for about N x grace seconds. With
        # 18 sessions that was 90s+ -- longer than a restarting successor's
        # whole retry budget, which lost the lock race and left no daemon
        # running (claunch-a5l9, 2026-09-10 17:31). Together, the drain is
        # bounded by one grace window whatever the session count.
        live = [s for s in self._sessions.values() if not s.exited]
        started = time.monotonic()
        if mode == "keep":
            records = await asyncio.gather(
                *(session.detach() for session in live), return_exceptions=True
            )
            detached = [s for s, r in zip(live, records) if isinstance(r, dict)]
            for s, r in zip(live, records):
                if isinstance(r, BaseException):
                    log.warning("session %r could not be detached (%s); ending it",
                                s.sdef.name, r)
            if detached:
                # Again, now with where each log ends: what adopt starts from.
                self.persist()
                log.info(
                    "left %d session(s) running in the PTY host in %.1fs",
                    len(detached), time.monotonic() - started,
                )
            live = [s for s in live if s not in detached]
        by = actors.make(actors.DAEMON, reason=reason)
        for session in self._ended_by_shutdown.values():
            if session in live:
                actors.note(session, by)
        await asyncio.gather(
            *(session.shutdown() for session in live), return_exceptions=True
        )
        if live:
            # What the endings are filed as (ended_by); was_running stays as
            # the first persist wrote it (_ended_by_shutdown).
            self.persist()
        log.info(
            "shut down %d live session(s) in %.1fs",
            len(live),
            time.monotonic() - started,
        )

    # ------------------------------------------------------------------ #
    # model reconciliation
    # ------------------------------------------------------------------ #
    def reconcile_models(self) -> List[str]:
        """Carry the model a session is *actually* answering on into its def.

        ``SessionDef.model`` is what the next launch puts on the command line,
        and until now nothing wrote it after creation. A person who switched
        model inside the harness (claude's ``/model``) therefore got the
        creation-time choice back at the next daemon restart, because
        :meth:`restore_all` replays the saved definition and the explicit
        ``--model`` flag on it outranks whatever the harness had persisted for
        itself. The rail showed the new model the whole time -- it reads the
        transcript, not the definition -- so the two disagreed with nothing
        saying so (docs/session-model-persistence.md).

        This is the same shape as ``conversation_id``: a value the harness
        settles at runtime, observed and written back into the definition.
        Only the newest reading is consulted; an older one is a choice already
        superseded. An id that :func:`harness.alias_for_model_id` cannot read
        leaves the definition alone and is recorded on the session, so the
        mismatch surfaces instead of being guessed at.

        Returns the names whose definition changed -- empty on the ordinary
        poll, which is why this is cheap enough to run on a timer.
        """
        changed: List[str] = []
        for session in list(self._sessions.values()):
            if session.exited:
                continue
            sdef = session.sdef
            entry = harness_registry.get(sdef.harness)
            if entry is None or not entry.models:
                continue
            try:
                reading = ctxsize.for_session(sdef)
            except Exception:  # a reading is never worth failing a poll for
                continue
            observed = (reading or {}).get("model")
            if not observed:
                continue
            alias = harness_mod.alias_for_model_id(entry, observed)
            if alias is None:
                # Not an error: a harness may answer on something this
                # registry does not name. Remember it for the session's meta
                # so a person can set the model explicitly, and say so once.
                if getattr(session, "unmapped_model_id", None) != observed:
                    session.unmapped_model_id = observed
                    log.info(
                        "session %r reports model id %r, which harness %r does "
                        "not map to any of %s; leaving its saved model %r alone",
                        sdef.name, observed, sdef.harness,
                        ", ".join(entry.models), sdef.model,
                    )
                continue
            session.unmapped_model_id = None
            if alias == sdef.model:
                continue
            log.info(
                "session %r is answering on %r (%s); its saved model was %r",
                sdef.name, alias, observed, sdef.model,
            )
            session.sdef = replace(sdef, model=alias)
            changed.append(sdef.name)
        return changed

    # ------------------------------------------------------------------ #
    # persistence / restore
    # ------------------------------------------------------------------ #
    def persist_soon(self) -> None:
        """:meth:`persist`, :attr:`PERSIST_COALESCE_SECONDS` from now, shared
        by every call made meanwhile. Without a running loop (the CLI, a
        synchronous caller) it persists at once. The write is the same
        :meth:`persist` -- the restore guard (``_loading``) and the
        empty-snapshot guard apply to it as to any other -- and any
        :meth:`persist` before it, ``shutdown_all``'s first of all, takes its
        place."""
        if self._persist_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.persist()
            return
        self._persist_handle = loop.call_later(
            self.PERSIST_COALESCE_SECONDS, self._persist_due
        )

    def _persist_due(self) -> None:
        self._persist_handle = None
        self.persist()

    def persist(self) -> None:
        handle = self._persist_handle
        if handle is not None and _on_running_loop():
            # This write covers what the pending one was scheduled for. Not
            # from a worker thread (the model-reconcile clock persists there):
            # a timer is the loop's to cancel, and the pending write is then
            # only one write too many.
            self._persist_handle = None
            handle.cancel()
        # The last chance to catch a model switch made since the timer's last
        # pass: at a shutdown this runs before anything is torn down, and what
        # it writes is exactly what restore_all will relaunch from.
        try:
            self.reconcile_models()
        except Exception:
            log.warning("model reconciliation failed; persisting as-is", exc_info=True)
        entries = []
        for session in self._sessions.values():
            entries.append(
                {
                    "def": session.sdef.to_dict(),
                    # An exit Windows caused at a logoff or shutdown counts
                    # as running: the machine went down around it, and the
                    # next boot is what brings it back (see ended_by_os).
                    # The flag, not the exit code: a record retired at an
                    # earlier boot keeps its code and must stay retired.
                    "was_running": not session.exited
                    or (self._ended_by_shutdown.get(session.sdef.name) is session
                        and not session.archived_at)
                    or (
                        getattr(session, "ended_by_os", False)
                        and not session.archived_at
                    ),
                    # Whether an agent was mid-turn here. persist() runs
                    # first in shutdown_all, before anything is torn down,
                    # so at a restart this is what the session was doing in
                    # the last moment before the daemon went down — the one
                    # question a restored-but-idle terminal cannot answer
                    # about itself (see daemon/resume.py).
                    "was_busy": session.status() == STATUS_BUSY
                    or session.sdef.name in self._delayed_busy,
                    "exit_code": session.exit_code,
                    # carried across restarts so a retired record keeps
                    # answering like the session it was
                    "pid": session.pid,
                    "created_at": session.created_at,
                    "last_output_at": session.last_output_at,
                    # When a person last looked in and last typed. Kept across
                    # the restart because that is exactly when the question
                    # gets asked: the sessions worth finding after a daemon
                    # comes back are the ones nobody has been near.
                    "last_visited_at": session.last_visited_at,
                    "last_input_at": session.last_input_at,
                    "exited_at": session.exited_at,
                    # Archive retains the record and only changes which fleet
                    # view owns it. Kept outside SessionDef because it is
                    # lifecycle state, not a launch option.
                    "archived_at": session.archived_at,
                    # The pause marker, kept for the same reason: a paused
                    # record that came back from a restart as a plain kill
                    # would drop out of the bulk resume it was paused for.
                    "paused_at": getattr(session, "paused_at", None),
                    # The hold marker (claunch-5l35i): a held record that came
                    # back from a restart as a plain kill would lose its wake
                    # and owe a board sweep it must not make.
                    "held_at": getattr(session, "held_at", None),
                    # The restore queue's marker (claunch-0j6gb.2): a boot
                    # that finds it relaunches the record through the queue
                    # again rather than leaving it a plain hold.
                    "delayed_at": getattr(session, "delayed_at", None),
                    # The failure marker (claunch-0j6gb.1): a failed record
                    # that came back as a plain kill would owe the board
                    # sweep its failure put off.
                    "failed_at": getattr(session, "failed_at", None),
                    # Whether this ending's board sweep was made. Without it
                    # every boot swept every exited record again, and the one
                    # write a sweep makes that does not change state -- the
                    # ORPHANED FOLLOW-UP comment -- piled up once per restart
                    # (claunch-fh8u1.2: 29 copies on one issue).
                    "swept_at": getattr(session, "swept_at", None),
                    # Who ended this incarnation (claunch-o6xws): only the
                    # live object knew, and a record retired at the next boot
                    # must keep answering the rail's "killed by whom".
                    "ended_by": getattr(session, "ended_by", None),
                    # A person's standing "type nothing in here". Written
                    # here so it survives the restart that has nothing to do
                    # with them; an exited record always reports False (see
                    # DeadSession.set_delivery_hold), so retiring a held
                    # session does drop the pin — the record has no terminal
                    # left to hold mail out of.
                    "delivery_hold": session.delivery_held(),
                    # The PTY host session it runs in (claunch-xnnct.1): what
                    # the next daemon matches to adopt it instead of
                    # relaunching it. None for a local PTY or an exited one.
                    "pty": (session.pty_record()
                            if callable(getattr(session, "pty_record", None)) else None),
                }
            )
        try:
            # Prune only in steady state: while a restore is still filling the
            # set, deleting the records not loaded yet would be the very clobber
            # this store exists to prevent. `allow_empty` lets clear/remove
            # empty it on purpose; every other empty write is refused by the
            # store's own guard.
            self._store.save(
                entries,
                prune=not self._loading,
                allow_empty=self._allow_empty_persist,
            )
        except sqlite3.Error:
            log.exception("failed to persist the session registry")
        finally:
            self._allow_empty_persist = False
        if self.shutting_down:
            return
        for hook in list(self.change_hooks):
            try:
                hook()
            except Exception:  # one hook must not silence the next
                log.exception("change hook %r failed", hook)

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

        The ones whose conversation was not on disk to reopen are recorded in
        :attr:`resumed_blank`. They came back alive and empty — no scrollback,
        and no opening task, which a restore does not replay — so "carry on"
        is not what they need and not what they are sent.
        """
        for _ in self._restore_steps():
            pass
        return self._restore_failed

    async def restore_all_async(self, progress=None) -> List[str]:
        """:meth:`restore_all`, with the loop still answering while it runs.

        The boot used to restore the whole fleet before binding its port, so
        every client spent the restore looking at a dead daemon -- on this
        machine 11.7s for 33 sessions, entirely inside the restart's black
        out (claunch-9akgh). The port is bound first now, and this variant
        yields to the event loop between registry entries so the API, the
        dashboards and the terminals of sessions already back can be
        answered while the rest are still being brought up.

        ``progress(done, total)`` is called once before the first entry (0,
        total) and after each one, for the boot status a restart reader
        polls (``api.boot_status``). total counts the records this boot
        tries to bring back; the rest are kept as exited records, which is
        cheap and happens in the same pass.
        """
        for _ in self._restore_steps(progress=progress):
            await asyncio.sleep(0)
        return self._restore_failed

    def _restore_steps(self, progress=None):
        """The body of :meth:`restore_all`, yielding after every entry.

        A generator rather than a plain loop because the two callers differ
        in exactly one place: the synchronous restore used by tests and by
        anything that is not serving HTTP, and the asynchronous one the
        daemon's boot runs with its port already bound. Everything else --
        the loading guard, the kept-record reconciliation, the persist and
        the timing -- is one implementation.

        The failure list is cleared here rather than in either caller: the
        boot only ever calls the asynchronous variant, but a process that
        restores twice must not carry the first pass's names into the second,
        and one reset in the shared body is the only place both entry points
        pass through (claunch-9akgh.1).
        """
        self._restore_failed = []
        try:
            entries = self._store.load_all()
        except sqlite3.Error:
            log.exception("failed to read the session registry")
            return
        failed: List[str] = self._restore_failed
        host = pty_backend.current_host()
        stranded = pty_backend.stranded_host() if host is None else None
        kept: Dict[str, dict] = {}
        if host is not None:
            try:
                kept = {str(r.get("sid")): r for r in host.list()}
            except Exception:
                log.warning("could not list the PTY host's sessions; restoring "
                            "as without one", exc_info=True)
                host = None
        # While the set is being filled, a persist would write a partial
        # fleet; pruning on it would delete the records not loaded yet. The
        # relaunches below skip theirs (see _launched), and the flag still
        # guards anything else that persists meanwhile. Hold it for the whole
        # loop, drop it before the one steady-state persist at the end.
        self._loading = True
        self._restore_clock = {
            "prepare": 0.0, "spawn": 0.0, "attach": 0.0, "adopt": 0.0,
        }
        started = _perf_counter()
        try:
            # Said up front: this is how many records the pass brings back,
            # which is also the total the boot status reports (claunch-9akgh).
            relaunching = sum(1 for e in entries if _brings_back(e))
            if relaunching:
                log.info(
                    "restoring %d session(s) (about a second each)",
                    relaunching,
                )
            if progress is not None:
                progress(0, relaunching)
            done = 0
            for entry in entries:
                self._restore_entry(entry, kept, host, stranded, failed)
                # `done` counts the same population `total` does. The
                # registry holds far more records than this boot brings
                # back -- exited ones kept for respawn -- and counting every
                # record made the boot banner run past its own total
                # ("restoring 1551/48" against this machine's 1551-record
                # registry, claunch-9akgh review). It lands exactly on
                # `total` because every counted record passes through here
                # once, processed or skipped.
                if _brings_back(entry):
                    done += 1
                    if progress is not None:
                        progress(done, relaunching)
                yield
            for row in kept.values():
                # Running in the host with no record here that owns it: a
                # session this registry ended or forgot, or one whose record
                # names another process. Nothing would ever drive it.
                log.warning(
                    "PTY host session %s (%r, pid %s) matches no running "
                    "record; ending it", row.get("sid"), row.get("name"), row.get("pid"),
                )
                self._end_kept(host, row)
        finally:
            self._loading = False
        if self.adopted:
            log.info("adopted %d session(s) from the PTY host: %s",
                     len(self.adopted), ", ".join(self.adopted))
        persist_started = _perf_counter()
        self.persist()
        if relaunching:
            # The boot's longest stretch, broken into its parts, so a slow
            # restart is read off the log instead of re-measured by hand:
            # "prepare" is building the command and replaying the previous
            # run's scrollback, "spawn" is the process and pseudo console.
            clock = self._restore_clock
            now = _perf_counter()
            log.info(
                "restore took %.1fs for %d session(s): prepare/replay %.1fs, "
                "spawn %.1fs, attach %.1fs, adopt/replay %.1fs, persist %.1fs, "
                "other %.1fs",
                now - started,
                relaunching,
                clock["prepare"],
                clock["spawn"],
                clock["attach"],
                clock["adopt"],
                now - persist_started,
                (persist_started - started)
                - clock["prepare"] - clock["spawn"]
                - clock["attach"] - clock["adopt"],
            )

    def _restore_entry(
        self, entry: dict, kept: Dict[str, dict], host, stranded, failed: List[str]
    ) -> None:
        """One registry record, as :meth:`_restore_steps` walks them.

        Split out so the generator can yield between records: this is where
        a boot's seconds go (an adopt replays up to ``REPLAY_TAIL_BYTES`` of
        pyte work per session), and holding the event loop for all of it is
        what kept the port from answering during a restart.
        """
        try:
            sdef = SessionDef.from_dict(entry.get("def") or {})
        except (KeyError, ValueError, TypeError):
            return
        if sdef.name in self._sessions:
            return  # a duplicated record must not clobber a live one
        row = self._kept_row(kept, sdef, entry)
        if row is not None:
            # Still running in the PTY host (claunch-xnnct.1): taken
            # over as it is, before the restore queue and the
            # relaunch below, which are for programs that ended.
            try:
                self._adopt(host, sdef, entry, row)
                return
            except Exception:
                log.exception(
                    "failed to adopt session %r from the PTY host; "
                    "ending it there and restoring it", sdef.name,
                )
                self._sessions.pop(sdef.name, None)
                self._end_kept(host, row)
        if stranded is not None and entry.get("pty") and entry.get("was_running"):
            # Still running in a PTY host this daemon could neither
            # use nor end (claunch-xnnct.1): a relaunch would run
            # the same conversation a second time.
            dead = self._retire(sdef, entry)
            # Nor has it ended, so it owes the board no sweep
            # (SESSION ENDED) either.
            if dead in self._retired_for_sweep:
                self._retired_for_sweep.remove(dead)
            log.warning(
                "session %r still runs in PTY host pid %s, which this daemon "
                "cannot reach; not relaunching it", sdef.name, stranded,
            )
            self.events.record(
                dead, "restore", "연결할 수 없는 PTY 호스트에서 실행 중이라 재실행하지 않음",
                host_pid=stranded,
            )
            return
        # A record still delayed when the previous daemon went down
        # was never started by it: it is owed the restore it did not
        # get, not left a plain hold.
        if sdef.restore and (entry.get("was_running") or entry.get("delayed_at")):
            if self.restore_stagger > 0 and self._resumable_def(sdef):
                self._defer_restore(sdef, entry)
                return
            # Asked before the relaunch, not after: the answer is about
            # the transcript the *previous* daemon left behind, and the
            # session we are about to start writes one of its own.
            blank = harness_mod.restores_blank(sdef)
            try:
                # Its own creation time, not this restart's: the
                # listings are ordered by it, and a restart that
                # restamped every relaunched session would flatten the
                # whole fleet into one moment and lose the order.
                restored = self.create(
                    sdef,
                    restoring=True,
                    created_at=entry.get("created_at"),
                    last_visited_at=entry.get("last_visited_at"),
                    last_input_at=entry.get("last_input_at"),
                    delivery_hold=bool(entry.get("delivery_hold")),
                )
                self.events.record(restored, "resume", "데몬 재시작 후 세션 복원")
                if entry.get("was_busy"):
                    self.resumed_busy.append(sdef.name)
                else:
                    self.resumed_idle[sdef.name] = entry.get("last_output_at")
                if not blank:
                    # Codex's blank restore is decided by the launch
                    # that just ran, not by the transcript the previous
                    # daemon left, so it is read here off the live
                    # definition -- `launch` resolves the conversation
                    # in place (see harness.codex_restores_blank).
                    launched = self._sessions.get(sdef.name)
                    blank = launched is not None and (
                        harness_mod.codex_restores_blank(launched.sdef)
                    )
                if blank:
                    self.resumed_blank.append(sdef.name)
                return
            except Exception:
                # The traceback is the only record this failure will
                # ever have: the record is retired on the next line
                # and nothing retries a relaunch, so the next boot
                # starts from a session that is simply not there
                # any more. Without it the log names the session and
                # says nothing about why — `failed to restore session
                # 's560'` and `'s571'` (2026-09-18 11:35:57) are
                # unrecoverable for exactly this reason.
                log.exception("failed to restore session %r", sdef.name)
                failed.append(sdef.name)
        self._retire(sdef, entry)

    @staticmethod
    def _kept_row(kept: Dict[str, dict], sdef: SessionDef, entry: dict) -> Optional[dict]:
        """The PTY host session ``entry`` was running in, taken off ``kept``
        -- or None. Name, sid and pid must all agree, and the record must be
        one the previous daemon left running; a row that fails that stays in
        ``kept``, which ends whatever is left there."""
        rec = entry.get("pty")
        if not isinstance(rec, dict):
            return None
        row = kept.get(str(rec.get("sid")))
        if row is None:
            return None
        if (row.get("name") != sdef.name or row.get("pid") != rec.get("pid")
                or not entry.get("was_running") or entry.get("held_at")
                or entry.get("archived_at")):
            return None
        return kept.pop(str(rec.get("sid")))

    def _adopt(self, host, sdef: SessionDef, entry: dict, row: dict) -> None:
        """Take over a session the PTY host kept across the restart.

        Running: a :class:`Session` is staged on it and re-attached
        (:meth:`Session.adopt`) -- same process, no relaunch, no resume.
        Exited while no daemon was attached: the output it made since is
        logged, and it is filed as an ending the way :meth:`_retire` files
        one that happened across a restart (its board sweep is claimed at
        boot), with the host's code and time.
        """
        rec = entry["pty"]
        sid = str(rec["sid"])
        from_seq = int(rec.get("seq") or 0)
        if row.get("exited"):
            handle, replay, reply = host.attach(sid, name=sdef.name, from_seq=from_seq)
            if rec.get("seq") is not None:
                _append_transcript(sdef.name, replay, int(reply.get("gap") or 0))
            code = reply.get("code")
            stamp = row.get("exited_at")
            exited_at = (
                datetime.fromtimestamp(float(stamp), timezone.utc).isoformat(timespec="seconds")
                if stamp else _now_iso()
            )
            dead = self._retire(sdef, {
                **entry, "exit_code": code, "exited_at": exited_at,
                "pid": row.get("pid"),
                # A nonzero code nobody asked for is a failure, as
                # _session_exited files one (claunch-0j6gb.1).
                "failed_at": exited_at if code not in (None, 0) else None,
                # Nothing asked for it (a kept session had no kill, pause
                # or hold pending), so the ender is what only the exit can
                # tell, as _attribute_ending decides it (claunch-o6xws).
                "ended_by": actors.make(actors.OS if ended_by_os(code) else actors.SELF),
            })
            handle.close()
            try:
                host.ack_exit(sid)
            except Exception:
                log.debug("ack_exit for %s failed", sid, exc_info=True)
            self.events.record(dead, "exit", "데몬이 없는 동안 세션 프로세스 종료",
                               exit_code=code, ended_by=dead.ended_by)
            return
        session = self.stage(
            sdef,
            restoring=True,
            created_at=entry.get("created_at"),
            last_visited_at=entry.get("last_visited_at"),
            last_input_at=entry.get("last_input_at"),
            delivery_hold=bool(entry.get("delivery_hold")),
        )
        # What an adoption costs, in its two parts: the host round trip that
        # hands back the output since the previous daemon detached, and the
        # pyte replay of the log tail that rebuilds the screen. The restore
        # log used to call all of it "other" (11.4s of 11.7s on 2026-10-21,
        # claunch-9akgh), which named no cause at all.
        clock = self._restore_clock if self._loading else None
        t0 = _perf_counter()
        handle, replay, reply = host.attach(sid, name=sdef.name, from_seq=from_seq)
        if clock is not None:
            clock["attach"] += _perf_counter() - t0
        t1 = _perf_counter()
        try:
            session.adopt(handle, replay, reply, rec)
        except Exception:
            handle.detach()
            raise
        finally:
            if clock is not None:
                clock["adopt"] += _perf_counter() - t1
        self.adopted.append(sdef.name)
        self.events.record(session, "adopt", "데몬 재시작 후 PTY 호스트의 세션 인수",
                           pid=session.pid, replayed=len(replay),
                           gap=int(reply.get("gap") or 0))
        self.flush_queued_input(session)
        self._run_launch_hooks(session)

    @staticmethod
    def _end_kept(host, row: dict) -> None:
        try:
            host.end(str(row.get("sid")))
        except Exception:
            log.warning("could not end PTY host session %s", row.get("sid"), exc_info=True)

    def _defer_restore(self, sdef: SessionDef, entry: dict) -> DeadSession:
        """File a session the boot owes a relaunch as *delayed* (a hold the
        restore queue owns, claunch-0j6gb.2) instead of relaunching it now.

        Only for a definition that comes back with its conversation
        (:meth:`_resumable_def`): a hold's wake is a respawn, and a blank
        relaunch would be decided by when it ran. Every wake path a hold has
        — a viewer, a mesh message, a cflow notice, a checks request, typing
        to it — therefore brings it forward of the queue for free.
        """
        stamp = entry.get("delayed_at") or _now_iso()
        dead = self._retire(
            sdef, {**entry, "held_at": entry.get("held_at") or stamp,
                   "delayed_at": stamp, "failed_at": None},
        )
        self.restore_queue.append(sdef.name)
        if entry.get("was_busy"):
            self.resumed_busy.append(sdef.name)
            self._delayed_busy.add(sdef.name)
        else:
            self.resumed_idle[sdef.name] = entry.get("last_output_at")
        self.events.record(dead, "delayed", "데몬 재시작 후 복원 대기열에 등록")
        return dead

    def _retire(self, sdef: SessionDef, entry: dict) -> DeadSession:
        """Register a definition as an exited record (nothing is running)."""
        dead = DeadSession(
            sdef,
            exit_code=entry.get("exit_code"),
            pid=entry.get("pid"),
            created_at=entry.get("created_at"),
            last_output_at=entry.get("last_output_at"),
            last_visited_at=entry.get("last_visited_at"),
            last_input_at=entry.get("last_input_at"),
            exited_at=entry.get("exited_at"),
            archived_at=entry.get("archived_at"),
            paused_at=entry.get("paused_at"),
            held_at=entry.get("held_at"),
            delayed_at=entry.get("delayed_at"),
            failed_at=entry.get("failed_at"),
            swept_at=entry.get("swept_at"),
            ended_by=actors.coerce(entry.get("ended_by")),
            scrollback=self.scrollback,
            idle_threshold=self.idle_threshold,
        )
        self._sessions[sdef.name] = dead
        dead.wake_on_message = self.wake_async
        dead.queue_on_message = self.queue_message
        dead.message_restore_pending = self.message_restore_pending
        # This ending never reaches the exit hooks (see
        # :attr:`_retired_for_sweep`), so the issue sweep it owes has to be
        # claimed at boot instead — by whoever owns the board.
        #
        # An archived record is the exception: archiving is the operator's own
        # "this is done, filed away", and the board sweep it owed ran when it
        # first exited, in the daemon life that archived it. Sweeping it again
        # here re-touches issues reconciled long ago — pure churn — and at
        # archive scale (hundreds of records) it is a slow boot for nothing.
        # A filed-away record is inert: it stays browsable, and the daemon
        # stops processing it.
        #
        # So is one whose sweep was already made (``swept_at``): the sweep is
        # owed once per ending, and the only records still owing it are the
        # ones that ended across the restart or whose sweep never finished.
        # And so is a held one: a hold is not an ending (Session.held_at).
        # And a failed one, for the same reason (claunch-0j6gb.1).
        if (not dead.archived_at and not dead.swept_at and not dead.held_at
                and not dead.failed_at):
            self._retired_for_sweep.append(dead)
        return dead

    def take_retired_for_sweep(self) -> List[DeadSession]:
        """The retired records whose board sweep is owed, once and once only.

        Called at boot after the board exists (the daemon's ``build_app``).
        Emptied here so a second call — a test rebuilding the app over the
        same manager — does not sweep the same records twice.
        """
        taken = self._retired_for_sweep
        self._retired_for_sweep = []
        return taken
