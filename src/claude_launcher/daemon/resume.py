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

* **Its cflow run is not parked.** The same rule the reminder clock uses
  (:func:`cflow_clock._actionable`): a run that is on a step, on an agent's
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

**A second audience, and a different message.** One restore branch does not
reopen a conversation at all: a session created in the seconds before the
restart has no transcript yet, so it is relaunched on ``--session-id`` and
comes back empty (:func:`harness.restores_blank`). Every sentence the nudge
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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, Iterable, List, Optional

from .. import store
from ..cflow import engine as cflow_engine, state as cflow_state
from ..harnesses import CLAUDE_HARNESS
from . import cflow_clock, rebrief
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


def gate(cwd: str, scope: str) -> Optional[bool]:
    """Whether this session's cflow position allows a resume nudge.

    ``True`` to nudge (the run is the agent's to move, or there is no run),
    ``False`` to stand down for good (parked on a gate, a user's selection,
    somebody else's answer, or finished), ``None`` when the run could not be
    read — the caller retries, because an unreadable state is a transient
    (mid-write, locked) far more often than it is an answer.

    Blocking (reads run state off disk); call it in a thread.
    """
    try:
        payload = cflow_engine.status(cflow_state.resolve_cwd(cwd), scope=scope)
    except Exception as exc:  # noqa: BLE001 — CflowError, StateError, OSError
        log.debug("resume nudge: run state for %r unreadable: %s", scope, exc)
        return None
    if payload.get("status") == "idle" and not payload.get("step_id"):
        return True  # no run here; nothing to be parked on
    return bool(cflow_clock._actionable(payload))


class ResumeNudge:
    """Tells the sessions a restart interrupted to carry on. One shot."""

    def __init__(
        self,
        manager,
        names: Iterable[str],
        *,
        blank: Iterable[str] = (),
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
        #: name -> monotonic time the session first read "ready". A session
        #: seen ready and then working again is being driven by somebody, and
        #: is dropped rather than nudged.
        self._ready_since: Dict[str, float] = {}
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

    async def _run(self) -> None:
        log.info(
            "resume nudge: %d restored session(s) were working: %s",
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
            if self.pending:
                log.info(
                    "resume nudge: gave up on %s (never became ready)",
                    ", ".join(self.pending),
                )
            if self.delivered:
                log.info("resume nudge delivered to %s", ", ".join(self.delivered))

    async def _attempt(self, name: str) -> bool:
        """One pass at one session. Returns whether it is settled (delivered,
        dropped, or refused) and should leave the pending list."""
        try:
            session = self.manager.get(name)
        except Exception:
            return True  # killed or cleared while we waited
        if getattr(session, "exited", True):
            return True

        state = self._readiness(session, name)
        if state == "wait":
            return False
        if state == "driven":
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
            block = blank_block(name, briefing=await self._briefing(name))
        else:
            verdict = await asyncio.to_thread(gate, session.sdef.cwd or "", name)
            if verdict is None:
                return False  # unreadable run state: look again next poll
            if not verdict:
                log.info(
                    "resume nudge for %r held: its cflow run is parked", name
                )
                return True
            block = nudge_block(name)

        if not await session.deliver(block):
            return False  # readiness/keyboard holds refused it; try again
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

        Ready is the same two-part test :meth:`Session._await_readable`
        applies — the TUI has taken the keyboard (bracketed paste) and has
        been quiet since — with the settle counted here so the whole window,
        not deliver's spawn-bound one, pays for a slow restore. A harness that
        is not the claude TUI never sets the mode, so for those "not starting
        any more" is all there is to wait for.
        """
        status = session.status()
        if status == STATUS_STARTING:
            return "wait"
        if getattr(session.sdef, "harness", CLAUDE_HARNESS) != CLAUDE_HARNESS:
            return "ready"
        armed = self._ready_since.get(name)
        if armed is None:
            screen = getattr(session, "screen", None)
            if status != STATUS_IDLE or not (screen and screen.bracketed_paste):
                return "wait"
            self._ready_since[name] = time.monotonic()
            return "wait"  # armed, not settled: later polls serve the settle
        if time.monotonic() - armed < INPUT_SETTLE:
            if status != STATUS_IDLE:
                # Still starting after all — the burst that follows the mode
                # going on. Begin the count again, exactly as _await_readable
                # does, rather than counting a busy TUI as settled.
                del self._ready_since[name]
            return "wait"
        # Settled once. Working now is somebody else driving it.
        return "driven" if status == STATUS_BUSY else "ready"


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


def blank_block(name: str, *, briefing: str = "") -> str:
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
    """
    lines = [
        "---",
        "# claunch: session resume (empty) -- machine-generated, not typed by "
        "the user",
        f"session: {name}",
        "what happened: the daemon restarted. This session's conversation had "
        "not been written to disk yet -- it was created within seconds of the "
        "restart -- so there was nothing to reopen and this terminal came back "
        "empty, on the same session id.",
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
