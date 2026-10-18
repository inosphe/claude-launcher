"""The nudge that restarts what a daemon restart stopped.

A restart is invisible to a session's *record* and fatal to its *turn*. The
manager brings every restorable session back (``--resume`` of the pinned
conversation, see :mod:`claude_launcher.daemon.manager`), so the terminal is
alive, the scrollback is there and the list says ``idle`` — but the agent that
was mid-work when the daemon went down is not working any more. Nothing ended
its turn and nothing will start the next one: an agent only acts when
something is put in front of it, and a restart puts nothing in front of
anything. The session sits there looking healthy, forever.

This module is what puts something in front of it. Two conditions decide who
hears it, and both are narrowing:

* **It was working.** ``persist`` records ``was_busy`` for every session in
  the moment before shutdown tears them down, and ``restore_all`` carries the
  ones that came back into :attr:`SessionManager.resumed_busy`. A session that
  was idle before the restart is idle for a reason — its agent had finished —
  and telling it to "continue" would invent work nobody asked for.

  With one exception (claunch-ccoof): an agent that ended its turn to wait
  for a background command (a test run, a long build) reads ``idle`` too,
  and the restart killed that command, so the notice that would have started
  its next turn never comes. Measured on 2026-10-09: s934 and s933 sat 77 and
  43 minutes on a cflow task after the 15:57 and 16:22 restarts. Such a
  session is told apart by two facts together: its PTY wrote something
  within :data:`RECENT_OUTPUT` of the restore (an agent that finished hours
  ago did not), and its run is waiting on it — a run is required here, unlike
  for a busy session, so an idle session with no run or a parked one is never
  typed into. It hears :func:`idle_block`, which says what was lost.

* **Its cflow run is not parked.** The same rule the reminder clock uses
  (:func:`cflow_clock._actionable`; for a v2 run, the cflow2 clock's
  :func:`cflow2_clock._waits_on_driver`): a run that is on a step, on an agent's
  branch choice, or on a delegated ask that reached nobody is the agent's to
  move, and it is the one to nudge. A run waiting on a human gate, on a
  user's selection or on another session's answer is *not* — it is parked
  exactly where the workflow wants it parked, and typing "carry on" into the
  session behind it is the daemon asking an agent to walk through a guardrail
  it cannot open anyway. A session with no run at all has no guardrail to
  reach, so it is nudged.

Then two more things it waits for, which are about landing rather than
deciding:

* **The TUI can take the message.** Same readiness Claude Code needs
  everywhere else (bracketed paste on, then quiet) — a restored session takes
  seconds to mount its input and a paste written into that gap is typed and
  never sent. ``deliver`` guards this too, but its own bound starts at spawn
  and a restart spawns every session at once, so the wait is done here where
  the budget is the whole window.

* **Nobody else got there first.** If a session starts working on its own
  after coming back — a human typed into it, a mesh delivery landed, the
  reminder clock fired — the nudge is dropped. Something is driving it, which
  is all this was ever trying to achieve.

One shot per daemon start. The list is fixed at restore, each name leaves it
delivered or dropped, and when it empties (or the window expires) the task
ends: this is a restart's opening move, not a clock.

**A second audience, and a different message.** Two restore branches do not
reopen a conversation at all, for different reasons. A *claude* session created
in the seconds before the restart has no transcript yet, so it is relaunched on
``--session-id`` and comes back empty (:func:`harness.restores_blank`). A
*codex* session whose directory holds no rollout of its own gets no conversation
either, because the alternative was opening another session's
(:func:`harness.codex_restores_blank`). Every sentence the nudge
above says is then false — there is no conversation above, re-reading the last
messages reaches someone else's screen or nothing, and the opening task went in
as argv on the first spawn and is not replayed. Those sessions are carried in
:attr:`SessionManager.resumed_blank` and hear :func:`blank_block` instead: what
was lost, that the scrollback is not theirs, and the re-briefing that carries
their task. Two differences from the nudge follow from that. It goes to blank
sessions whether or not they were working — an idle one lost just as much —
and it is not held by the cflow gate, because telling an agent who it is does
not walk it through any guardrail. It does answer to the same ``resume_nudge``
switch, though: one knob decides whether a restart types into anything here,
and a second one for this message would be a setting nobody knows they have.

**A third audience: operator sessions, always (claunch-gi7g4).** An operator
-- a session bound to an Operator v2 record, or a mesh member holding the
``operator`` role -- spends most of its life parked on purpose: its run sits on
``watch-gate`` or on the ``rest`` timer, so it is idle at the shutdown and its
run reads as parked, and both rules above leave it silent. But its job is to
watch and to relay, and a restart is exactly what it is there to notice: its
polls failed while the daemon was down, user input may have queued behind the
gap, and the timer that would have woken it died with the old daemon. So every
restored operator hears :func:`operator_block`, whatever it was doing and
wherever its run stands. A blank restore still hears :func:`blank_block`
instead, which already says more. And one that is working when it comes back
is not dropped as driven: the turn it is in was started by somebody else's
line, not by this notice, so the notice waits for that turn to end. The
``resume_nudge`` switch still decides for operators too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .. import store
from ..cflow import engine as cflow_engine, state as cflow_state
from ..cflow2 import runtime as cflow2_runtime
from ..harnesses import CLAUDE_HARNESS
from . import cflow2_clock, cflow_clock, rebrief
from .session import INPUT_SETTLE, STATUS_BUSY, STATUS_IDLE, STATUS_STARTING

log = logging.getLogger("claunch.daemon.resume")

#: How often a pending session is re-examined. Fine, because what it is
#: waiting for is a TUI finishing its startup — seconds, not minutes — and the
#: list is short and empties.
POLL = 1.0

#: How long the whole thing may take before the remaining sessions are given
#: up on. Generous: a restart relaunches every session at once and a machine
#: with a dozen ``--resume``\\ s on it is slow to settle. A session that never
#: became ready inside this heard nothing, which is the safe direction — a
#: paste into a TUI that is still starting is typed and never sent.
WINDOW = 600.0

#: How recently (seconds before the restore) an idle session's PTY must have
#: written for the restart to count as having cut it off. Covers a restart's
#: own downtime (a few minutes) plus a test run the agent was waiting on; a
#: session that finished its work is quiet for much longer than this.
RECENT_OUTPUT = 1800.0

#: Why a session is still on the pending list. Reported when the window
#: closes, because these are not one event and they do not call for one
#: response: a session still ``starting`` never got its TUI up (something is
#: wrong with the restore), one without ``keyboard`` is up but has not taken
#: the keyboard, and one that is ``working`` is doing exactly what this nudge
#: exists to restore — it needs nothing, and its silence is not a lost
#: resume. Reporting all of them as "never became ready" is true of the first
#: only: ``gave up on s635 (never became ready)`` was logged on 2026-09-21
#: 04:24:19 while that session's own transcript carries 66 entries inside the
#: window and a 179-second quiet gap that is a tool call in flight, not a
#: TUI that failed to come up. The distinction is the whole reason an
#: operator reads the line: "the daemon did not resume my session" and "my
#: session was busy" are different findings and only one is a defect.
WAIT_STARTING = "starting"
WAIT_KEYBOARD = "no-keyboard"
WAIT_WORKING = "working"
WAIT_SETTLING = "settling"
WAIT_RUN = "run-unreadable"
WAIT_PASTE = "paste-refused"
WAIT_DELAYED = "delayed"

#: The same keys as the sentence the give-up line prints. Written out rather
#: than interpolated so the line reads as a reason and not as a code.
_WAIT_REASONS = {
    WAIT_STARTING: "its TUI never finished starting",
    WAIT_KEYBOARD: "its TUI never took the keyboard (no bracketed paste)",
    WAIT_WORKING: "it was working the whole window (a turn was in flight)",
    WAIT_SETTLING: "it never held still long enough to paste into",
    WAIT_RUN: "its cflow run state could not be read",
    WAIT_PASTE: "the terminal would not take the paste",
    WAIT_DELAYED: "the restore queue had not started it yet",
}


def gate(cwd: str, scope: str, *, require_run: bool = False) -> Optional[bool]:
    """Whether this session's cflow position allows a resume nudge.

    ``True`` to nudge (the run is the agent's to move, or there is no run and
    ``require_run`` is off), ``False`` to stand down for good (parked on a
    gate, a user's selection, somebody else's answer, or finished), ``None``
    when the run could not be read — the caller retries, because an
    unreadable state is a transient (mid-write, locked) far more often than
    it is an answer.

    A v2 run in the slot is read first: v1's status cannot see it, and a v2
    run parked on a person's question used to read as "no run here".

    Blocking (reads run state off disk); call it in a thread.
    """
    try:
        payload2 = cflow2_runtime.status(cwd=cwd, scope=scope, tick=False)
    except Exception as exc:  # noqa: BLE001 — FlowError, StoreError, OSError
        log.debug("resume nudge: v2 run state for %r unreadable: %s", scope, exc)
        return None
    if payload2.get("status") in cflow2_runtime.LIVE_STATUSES:
        return cflow2_clock._waits_on_driver(payload2)
    try:
        payload = cflow_engine.status(cflow_state.resolve_cwd(cwd), scope=scope)
    except Exception as exc:  # noqa: BLE001 — CflowError, StateError, OSError
        log.debug("resume nudge: run state for %r unreadable: %s", scope, exc)
        return None
    if payload.get("status") == "idle" and not payload.get("step_id"):
        return not require_run  # no run here; nothing to be parked on
    return bool(cflow_clock._actionable(payload))


def recent_idle(idle: Dict[str, Optional[str]], now: Optional[float] = None,
                within: float = RECENT_OUTPUT) -> List[str]:
    """The idle restored sessions whose PTY wrote within ``within`` seconds
    of ``now`` (wall clock) -- the ones a restart may have cut off."""
    at = time.time() if now is None else now
    out = []
    for name, stamp in idle.items():
        if not stamp:
            continue
        try:
            written = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if 0 <= at - written <= within:
            out.append(name)
    return out


def operator_sessions(
    names: Iterable[str], *, operators=None, mesh_mgr=None
) -> List[str]:
    """The ``names`` that are operator sessions: bound to an Operator v2
    record (``operators`` is :class:`operators_v2.OperatorsV2`), or holding
    the ``operator`` role on a mesh ``mesh_mgr`` knows. Either reader may be
    missing or fail; it then answers nothing, never raises."""
    bound: set = set()
    if operators is not None:
        try:
            bound = set(operators.operator_names())
        except Exception:  # noqa: BLE001 — an unreadable record is not fatal
            log.exception("resume nudge: operator v2 records unreadable")
    out = []
    for name in dict.fromkeys(names):
        if name in bound:
            out.append(name)
            continue
        if mesh_mgr is None:
            continue
        try:
            meshes = mesh_mgr.meshes_for_session(name)
        except Exception:  # noqa: BLE001 — MeshError, OSError
            log.exception("resume nudge: mesh roles of %r unreadable", name)
            continue
        if any("operator" in (m.get("roles") or [m.get("role")]) for m in meshes):
            out.append(name)
    return out


def _turn_began(session, since: float) -> bool:
    """Whether a turn started in ``session`` at or after monotonic ``since``.

    The positive signal is the harness's own turn animation
    (:meth:`activity.Detector.turn_started_since`), retained through idle so a
    turn that began and ended between two polls still counts. A session
    object without a detector (a test double) keeps the old reading: busy
    after the settle is a turn.
    """
    activity = getattr(session, "_activity", None)
    if activity is None:
        return True
    return activity.turn_started_since(since)


class ResumeNudge:
    """Tells the sessions a restart interrupted to carry on. One shot."""

    def __init__(
        self,
        manager,
        names: Iterable[str],
        *,
        blank: Iterable[str] = (),
        recent: Iterable[str] = (),
        operators: Iterable[str] = (),
        mesh_mgr=None,
        poll: float = POLL,
        window: float = WINDOW,
        enabled: Optional[bool] = None,
    ) -> None:
        self.manager = manager
        self.mesh_mgr = mesh_mgr
        self.poll = poll
        self.window = window
        #: Read once, here: the switch decides whether this restart nudges at
        #: all, and re-reading it mid-window would only make one restart
        #: half-nudge.
        if enabled is None:
            try:
                enabled = bool(store.daemon_config().get("resume_nudge"))
            except Exception:  # noqa: BLE001 — an unreadable config is not fatal
                enabled = True
        self.enabled = enabled
        #: Who came back empty (:attr:`SessionManager.resumed_blank`). These
        #: hear :func:`blank_block` instead of :func:`nudge_block`, and they
        #: are on the pending list whether or not they were working: a blank
        #: restore loses what an idle session knew exactly as completely.
        self._blank = set(blank)
        self.pending: List[str] = list(names)
        for name in self._blank:
            if name not in self.pending:
                self.pending.append(name)
        #: Idle at the shutdown but writing shortly before it (see
        #: :func:`recent_idle`): nudged only when a run waits on them, and
        #: with :func:`idle_block`. A blank restore is not one of these -- it
        #: hears the blank block whatever it was doing.
        self._recent = {n for n in recent if n not in self.pending}
        self.pending.extend(sorted(self._recent))
        #: Operator sessions (:func:`operator_sessions`): told of the restart
        #: whatever they were doing and wherever their run stands, with
        #: :func:`operator_block` -- unless blank, which says more.
        self._operators = set(operators) - self._blank
        self._recent -= self._operators
        for name in sorted(self._operators):
            if name not in self.pending:
                self.pending.append(name)
        #: name -> monotonic time the session first read "ready". A session
        #: seen ready and then working again is being driven by somebody, and
        #: is dropped rather than nudged.
        self._ready_since: Dict[str, float] = {}
        #: name -> the :data:`WAIT_*` key it was last held by. Read only when
        #: the window closes, so the give-up line can say which of the six
        #: things actually happened instead of naming one of them for all.
        self._waiting: Dict[str, str] = {}
        self._task: Optional[asyncio.Task] = None
        #: Names actually delivered to — the tests' handle on the outcome,
        #: and what the log line at the end reports.
        self.delivered: List[str] = []

    def start(self) -> None:
        if self._task is not None or not self.enabled or not self.pending:
            if self.pending and not self.enabled:
                log.info(
                    "resume nudge disabled; %d restored session(s) left idle",
                    len(self.pending),
                )
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def adopt_blank(self, name: str) -> None:
        """Owe ``name`` the blank-restore message after all: a delayed codex
        record whose relaunch (by the restore queue, after this nudge was
        built) found no rollout of its own (claunch-0j6gb.2)."""
        self._blank.add(name)
        self._recent.discard(name)
        self._operators.discard(name)
        if name not in self.pending:
            self.pending.append(name)
        if self.enabled and (self._task is None or self._task.done()):
            self._task = None
            self.start()

    async def _run(self) -> None:
        log.info(
            "resume nudge: %d restored session(s) were working, recently "
            "active or operators: %s",
            len(self.pending),
            ", ".join(self.pending),
        )
        deadline = time.monotonic() + self.window
        try:
            while self.pending and time.monotonic() < deadline:
                await asyncio.sleep(self.poll)
                for name in list(self.pending):
                    try:
                        settled = await self._attempt(name)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One unreadable session must not strand the rest.
                        log.exception("resume nudge for %r failed", name)
                        settled = True
                    if settled:
                        self.pending.remove(name)
        except asyncio.CancelledError:
            raise
        finally:
            for name in self.pending:
                log.info(
                    "resume nudge: gave up on %r (%s)",
                    name, self._waiting_reason(name),
                )
            if self.delivered:
                log.info("resume nudge delivered to %s", ", ".join(self.delivered))

    async def _attempt(self, name: str) -> bool:
        """One pass at one session. Returns whether it is settled (delivered,
        dropped, or refused) and should leave the pending list."""
        if getattr(self.manager, "is_waking", lambda _n: False)(name):
            return False  # mid-relaunch: the record is out of the registry
        try:
            session = self.manager.get(name)
        except Exception:
            return True  # killed or cleared while we waited
        if getattr(session, "exited", True):
            # Still in the restore queue (claunch-0j6gb.2): it is owed the
            # nudge once it is up, not dropped for not being up yet.
            if getattr(session, "delayed_at", None):
                self._waiting[name] = WAIT_DELAYED
                return False
            return True

        state = self._readiness(session, name)
        if state == "wait":
            return False
        if state == "driven":
            if name in self._operators:
                # Somebody else's line started this turn; the restart is
                # still news to it. Wait for the turn to end and arm again.
                self._ready_since.pop(name, None)
                self._waiting[name] = WAIT_WORKING
                return False
            log.info("resume nudge for %r dropped: it is working again", name)
            return True

        if name in self._blank:
            # Not gated on the run's position. The gate exists to stop the
            # daemon telling an agent to walk through a guardrail it cannot
            # open; this message tells it nothing of the sort — it says the
            # scrollback it is looking at is not its own history, and restates
            # who it is. A session parked on a human gate needs that as much as
            # one mid-step, and it will read the gate for itself the moment it
            # calls 'status'.
            block = blank_block(
                name,
                briefing=await self._briefing(name),
                harness=session.sdef.harness,
            )
        elif name in self._operators:
            # Not gated either: an operator's run is parked by design (its
            # watch gate, its rest timer), and this tells it of the restart
            # rather than asking it through any guardrail.
            block = operator_block(name)
        else:
            recent = name in self._recent
            if recent:
                verdict = await asyncio.to_thread(
                    gate, session.sdef.cwd or "", name, require_run=True)
            else:
                verdict = await asyncio.to_thread(gate, session.sdef.cwd or "", name)
            if verdict is None:
                self._waiting[name] = WAIT_RUN
                return False  # unreadable run state: look again next poll
            if not verdict:
                log.info(
                    "resume nudge for %r held: %s", name,
                    "no run waits on it" if recent else "its cflow run is parked",
                )
                return True
            block = idle_block(name) if recent else nudge_block(name)

        if not await session.deliver(block):
            # readiness/keyboard holds refused it; try again. Worth its own
            # reason: this one means the session *is* settled by this module's
            # test and still would not take the paste, which points at the
            # terminal rather than at the restore.
            self._waiting[name] = WAIT_PASTE
            return False
        self.delivered.append(name)
        return True

    async def _briefing(self, name: str) -> str:
        """The re-briefing to carry to a blank session, or ``""``.

        The same composition ``/compact`` and ``/clear`` already get
        (:mod:`claude_launcher.daemon.rebrief`) — parent, mesh, run, asks,
        children and the recorded opening task. A blank restore is the third
        way a session loses that half of what it knows, so it is answered with
        the same text rather than a second one written for this path.

        Never fatal: a session told only that its terminal is empty is worse
        off than one told that plus its task, and better off than one told
        nothing because composing the briefing raised.
        """
        if self.mesh_mgr is None:
            return ""
        try:
            return await asyncio.to_thread(
                rebrief.compose, name, manager=self.manager,
                mesh_mgr=self.mesh_mgr,
            )
        except Exception:  # noqa: BLE001 — ManagerError, MeshError, OSError
            log.exception("resume nudge: re-briefing for %r failed", name)
            return ""

    def _readiness(self, session, name: str) -> str:
        """``"wait"`` / ``"ready"`` / ``"driven"`` for one session.

        The readiness half is the two-part test :meth:`Session._await_readable`
        applies — the TUI has taken the keyboard (bracketed paste) and has
        been quiet since — with the settle counted here so the whole window,
        not deliver's spawn-bound one, pays for a slow restore. A harness that
        is not the claude TUI never sets the mode, so for those "not starting
        any more" is all there is to wait for.

        The two halves answer different questions and this method's caller
        needs both, so it is deliberately not the same call: ``_await_readable``
        asks *may I type into this terminal*, and reads the quiescence
        heuristic. This one also asks *is somebody already driving it*, which
        only a finished turn can answer, so the waiting branches read
        :meth:`Session.status` — the heuristic plus the turn-in-flight
        override. That is the stricter of the two, and it is why a session can
        be receiving mesh mail and reminders (delivery's test) while this
        module still holds its nudge. Both facts are true at once and they are
        not in conflict.

        Every ``"wait"`` records which of the :data:`WAIT_*` states it was,
        because the caller reports that when the window closes and "held
        because it is busy" is not the same finding as "held because the TUI
        never came up".
        """
        status = session.status()
        if status == STATUS_STARTING:
            self._waiting[name] = WAIT_STARTING
            return "wait"
        if getattr(session.sdef, "harness", CLAUDE_HARNESS) != CLAUDE_HARNESS:
            return "ready"
        armed = self._ready_since.get(name)
        if armed is None:
            screen = getattr(session, "screen", None)
            if not (screen and screen.bracketed_paste):
                self._waiting[name] = WAIT_KEYBOARD
                return "wait"
            if status != STATUS_IDLE:
                # Output is still flowing and no settle has been observed
                # since the mode went on. That is the shape of a restored
                # session painting its transcript *and* of one already at
                # work, and the two cannot be told apart this early — which
                # is why this waits for a quiet moment rather than guessing.
                self._waiting[name] = WAIT_WORKING
                return "wait"
            self._ready_since[name] = time.monotonic()
            self._waiting[name] = WAIT_SETTLING
            return "wait"  # armed, not settled: later polls serve the settle
        if time.monotonic() - armed < INPUT_SETTLE:
            if status != STATUS_IDLE:
                # Still starting after all — the burst that follows the mode
                # going on. Begin the count again, exactly as _await_readable
                # does, rather than counting a busy TUI as settled.
                del self._ready_since[name]
                self._waiting[name] = WAIT_WORKING
            else:
                self._waiting[name] = WAIT_SETTLING
            return "wait"
        # Settled once. Working now is somebody else driving it -- but only
        # when a turn actually began since the arm. Busy without one is the
        # screen moving, not a turn: on 2026-10-14 fifteen of seventeen
        # sessions dropped here had no transcript entry around the drop, and
        # s498 sat idle five minutes until a person typed (claunch-f9ene.2).
        # Such a reading starts the settle again instead of settling the name.
        if status == STATUS_BUSY and not _turn_began(session, armed):
            del self._ready_since[name]
            self._waiting[name] = WAIT_WORKING
            return "wait"
        self._waiting.pop(name, None)
        return "driven" if status == STATUS_BUSY else "ready"

    def _waiting_reason(self, name: str) -> str:
        """The sentence the give-up line prints for one still-pending name.

        The state it was last held in, which is the state it was *still* in
        when the window closed — that is the question an operator is asking.
        A name that reached no branch (it was settled and refused delivery
        only through a path that did not record one) falls back to the
        generic wording rather than inventing a cause.
        """
        return _WAIT_REASONS.get(self._waiting.get(name, ""), "it never became ready")


def nudge_block(name: str) -> str:
    """What a restored session hears.

    It says the one thing the session cannot see for itself — that the gap in
    its conversation is a daemon restart, not a finished turn — and stops
    there. No instructions are restated: unlike a reminder, this session has
    not drifted from anything, it was cut off mid-stride, and its own
    scrollback (and, if it has a run, ``status``) is the truthful account of
    what it was doing. Marked machine-generated for the same reason every
    other automated delivery is: an agent must never read it as its user
    speaking.
    """
    return "\n".join(
        [
            "---",
            "# claunch: session resume -- machine-generated, not typed by the user",
            f"session: {name}",
            "what happened: the daemon restarted. This terminal was relaunched "
            "with --resume, so the conversation above is intact -- but the turn "
            "you were in the middle of died with the old daemon, and nothing "
            "has been driving this session since.",
            "protocol: continue the work you were doing. Re-read the last "
            "messages above to pick the thread back up; if you are driving a "
            "cflow run, call its 'status' tool first -- it is the current "
            "truth, and it may have moved while you were down. This is not a "
            "new task and nobody typed it.",
            "---",
        ]
    )


def operator_block(name: str) -> str:
    """What a restored operator session hears, every restart (claunch-gi7g4).

    It cannot reuse :func:`nudge_block` (an operator was usually not in a
    turn) or :func:`idle_block` (which assumes a run waiting on it). What is
    true of every operator is that the daemon was gone for a while: the polls
    and timers that pace its loop stopped, and what the user sent may be
    waiting in its inbox.
    """
    return "\n".join(
        [
            "---",
            "# claunch: session resume (operator) -- machine-generated, not "
            "typed by the user",
            f"session: {name}",
            "what happened: the daemon restarted. This terminal was relaunched "
            "with --resume, so the conversation above is intact. While the "
            "daemon was down your polls and the timer that wakes your run "
            "stopped, any turn or background command you had running was "
            "stopped with it, and user input may have queued up meanwhile.",
            "protocol: read and handle pending user input first (opv2_inbox), "
            "then call your cflow run's 'status' tool and continue your "
            "service loop from where it stands. If you posted an outage card "
            "while the daemon was unreachable, post the recovery as the "
            "observe step says. This is not a new task and nobody typed it.",
            "---",
        ]
    )


def idle_block(name: str) -> str:
    """What a restored session that was idle -- but recently active, with a
    run waiting on it -- hears (claunch-ccoof).

    Its turn did not die; what it may have been waiting on did. Claude Code
    wakes an agent when a background command it started finishes, and a
    restart kills that command with the old daemon, so the wake never comes.
    """
    return "\n".join(
        [
            "---",
            "# claunch: session resume -- machine-generated, not typed by the user",
            f"session: {name}",
            "what happened: the daemon restarted while this session was idle. "
            "The conversation above is intact, and your cflow run is waiting "
            "on you. Anything you left running in the background (a test run, "
            "a build, a long shell command) was stopped with the old daemon "
            "and will not report back.",
            "protocol: if you ended your last turn waiting for such a command, "
            "check what it left (its output file, a receipt) and run it again "
            "if it did not finish. Then call your run's 'status' tool and "
            "continue from where it stands. This is not a new task and nobody "
            "typed it.",
            "---",
        ]
    )


def blank_block(
    name: str, *, briefing: str = "", harness: str = CLAUDE_HARNESS
) -> str:
    """What a session restored into an *empty* conversation hears.

    :func:`nudge_block` cannot serve here. It says the conversation above is
    intact and to re-read the last messages to pick the thread back up, and on
    this branch there is no conversation above: the restore found no transcript
    for the pinned id and opened a fresh one on it
    (:func:`harness.restores_blank`). An agent that follows those instructions
    reads someone else's screen or an empty one, and reports back on nothing.

    So this says the two things that are actually true — the work that terminal
    was doing is gone and is not coming back, and what the session is *for* is
    below — and then carries the re-briefing, because the opening task went in
    as argv on the first spawn and a restore does not replay it.

    ``briefing`` is :func:`rebrief.compose`'s block. Empty is allowed and
    honest: a bare session with no mesh, no run and no recorded task has
    nothing to restate, and saying so beats implying something was withheld.

    ``harness`` picks which cause the block states, because two different ones
    land here and only the consequences are shared. Claude's is timing: the
    pinned conversation had no transcript yet (:func:`harness.restores_blank`).
    Codex's is ownership: no rollout in the session's directory was this
    session's own, so there was nothing it could reopen without opening
    somebody else's (:func:`harness.codex_restores_blank`). Naming the wrong
    one is not a cosmetic slip -- an agent told "it was created seconds before
    the restart" about a session that had been running for an hour will go
    looking for the contradiction instead of starting work.
    """
    if harness == "codex":
        cause = (
            "what happened: the daemon restarted. No conversation in this "
            "session's working directory was this session's own -- every "
            "recorded one there belongs to another session -- so there was "
            "nothing to reopen and this terminal came back empty."
        )
    else:
        cause = (
            "what happened: the daemon restarted. This session's conversation "
            "had not been written to disk yet -- it was created within seconds "
            "of the restart -- so there was nothing to reopen and this "
            "terminal came back empty, on the same session id."
        )
    lines = [
        "---",
        "# claunch: session resume (empty) -- machine-generated, not typed by "
        "the user",
        f"session: {name}",
        cause,
        "what this costs: whatever the previous terminal had done is gone and "
        "cannot be recovered; nothing above this line is your history. Do not "
        "report that work as done, and do not re-read the scrollback for it.",
        "protocol: start from the re-briefing below, which carries your "
        "opening task. If you are driving a cflow run, call its 'status' tool "
        "first -- it is the current truth and it has not been reset. This is "
        "not a new task and nobody typed it.",
        "---",
    ]
    if briefing:
        lines.extend(["", briefing])
    return "\n".join(lines)
