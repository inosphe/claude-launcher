"""The clocks cflow cannot carry itself.

Everything else in cflow happens because somebody called a tool: the agent
advances, a human approves, a responder answers. Five things have nobody to
call them, so the daemon carries all five, scanning the same machine-local
run registry the dashboard lists runs from:

* :class:`AskClock` — a delegated decision's ``timeout``. The one agent that
  would notice an expiry is the one stopped waiting for the answer.
* :class:`ReminderClock` — the step instructions an agent has drifted away
  from. The agent that would notice it has forgotten the protocol is,
  definitionally, the one that forgot it. The same clock carries the opposite
  errand for a step that declares an ``awaits``: re-measuring what the step is
  waiting for and speaking only when it moves. One class, because the two are
  the same decision — *does this position have anything to say right now?* —
  and splitting them would need two clocks negotiating each other's silence.
* :class:`StallPingClock` — the session that simply STOPPED, at a step no
  gate is holding. The reminder above never reaches it (it types only into a
  session that is working), and no gate event fires (there is no gate), so
  the one run position nobody watches is the one where nothing is wrong
  except that nobody is working.
* :class:`RunEventClock` — the moment a run stops being its own agent's: a
  human gate entered, a recurring round finished, a driver that exited. The
  session that would want to know — the overseer that spawned the driver —
  is precisely not the one anything happens in, so nothing else tells it.
* :class:`WindowClock` — a paced select option's window opening. The agent
  chose, and the workflow said "not more often than every N seconds", so
  the choice is parked; the one agent that would notice the moment is the
  one that ended its turn to wait for it.

Two consequences worth stating plainly, because both are deliberate:

**Without a daemon, neither fires.** A question keeps waiting for its
responder exactly as a human gate has always waited, and a drifting agent
drifts. That is the safe direction: a run that proceeds — or a terminal that
gets typed into — because nobody was watching the clock is the failure mode.

**The tick runs off the event loop.** The ask tick's escalation goes through
this daemon's own HTTP API; doing that from the loop would have it waiting on
a request only it can serve. A worker thread keeps the self-call honest, and
keeps a slow filesystem off the loop besides. The reminder clock keeps the
same split for the filesystem reason alone — its scan only reads, and its
deliveries go straight to the in-process sessions, on the loop, where they
belong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

from .. import store
from ..cflow import engine as cflow_engine, model as cflow_model, state as cflow_state
from . import rebrief
from .session import STATUS_BUSY, STATUS_IDLE

log = logging.getLogger("claunch.daemon.cflow")

#: How often to look. Deadlines are minutes-to-hours (a human or an agent has
#: to read a diff and decide), so a coarse tick costs nothing and keeps the
#: scan off a busy machine's back.
DEFAULT_INTERVAL = 20.0


class AskClock:
    """Expires timed-out delegated decisions across every run on this machine."""

    def __init__(self, *, interval: float = DEFAULT_INTERVAL) -> None:
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                for moved in await asyncio.to_thread(tick):
                    log.info(
                        "cflow ask %s at step %s expired with %s; now with %s",
                        moved.get("ask"),
                        moved.get("step"),
                        moved.get("expired") or "nobody",
                        ", ".join(moved.get("now_with") or []) or "nobody",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow ask clock tick failed")


def tick() -> List[dict]:
    """One pass over the registry. Blocking; call it in a thread."""
    moved: List[dict] = []
    for cwd, scope in cflow_state.known_runs():
        try:
            result = cflow_engine.expire_ask(cwd=cwd, scope=scope)
        except Exception as exc:
            # Includes the slot being locked by the agent mid-transition: the
            # deadline is already past, so the next tick is soon enough.
            log.debug("cflow ask expiry skipped for %s/%s: %s", cwd, scope, exc)
            continue
        if result:
            moved.append(result)
    return moved


# --------------------------------------------------------------------------- #
# the reminder clock
# --------------------------------------------------------------------------- #
#: How often the reminder clock looks. Finer than the reminder intervals it
#: enforces (minutes), coarse enough that the registry scan stays invisible.
REMINDER_POLL = 15.0

#: The two statuses where the *agent* is plainly the one the run is waiting
#: on. Every other position is blocked on somebody else — a human's gate, a
#: responder's answer — and re-typing step instructions there would tell the
#: agent to act on a step it is not allowed to enter.
_ACTIONABLE = ("step", "select")


def _ask_reached_nobody(payload: dict) -> bool:
    """A ``waiting_answer`` that was never actually put to anyone.

    Opening a delegated ask is a write, so a read-only ``status`` cannot do
    it (see :func:`cflow.engine.goto`): a run forced onto such a step with
    ``goto`` reports ``waiting_answer`` with no ask behind it — "not put to
    anyone yet" — and only the driver's own ``next`` opens the question.

    The distinction matters because the two ``waiting_answer``\\ s want
    opposite treatment. One that genuinely reached a responder is theirs to
    answer, and poking the driver about it is noise. One that reached nobody
    is nobody's, and it is the shape a run dies in: the driver hears nothing,
    the dashboard calls it delegated, and the run sits forever.
    """
    if payload.get("status") != "waiting_answer":
        return False
    return not ((payload.get("ask") or {}).get("asked") or [])


def _actionable(payload: dict) -> bool:
    """Whether the run's own agent is the one who can move it from here."""
    return payload.get("status") in _ACTIONABLE or _ask_reached_nobody(payload)

#: A reminder restates, it does not re-document: past this the step's own
#: `status` call is the readable copy, and the block says so.
_INSTRUCTIONS_LIMIT = 1200


def reminder_policy(payload: dict, cfg: dict) -> Tuple[bool, float]:
    """Effective ``(enabled, interval)`` for one run: the machine defaults
    with the run's own override laid over them, floor applied.

    Split out of :meth:`ReminderClock.scan` because the dashboard has to
    answer the same question — *is this clock going to fire here, and how
    often* — and a second copy of the rule is a second rule. The floor is
    part of the answer, not a detail of enforcement: a run overridden to 5s
    does not get reminded every 5 seconds, and a readout that said so would
    be wrong in the one direction a reader cannot check.
    """
    override = payload.get("reminder") or {}
    enabled = bool(override.get("enabled", bool(cfg.get("cflow_reminder"))))
    interval = float(
        override.get("interval", cfg.get("cflow_reminder_interval") or 0) or 0
    )
    if interval > 0:
        interval = max(interval, cflow_engine.REMINDER_MIN_INTERVAL)
    return enabled, interval


def ping_policy(cfg: dict) -> Tuple[bool, float]:
    """Effective ``(enabled, interval)`` for the stall ping. Machine-wide —
    unlike the reminder there is no per-run override — and floored the same
    way, for the same reason."""
    enabled = bool(cfg.get("cflow_ping"))
    interval = float(cfg.get("cflow_ping_interval") or 0)
    if interval > 0:
        interval = max(interval, PING_MIN_INTERVAL)
    return enabled and interval > 0, interval


class ReminderClock:
    """Re-types the current step's instructions into runs that stopped moving.

    Sessions forget the /cflow protocol the way they forget everything else —
    compaction, distraction, a long side quest — and a forgotten run does not
    fail, it just sits. This clock watches every run on the machine and, when
    one has held the same agent-actionable position for its reminder interval,
    types that position's instructions back into the driving session.

    *No progress* is the trigger, not the calendar: the position key
    (run, status, step, visit) resets the timer whenever it changes, so an
    agent that is advancing hears nothing, and one that has stalled hears
    from this clock every interval until it moves.

    It does not hear the same thing every time. The FIRST reminder at a
    position restates the step in full (:func:`reminder_block`), because an
    agent that has genuinely lost the thread cannot act on a pointer. Every
    repeat at that same position is the short form
    (:func:`repeat_block`) — the position, how long it has not moved, the
    completion test, and the two calls that fetch the long version on
    demand: ``status`` for the step, ``rebrief`` for the whole session. The
    split is measured, not aesthetic: across this machine's history 68% of
    reminders delivered were repeats at an already-reminded position (one
    stretch ran to 26), and a restatement that failed to move the run does
    not move it by being pasted again. What it does do is cost the agent the
    context the step is competing for. The full block is worth its size
    once; after that the agent is told where to pull it from instead.

    And *only while the agent is working*: the drift this clock corrects is
    an agent mid-turn, burying the step instructions under everything else
    on its screen — so a reminder is typed only into a session that reads
    busy. Idle, suspended and exited sessions hear nothing: nobody is
    working there, so there is no work to steer, and a paste would open a
    fresh turn just to say "keep going" to an agent that has stopped. A due
    reminder is held rather than dropped — retried every poll — so it lands
    the moment the session is working again.

    Configuration is read fresh on every pass: the machine defaults
    (``cflow_reminder`` / ``cflow_reminder_interval``) come from
    ``store.daemon_config()`` — so a ``claunch daemon config`` edit or the web
    UI's PUT applies by the next tick, no restart — and each run may override
    both in its own state (:func:`cflow.engine.set_reminder`).
    """

    def __init__(self, manager, mesh=None, *, poll: float = REMINDER_POLL) -> None:
        self.manager = manager
        #: The mesh registry, for the situation lines (:func:`situation_lines`).
        #: Optional the way :class:`RunEventClock` takes it: a daemon without
        #: meshes still reminds, it just has one fewer thing to say.
        self.mesh = mesh
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        #: (cwd, scope) -> {"pos": position key, "at": monotonic seconds} —
        #: in memory only. A daemon restart forgets the timers, which merely
        #: delays each run's next reminder by one interval; persisting them
        #: would buy nothing worth a state write per tick.
        self._seen: Dict[Tuple[str, str], dict] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                due = await asyncio.to_thread(self.scan, time.monotonic())
                for cwd, scope, block, kind in due:
                    await self._deliver(cwd, scope, block, kind)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow reminder clock tick failed")

    def scan(self, now: float) -> List[Tuple[str, str, str, str]]:
        """Decide who is due, as ``(cwd, scope, block, kind)``.

        Blocking — config, every run's state, and any probe whose poll
        interval has elapsed — so call it in a thread. Public for the tests,
        which own ``now`` there (and, through it, the probe spacing).

        ``kind`` is ``"reminder"`` or ``"signal"``, and it is carried rather
        than inferred because the two are delivered under different rules;
        see :meth:`_deliver`.
        """
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("cflow reminder: config unreadable, skipping: %s", exc)
            return []
        due: List[Tuple[str, str, str, str]] = []
        live = set()
        for cwd, scope in cflow_state.known_runs():
            key = (cwd, scope)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                log.debug("cflow reminder skipped %s/%s: %s", cwd, scope, exc)
                continue
            if not _actionable(payload):
                self._seen.pop(key, None)
                continue
            enabled, interval = reminder_policy(payload, cfg)
            awaits = payload.get("awaits") or {}
            if not enabled or (interval <= 0 and not awaits.get("probe")):
                # `enabled` is this clock's master switch: off, it says
                # neither of the two things it can say. An interval of zero
                # turns off only the clock half, which leaves the one
                # configuration in which this clock speaks nothing but news —
                # a step that declares what it waits for, and silence until
                # that changes.
                self._seen.pop(key, None)
                continue
            pos = (
                payload.get("run"), payload.get("status"),
                payload.get("step_id"), payload.get("visit"),
            )
            entry = self._seen.get(key)
            arrived = entry is None or entry["pos"] != pos
            if arrived:
                # Progress (or first sight) arms the timer; it does not fire
                # it. An agent that just took this step from 'next' has the
                # instructions already — and, for the same reason, the first
                # probe below is a baseline and never a signal: the state a
                # step arrives in is not news about it.
                entry = {
                    "pos": pos, "at": now, "probed_at": None, "probe": None,
                    # When this position was reached, as opposed to when the
                    # clock was last armed on it. ``at`` is re-armed by every
                    # delivery, so it answers "how long since I last spoke";
                    # only this answers "how long has the run been here",
                    # which is the number a repeat has to state.
                    "arrived_at": now,
                    # Dropped by the arming, unlike the three below: whether
                    # the step has been restated is a fact about THIS
                    # position, and carrying it forward would hand a fresh
                    # step the short form on its very first reminder.
                    "restated": False,
                    # Kept across the arming: "when did this run last hear
                    # from me" is a fact about the run, not about this
                    # stretch of it, and it is the one thing a reader has to
                    # tell a clock that is working from one that is merely
                    # configured.
                    "fired_at": (entry or {}).get("fired_at"),
                    "fired_kind": (entry or {}).get("fired_kind"),
                    "held_at": (entry or {}).get("held_at"),
                }
                self._seen[key] = entry
            if awaits.get("probe"):
                before, after = entry["probe"], self._measure(cwd, awaits, entry, now)
                if after is not None:
                    entry["probe"] = after
                    if before is not None and before["code"] != after["code"]:
                        # A signal also re-arms the clock: whatever the agent
                        # does next, it was just spoken to, and following that
                        # with the step restated would undo the point.
                        entry["at"] = now
                        due.append(
                            (cwd, scope, signal_block(payload, before, after), "signal")
                        )
                    # Measured — so something IS watching this position, and
                    # the clock has nothing to add. An unchanged condition is
                    # not news, and the step repeated over it is exactly the
                    # noise this path exists to remove.
                    continue
                # No answer at all: the probe could not be launched, or did
                # not finish inside its budget. That is not "not yet", so the
                # ordinary reminder resumes below. A broken probe must never
                # be the reason a stalled run goes quiet.
            if arrived:
                continue
            if interval > 0 and now - entry["at"] >= interval:
                # First time at this position, the step is restated in full;
                # after that it is not. The full block is what an agent that
                # has genuinely lost the step needs, and it is worth its size
                # exactly once — a restatement that did not move the run does
                # not move it by arriving again, and the fleet log says so:
                # 68% of all reminders ever delivered were a repeat at a
                # position already reminded, with one stretch reaching 26.
                # So the repeat says the short thing instead and hands the
                # agent the two pulls that carry the long one, 'status' for
                # the step and 'rebrief' for the session.
                #
                # One position is exempt, and it is the one whose block is
                # not a restatement at all: a delegated decision that reached
                # nobody. There the agent believes it is waiting on somebody
                # else and the block's whole content is the news that nobody
                # has it. That is not something the agent already has, so it
                # is not something a pointer can replace — and at ~480
                # characters it is already the short form.
                short = entry.get("restated") and not _ask_reached_nobody(payload)
                # ``or entry["at"]`` rather than a bare lookup: the arming is
                # not the only writer of this table any more, and a staleness
                # figure that falls back to the arming is off by at most one
                # interval, where a KeyError here would silence the clock.
                since = now - (entry.get("arrived_at") or entry["at"])
                block = reminder_block(payload, interval)
                if short:
                    pointer = repeat_block(payload, interval, since)
                    # Take the short form only when it IS shorter. Its own
                    # protocol paragraph is a fixed cost, so against a step
                    # whose instructions are a line long the "short" block is
                    # the bigger one — and then the whole argument for it has
                    # inverted: the agent would pay more to be told less.
                    # Comparing is cheaper than a threshold nobody maintains,
                    # and it cannot drift away from the reason for the rule.
                    if len(pointer) < len(block):
                        block = pointer
                due.append((cwd, scope, block, "reminder"))
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return due

    def _measure(self, cwd: str, awaits: dict, entry: dict, now: float) -> Optional[dict]:
        """This position's standing measurement, re-taken when due.

        Three returns, and only one of them is a fresh subprocess:

        * between samples — the previous answer, unchanged. Returning it
          rather than ``None`` is what keeps the clock quiet in the gaps: to
          the caller ``None`` means *broken*, and a gap is not that.
        * a fresh dict — the poll interval elapsed and the probe ran.
        * ``None`` — it ran and could not answer (see
          :func:`cflow.engine.run_probe`).

        The ceilings are re-applied here, not trusted from the payload. The
        parser already refuses a probe that could hold the daemon for long,
        but this clock reads runs whose workflow file it never opened —
        snapshots written by an older parser included — and "the probe is
        cheap" is the one promise this class cannot afford to take on faith.
        """
        poll = max(float(awaits.get("poll") or 0), cflow_model.MIN_AWAITS_POLL)
        last = entry.get("probed_at")
        if last is not None and now - last < poll:
            return entry.get("probe")
        entry["probed_at"] = now
        timeout = float(awaits.get("timeout") or cflow_model.DEFAULT_AWAITS_TIMEOUT)
        return cflow_engine.run_probe(
            awaits["probe"], cwd, min(timeout, cflow_model.MAX_AWAITS_TIMEOUT)
        )

    async def _deliver(
        self, cwd: str, scope: str, block: str, kind: str = "reminder"
    ) -> None:
        session = self._session_for(cwd, scope)
        if session is None:
            return
        if kind == "reminder" and session.status() != STATUS_BUSY:
            # A REMINDER waits for a working session. It carries nothing the
            # agent does not already have — it restates the step — so its
            # whole value is landing in front of an agent mid-turn that has
            # buried the protocol under everything else on its screen. Pasted
            # into a session that has ended its turn it would open a fresh one
            # just to say "keep going" to somebody who already stopped. Held,
            # not dropped: the debt stays due and is retried each poll, so it
            # lands the moment the session is working again.
            #
            # A SIGNAL does not wait, and the difference is not a preference.
            # It is news the agent does NOT have, about the very thing it
            # ended its turn to wait for. Holding it until the session happens
            # to be busy would leave the run correctly waiting while the
            # daemon sits on the answer — the exact failure this path exists
            # to remove. So a signal wakes an idle session, as a released
            # window does (:class:`WindowClock`).
            log.debug("cflow reminder held for %r: session is not working", scope)
            entry = self._seen.get((cwd, scope))
            if entry is not None:
                # Stamped, not merely logged: "due, but the session stopped"
                # is the one state where this clock is configured, armed and
                # correctly silent — indistinguishable from broken to anyone
                # reading a countdown, unless the hold itself is reported.
                entry["held_at"] = time.monotonic()
            return
        if kind == "reminder":
            # Composed HERE and not in `scan`, and the reason is threading,
            # not taste: `scan` runs in a worker thread (see this module's
            # docstring) and ``MeshManager`` is event-loop only. The one
            # source that is neither — the open-ask scan — is a filesystem
            # walk, so it goes back to a thread rather than blocking the loop
            # this method runs on. Deliveries are rare enough (four figures
            # across this machine's entire history) that the hop costs nothing.
            try:
                open_asks = len(
                    await asyncio.to_thread(cflow_engine.open_asks, scope)
                )
            except Exception:  # noqa: BLE001 — decoration must not sink a send
                open_asks = 0
            extra = situation_lines(scope, self.manager, self.mesh, open_asks)
            # ``restated`` is written after a successful send, so it is still
            # False here on the fire that carries the full restatement —
            # which is the one fire the session-level ids ride on.
            if not (self._seen.get((cwd, scope)) or {}).get("restated"):
                extra = extra + carried_id_lines(
                    scope, self.manager, self.mesh
                )
            block = splice(block, extra)
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow %s delivery to %r failed", kind, scope)
            return
        if delivered:
            # Rearm only on success: a delivery that failed past deliver's
            # holds keeps its debt and is tried again next poll.
            entry = self._seen.get((cwd, scope))
            if entry is not None:
                entry["at"] = time.monotonic()
                entry["fired_at"] = entry["at"]
                entry["fired_kind"] = kind
                entry["held_at"] = None
                if kind == "reminder":
                    # Stamped on DELIVERY, never on composition. A block held
                    # for a session that stopped is composed again next poll,
                    # and flipping this in `scan` would let the full
                    # restatement be replaced by the short form having never
                    # actually landed. A signal is not a restatement and
                    # leaves this alone.
                    entry["restated"] = True
            log.info("cflow %s delivered to %r (%s)", kind, scope, cwd)

    def skip(self, cwd: str, scope: str) -> bool:
        """Let ONE of a run's reminders go by, without switching the clock off.

        Re-arms this run's timer where it stands and drops any reminder
        already held for a session that had stopped, leaving `enabled` and
        `interval` — the run's override and the machine defaults alike —
        untouched. The next reminder is then due a full interval from now.

        It exists because pausing is the wrong size for the thing people
        actually want here. A pause (:func:`cflow.engine.set_reminder`) is a
        state somebody has to remember to undo, and it is set at exactly the
        moment they are least likely to: they are watching one session do one
        long thing, and they want *this* reminder not to land in the middle
        of it. Turning the clock off for that is how a run ends up with no
        reminder for the rest of the afternoon, for a reason nobody can see
        two hours later. This changes nothing that outlives the interval.

        Returns whether there was a timer to re-arm. ``False`` means this
        clock is keeping none for that run — switched off there, a position
        that is not the agent's to move, or a run that only just arrived —
        and in each of those cases no reminder was coming for a skip to stop,
        so there is nothing to report but the fact.

        Racing the tick is possible and deliberately not locked out: a skip
        landing after :meth:`scan` has already put this run in its due list
        loses, and one more reminder is typed. Guarding that would put a lock
        between this call and the poll's whole worker thread, to win a race
        whose entire prize is one repetition of a message whose contract is
        already "again every interval until it moves".
        """
        entry = self._seen.get((cwd, scope))
        if entry is None:
            return False
        entry["at"] = time.monotonic()
        # The held debt goes with it. A held reminder is *due and retried
        # every poll*, so leaving the stamp would land the very reminder this
        # call just said to skip, the moment the session reads busy again.
        entry["held_at"] = None
        log.info("cflow reminder skipped once for %r (%s)", scope, cwd)
        return True

    @property
    def running(self) -> bool:
        """Whether the tick is actually alive.

        The difference between "reminders are on" and "reminders happen" is
        this attribute, and nothing else on the machine records it: the
        config says what was asked for, and a clock whose task died to an
        unhandled cancel keeps every configured value saying yes.
        """
        return self._task is not None and not self._task.done()

    def timers(self, now: Optional[float] = None) -> Dict[Tuple[str, str], dict]:
        """What this clock is holding for each run, in plain seconds.

        Read-only and cheap on purpose — no config, no state file, no probe.
        Just the monotonic stamps already in memory, turned into ages the
        caller can subtract from an interval it looks up itself. The policy
        is deliberately NOT copied in here: this class re-reads it every
        pass, and a copy taken at the last scan would be up to a poll stale
        exactly when somebody has just changed it.

        A key missing from the result means this clock is keeping no timer
        for that run — because it is not enabled there, or because the
        position is not the agent's to move. Which of the two is a question
        for the policy, not for this.
        """
        at = time.monotonic() if now is None else now

        def ago(stamp):
            return None if stamp is None else max(0.0, at - stamp)

        out: Dict[Tuple[str, str], dict] = {}
        for key, entry in list(self._seen.items()):
            probe = entry.get("probe") or {}
            out[key] = {
                "armed_ago": ago(entry.get("at")),
                "fired_ago": ago(entry.get("fired_at")),
                "fired_kind": entry.get("fired_kind"),
                "held_ago": ago(entry.get("held_at")),
                "probed_ago": ago(entry.get("probed_at")),
                "probe_code": probe.get("code"),
                # Which block the NEXT reminder here will be. A countdown
                # that cannot say this is only half an answer: the reader
                # watching it is deciding whether to let the clock speak,
                # and "in 40s" means a different thing at 1.5k characters
                # than at 0.6k.
                "restated": bool(entry.get("restated")),
            }
        return out

    def _session_for(self, cwd: str, scope: str):
        return session_for(self.manager, cwd, scope)


def session_for(manager, cwd: str, scope: str):
    """The live session a run maps 1:1 to, or None.

    Same containment rule as the dashboard's ``_scope_sessions``: the scope
    IS the session name, and the cwd must match so a name reused in another
    directory is never typed into by that directory's run. Shared by every
    clock here that types into a driver, so the containment rule is stated
    once and cannot drift between them.
    """
    try:
        session = manager.get(scope)
    except Exception:
        return None
    if session.exited or not session.sdef.cwd:
        return None
    try:
        if cflow_state.resolve_cwd(session.sdef.cwd) != cwd:
            return None
    except Exception:
        return None
    return session


def reminder_block(payload: dict, interval: float) -> str:
    """The text a stalled run's session hears, composed from its status.

    A restatement, not a nudge: the step's own instructions ride in it,
    because "continue per the protocol" means nothing to an agent that no
    longer remembers what the step was. Framed as the *same* instruction so
    an agent mid-work does not read it as a new order — and pointed at the
    ``status`` tool for everything the block leaves out.
    """
    lines = [
        "---",
        "# claunch cflow: reminder -- machine-generated; repeats every "
        f"{interval:.0f}s while you keep working without this step moving",
        f"workflow: {payload.get('workflow')}",
    ]
    if payload.get("digest"):
        # The id of the text below, said WITH the text and not instead of it.
        # This is the copy the agent keeps; every repeat quotes this id rather
        # than pasting the body again, so the id has to arrive attached to
        # what it names or there is nothing for the agent to match it to.
        lines.append(f"step text id: {payload['digest']}")
    step = payload.get("step_id")
    visit = payload.get("visit")
    position = f"step '{step}'" + (f" (visit {visit})" if visit and visit > 1 else "")
    if _ask_reached_nobody(payload):
        # No instructions to restate: the step's content is withheld behind
        # the very approval nobody holds. So this block says the one true
        # thing about the position instead — the question exists, it was
        # never put to anyone, and 'next' is what puts it.
        lines.append(f"position: {position}, entry approval not yet opened")
        if payload.get("prompt"):
            lines.append(f"prompt: {payload['prompt']}")
        lines.append(
            "protocol: this run is parked on a delegated decision that was "
            "never actually put to anyone -- most likely the position was "
            "forced here with 'goto', which does not deliver the step. It "
            "reads as 'waiting on somebody else', but nobody has it. Call "
            "'next' to open the question and route it; do not wait to be "
            "answered."
        )
    elif payload.get("status") == "select":
        lines.append(f"position: branch choice at {position}")
        lines.append(f"prompt: {payload.get('prompt')}")
        for opt in payload.get("options") or []:
            lines.append(f"  - {opt.get('name')}: {opt.get('description')}")
        lines.append(
            "protocol: you are the chooser. Pick one with the cflow 'select' "
            "tool; if you have lost the thread, call 'status' first -- it is "
            "the current truth."
        )
    else:
        instructions = str(payload.get("instructions") or "").strip()
        if len(instructions) > _INSTRUCTIONS_LIMIT:
            instructions = instructions[:_INSTRUCTIONS_LIMIT] + (
                " [... cut; the 'status' tool serves the full text]"
            )
        lines.append(f"position: {position}")
        lines.append(f"instructions: {instructions}")
        done_when = str(payload.get("done_when") or "").strip()
        if done_when:
            # The one line a stalled agent needs most: what would let it
            # advance — a stated criterion, not its own sense of enough.
            lines.append(f"done when: {done_when}")
        if payload.get("verify"):
            lines.append(f"verify: {payload['verify']}")
        lines.append(
            "protocol: this is the step you are on -- the same instruction, "
            "repeated because the run has not moved, not a new one. If you "
            "are mid-work, keep going. When it is done"
            + (" (the 'done when' line is the test)" if done_when else "")
            + ", file it with "
            "'report' and advance with 'next'; if you have lost the thread, "
            "call 'status' first -- it is the current truth."
        )
    lines.append("---")
    return "\n".join(lines)


def repeat_block(payload: dict, interval: float, stalled_for: float) -> str:
    """The text a run hears on every reminder after the first at a position.

    :func:`reminder_block` has already said the step here, in full, and the
    run did not move. Saying it again is the one thing this block refuses to
    do — not to be terse, but because the repeat is where the push model runs
    out: a paste that failed to reach the agent's attention does not reach it
    by being longer the second time, and each retry is charged to the very
    context the step is competing for.

    So it says only what the first block could NOT have said — how long the
    run has now been here — keeps the completion test, which is the one line
    that tells a working agent whether it is nearly done, and converts the
    rest into two pulls. ``status`` restates the step; ``rebrief`` restates
    the session. That is the same content, moved from the daemon's push to
    the agent's own call, and it is offered rather than demanded: an agent
    that is mid-work and knows exactly where it is should spend the turn on
    the work, not on re-reading what it already has.

    The framing repeats :func:`reminder_block`'s promise for the same reason
    — this is the same instruction, not a new one — and says which form it
    is, so a shorter block never reads as a step that quietly shrank.
    """
    step = payload.get("step_id")
    visit = payload.get("visit")
    position = f"step '{step}'" + (f" (visit {visit})" if visit and visit > 1 else "")
    chooser = payload.get("status") == "select"
    if chooser:
        position = f"branch choice at {position}"
    minutes = max(1, int(stalled_for // 60))
    lines = [
        "---",
        "# claunch cflow: reminder -- machine-generated; the step was already "
        f"restated here once, so this is the short form (every {interval:.0f}s)",
        f"workflow: {payload.get('workflow')}",
        f"position: {position}, unmoved for ~{minutes} min",
    ]
    digest = payload.get("digest") or ""
    if digest:
        lines.append(f"step text id: {digest}")
    done_when = str(payload.get("done_when") or "").strip()
    if done_when and not chooser:
        lines.append(f"done when: {done_when}")
    advance = (
        "'select' is what moves it" if chooser
        else "'report' then 'next' is what advances it"
    )
    what = "this choice and its options" if chooser else "this step"
    if digest:
        # The predicate is the whole design. "Do you remember the step" is
        # not a question an agent can answer, so it guesses, and a guess
        # resolves to "keep going" every time. "Is this id above you in this
        # conversation" is a question it CAN answer by looking, and the two
        # answers lead to different actions. So the block asks that one.
        lines.append(
            f"protocol: same position, still yours to move, and nothing here "
            "is new. You were given this position's text in full, once, under "
            f"the id above. Look for {digest} in this conversation: if it is "
            f"there, you still have {what} -- keep working and do not spend "
            "the turn re-reading. If it is NOT there, your context no longer "
            f"holds it: call the cflow 'recall' tool with id {digest} and it "
            "will hand the text back. Do not reconstruct it from memory, and "
            f"do not treat this line as the text. {advance}."
        )
    else:
        lines.append(
            f"protocol: same position, still yours to move, and nothing here "
            "is new. If you are mid-work, keep going -- do not spend the turn "
            "re-reading. If you have lost the thread, the cflow 'status' tool "
            f"restates {what} in full. {advance}."
        )
    lines.append("---")
    return "\n".join(lines)


def splice(block: str, lines: List[str]) -> str:
    """Put ``lines`` inside a block's fence, just before it closes.

    The blocks here are fenced so an agent can tell where machine-generated
    text starts and stops; anything appended after the closing ``---`` reads
    as a separate paste, and anything that replaces the fence stops reading
    as machine-generated at all. So additions go inside, at the end, where a
    reader has already taken in the position they belong to.
    """
    if not lines:
        return block
    rows = block.splitlines()
    if rows and rows[-1] == "---":
        return "\n".join(rows[:-1] + list(lines) + ["---"])
    return "\n".join(rows + list(lines))


def situation_lines(name: str, manager, mesh_mgr, open_asks: int = 0) -> List[str]:
    """What is true around this session right now, said in full every time.

    The counterpart to the content id, and the reason both exist. A step's
    text is immutable for the life of a position, so it can be named by a
    hash and pulled back on demand. None of *this* is: replies come due, a
    child exits, a decision lands. An id for it would be a promise the
    daemon cannot keep — quote ``7c1e`` for a roster and the agent that calls
    for it later gets either a stale snapshot or a different roster, and both
    are worse than the two lines it would have replaced. So the volatile half
    is pushed, in full, and stays small enough to be worth pushing.

    Only what has something to say. A session with no mail owing, no
    decisions on it, no children and a live parent adds nothing here — which
    is the ordinary case, and why the repeat stays at its measured size.

    Never raises: this decorates a reminder, and a reminder that failed to
    send because the roster was mid-write would be a bad trade.
    """
    lines: List[str] = []
    try:
        sdef = manager.get(name).sdef
    except Exception:  # noqa: BLE001 — raced an exit; the rest still stands
        return lines
    if open_asks:
        lines.append(
            f"asks: {open_asks} delegated decision(s) from other runs await "
            "your answer -- their workflows are stopped on it. The cflow "
            "'asks' tool serves them, 'answer' closes them."
        )
    if mesh_mgr is not None:
        try:
            for row in mesh_mgr.meshes_for_session(name):
                mesh = mesh_mgr.get(row["mesh"])
                member = mesh_mgr.member_for_session(mesh, name)
                if member is None:
                    continue
                owed = mesh.owed(member.handle)
                if owed:
                    lines.append(
                        f"owed: {len(owed)} delivered message(s) on mesh "
                        f"{mesh.name} still await your reply -- to the "
                        "senders, silence is silence. 'claunch mesh history "
                        f"{mesh.name} -n 30' shows them."
                    )
        except Exception:  # noqa: BLE001 — a mesh deleted under us says nothing
            pass
    try:
        live = manager.live_children(name)
    except Exception:  # noqa: BLE001
        live = []
    if live:
        lines.append(
            f"children: {', '.join(live)} still running and still reporting "
            "to you -- a child holding a finished result keeps holding it "
            "until you ask."
        )
    if sdef.parent:
        try:
            if manager.get(sdef.parent).exited:
                lines.append(
                    f"parent: {sdef.parent} has exited -- whatever you were "
                    "going to report to it has nowhere to go. Say so in this "
                    "run's next report rather than reporting into the void."
                )
        except Exception:  # noqa: BLE001 — no record is not "exited"
            pass
    if lines:
        lines.insert(
            0,
            "-- around you right now (this part changes; it is stated in "
            "full because there is no id that could stay true for it) --",
        )
    return lines


def carried_id_lines(name: str, manager, mesh_mgr) -> List[str]:
    """The ids of the session-level text this agent was handed, named not sent.

    The same trade the step's own id makes (:func:`repeat_block`), applied to
    the two blocks that outlive any one position: the opening task and the
    binding stance. Both are immutable, both are re-derivable, and both are
    exactly the kind of thing an agent quietly stops having after a summary
    without noticing it stopped.

    Named ONLY on the full form, once per position, and the reason is a
    measurement rather than taste: the repeat is 777 characters against the
    full block's 1545, and that gap is the whole product. A reference line on
    every repeat would spend a third of the saving on a question that is
    almost never the one being asked — an agent whose context was compacted
    has already had the hook re-deliver these blocks, with their ids, before
    its next turn.

    Only ids the session was *given next to their text*
    (:func:`rebrief.given_ids`). An id it has never seen attached to prose
    would fail the check by construction, and buy a recall of text it may
    well already hold.

    Never raises, for :func:`situation_lines`' reason: this decorates a
    reminder, and a reminder lost to a mid-write roster is a bad trade.
    """
    try:
        ids = rebrief.given_ids(name, manager=manager, mesh_mgr=mesh_mgr)
    except Exception:  # noqa: BLE001 — no ids is not a reason to send nothing
        return []
    if not ids:
        return []
    named = "; ".join(f"{ident} ({kind})" for ident, kind in ids)
    return [
        f"session text ids: {named}. These name text you were GIVEN, printed "
        "next to it — not text in this reminder, and not this line. If you "
        "cannot find one of them attached to its text in this conversation, "
        "your context no longer holds that block: call the mesh 'rebrief' "
        "tool with that id and it hands the text back."
    ]


def signal_block(payload: dict, before: dict, after: dict) -> str:
    """The text a waiting run hears when its awaited condition MOVED.

    The opposite errand to :func:`reminder_block`, and it must not read like
    it. A reminder repeats what the agent already has; this carries something
    it does not — so the step's instructions are deliberately absent. Their
    presence is what would turn "the thing you were waiting for arrived" back
    into "here is your step again", which is the noise the whole ``awaits``
    path exists to remove, and the agent has ``status`` for the step anyway.

    Framed like the window notice, for the same reason: it lands in a session
    that ended its turn, and an unframed line reads as a user message.

    The probe's own output rides along as *evidence*, and the block says in
    as many words that it is not proof. A probe is a cheap check on state
    something else changed; between the sample and the reading, that
    something else may have changed it again.
    """
    step = payload.get("step_id")
    visit = payload.get("visit")
    position = f"step '{step}'" + (f" (visit {visit})" if visit and visit > 1 else "")
    awaits = payload.get("awaits") or {}
    lines = [
        "---",
        "# claunch cflow: signal -- machine-generated, not typed by the user",
        f"workflow: {payload.get('workflow')}",
        f"position: {position}",
        f"awaiting: {awaits.get('describe') or awaits.get('probe')}",
        f"changed: exit {before.get('code')} -> exit {after.get('code')}",
    ]
    if awaits.get("describe") and awaits.get("probe"):
        lines.append(f"probe: {awaits['probe']}")
    says = str(after.get("says") or "").strip()
    if says:
        lines.append("probe said:")
        lines.extend(f"  {line}" for line in says.splitlines())
    lines.append(
        "protocol: this fired because the condition MOVED, and you will hear "
        "nothing more from it until it moves again -- silence is not 'still "
        "waiting', it is 'nothing new'. The probe is a cheap check and not "
        "proof, so confirm the change yourself before you act on it, then "
        "carry on with this step. The step itself has not moved and nothing "
        "was decided for you; call the cflow 'status' tool if you need it "
        "restated."
    )
    lines.append("---")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the stall ping clock
# --------------------------------------------------------------------------- #
#: How often the ping clock looks. The reminder's cadence — the stalls it
#: measures are minutes to hours, so a finer poll would only cost scans.
PING_POLL = 15.0

#: The floor on the configured interval. A ping opens a fresh turn in a
#: session that had stopped; at less than a minute apart that is not a nudge,
#: it is a session that never gets to finish reading the last one.
PING_MIN_INTERVAL = 60.0


class StallPingClock:
    """Pings a session that STOPPED at a step nothing is holding.

    Between the reminder and the gate events there is one position nobody
    watches, and it is the one a fleet actually dies in: the run sits at a
    step (or a select) that is the agent's own to move — no approval, no
    selection, no delegated answer outstanding — and the session driving it
    is not working. The reminder clock will not touch it on purpose: it
    types only into a *busy* session, because its job is to re-aim an agent
    mid-turn, not to restart one. The run event clock will not either: there
    is no gate to report, no round finished, and the session has not exited.
    So nothing at all happens, indefinitely, and the run looks exactly like
    one that is making progress.

    This clock is the one that pokes it. The trigger is *stopped at an
    unchanged actionable position*: the position key resets the timer when
    the run moves, and a session that reads busy resets it too — an agent
    working a long side quest at one step is the reminder's business, not
    this clock's. What lands is a machine-generated frame carrying the
    operator's configured message, so the text of the poke is a setting and
    not a constant baked into the daemon.

    Configuration is the machine's, read fresh every pass like the
    reminder's: ``cflow_ping`` (off by default), ``cflow_ping_interval`` and
    ``cflow_ping_message`` in :func:`store.daemon_config`, editable from
    ``claunch daemon config`` or the web UI's ``PUT /api/cflow/ping``. While
    it is off no timers are kept at all, so switching it on starts every run
    at zero rather than firing a backlog of pings for stalls that accrued in
    the dark.

    Why it is off by default, unlike the reminder: a run may be idle at an
    actionable step *legitimately* — a workflow whose intake step parks until
    a human hands it a goal is stopped, actionable and perfectly healthy. The
    daemon cannot tell that apart from an agent that forgot, so the operator
    turns this on for the fleet where the trade is worth it.
    """

    def __init__(self, manager, *, poll: float = PING_POLL) -> None:
        self.manager = manager
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        #: (cwd, scope) -> {"pos": position key, "at": monotonic seconds} —
        #: in memory only, same trade as the reminder's timers: a restart
        #: delays the next ping by one interval and replays nothing.
        self._seen: Dict[Tuple[str, str], dict] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                due = await asyncio.to_thread(self.scan, time.monotonic())
                for cwd, scope, block in due:
                    await self._deliver(cwd, scope, block)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow stall ping clock tick failed")

    def scan(self, now: float) -> List[Tuple[str, str, str]]:
        """Decide who is stalled. Blocking (config + every run's state); call
        it in a thread. Public for the tests, which own ``now`` there."""
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("cflow stall ping: config unreadable, skipping: %s", exc)
            return []
        enabled, interval = ping_policy(cfg)
        if not enabled:
            # Off: keep no timers, so turning it on does not fire a backlog.
            self._seen.clear()
            return []
        message = str(cfg.get("cflow_ping_message") or "").strip()
        due: List[Tuple[str, str, str]] = []
        live = set()
        for cwd, scope in cflow_state.known_runs():
            key = (cwd, scope)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                log.debug("cflow stall ping skipped %s/%s: %s", cwd, scope, exc)
                continue
            if not _actionable(payload):
                # A guardrail IS holding this one — an approval, a selection,
                # a responder's answer. That stop is the protocol working, and
                # the overseer already hears about it (RunEventClock).
                self._seen.pop(key, None)
                continue
            session = session_for(self.manager, cwd, scope)
            if session is None:
                # No session of this machine drives it (a CLI run), or the
                # driver has exited — the latter is 'orphaned', not a stall.
                self._seen.pop(key, None)
                continue
            pos = (
                payload.get("run"), payload.get("status"),
                payload.get("step_id"), payload.get("visit"),
            )
            entry = self._seen.get(key)
            working = self._working(session)
            if entry is None or entry["pos"] != pos or working:
                # Working, or moved, or first sight: arm, never fire. Only an
                # unbroken stretch of *stopped at the same position* counts.
                self._seen[key] = {
                    "pos": pos, "at": now, "working": working,
                    # Survives the re-arm: a reader asking "has this clock
                    # ever actually spoken here" is asking about the run, not
                    # about the stretch the re-arm just ended.
                    "fired_at": (entry or {}).get("fired_at"),
                }
                continue
            entry["working"] = working
            if now - entry["at"] >= interval:
                due.append((cwd, scope, ping_block(payload, message, now - entry["at"])))
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return due

    @staticmethod
    def _working(session) -> bool:
        """Whether somebody is at work in this session right now.

        Only ``idle`` is a stall. ``busy`` is an agent mid-turn (the
        reminder's audience), and ``starting`` is a session whose harness has
        not printed yet — pinging either says "you have stopped" to one that
        has not.
        """
        try:
            return session.status() != STATUS_IDLE
        except Exception:
            return True  # unreadable status: assume working, never ping blind

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        session = session_for(self.manager, cwd, scope)
        if session is None:
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow stall ping delivery to %r failed", scope)
            return
        if delivered:
            # Rearm only on success — a failed delivery keeps its debt and is
            # retried on the next poll, exactly like the reminder's.
            entry = self._seen.get((cwd, scope))
            if entry is not None:
                entry["at"] = time.monotonic()
                entry["fired_at"] = entry["at"]
            log.info("cflow stall ping delivered to %r (%s)", scope, cwd)

    @property
    def running(self) -> bool:
        """Whether the tick is alive — see :attr:`ReminderClock.running`."""
        return self._task is not None and not self._task.done()

    def timers(self, now: Optional[float] = None) -> Dict[Tuple[str, str], dict]:
        """What this clock is holding for each run, in plain seconds.

        Same contract as :meth:`ReminderClock.timers`, plus ``working``:
        this clock re-arms every pass while somebody is at work in the
        session, so a countdown drawn from ``armed_ago`` alone would look
        stuck at the top and read as broken. It is not counting down because
        there is nothing to count — that is the answer, and it has to travel
        with the number.
        """
        at = time.monotonic() if now is None else now
        out: Dict[Tuple[str, str], dict] = {}
        for key, entry in list(self._seen.items()):
            fired = entry.get("fired_at")
            out[key] = {
                "armed_ago": max(0.0, at - entry["at"]),
                "fired_ago": None if fired is None else max(0.0, at - fired),
                "working": bool(entry.get("working")),
            }
        return out


def ping_block(payload: dict, message: str, stalled_for: float) -> str:
    """The text a stopped session hears: the operator's message, framed.

    The frame is not decoration. A ping arrives in a session that had ended
    its turn, so it reads as a fresh user message unless it says otherwise —
    and an agent that mistakes it for one starts explaining itself instead of
    working. So the block names itself machine-generated, states the position
    and the two things the reader most needs to know about it (nothing is
    blocking it; the step is theirs to move), and carries the configured
    message as the operator's own words inside that.
    """
    minutes = max(1, int(stalled_for // 60))
    step = payload.get("step_id")
    visit = payload.get("visit")
    position = f"step '{step}'" + (f" (visit {visit})" if visit and visit > 1 else "")
    if payload.get("status") == "select":
        position = f"branch choice at {position}"
    elif _ask_reached_nobody(payload):
        position = f"{position}, on a delegated decision nobody was ever asked"
    lines = [
        "---",
        "# claunch cflow: stall ping -- machine-generated, not typed by the "
        f"user. This session has been stopped for ~{minutes} min and no gate "
        "is holding its run.",
        f"workflow: {payload.get('workflow')}",
        f"position: {position}",
    ]
    if message:
        lines.append(f"message: {message}")
    lines.append(
        "protocol: nothing is waiting on anybody else -- no approval, no "
        "selection, no delegated answer is outstanding, so this position is "
        "yours to move. If you are deliberately parked (waiting for a person "
        "to hand you a goal, or for work you cannot start yet), say what you "
        "are waiting for and stay put -- that is a real answer. Otherwise "
        "call the cflow 'status' tool for the current truth and carry on; "
        "'report' then 'next' is what advances it."
    )
    lines.append("---")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the window clock
# --------------------------------------------------------------------------- #
#: How often the window clock looks. A cadence is minutes; a quarter-minute
#: of lateness is invisible next to it, and the scan is one read per run.
WINDOW_POLL = 15.0


class WindowClock:
    """Releases held choices whose window has opened, and wakes the driver.

    A paced select option (``interval:`` on the option) parks the driver's
    choice until the interval since the option's last take has passed
    (:func:`cflow.engine.release_window`). The one agent that would notice
    the moment is the one that stopped its turn to wait for it — the same
    shape as an ask's timeout, so the same answer: the daemon carries the
    clock. What lands in the driver's terminal is a machine-generated frame
    saying the run moved and where, so an agent whose context lost the hold
    reads its position instead of guessing it.

    Nothing here decides anything. The choice was the agent's, made and
    journaled when it was held; this clock only lets it through at the
    moment the workflow declared. Without a daemon the hold is late, never
    lost: the driver's own next ``next`` past the moment releases it.
    """

    def __init__(self, manager, *, poll: float = WINDOW_POLL) -> None:
        self.manager = manager
        self.poll = poll
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                for cwd, scope, block in await asyncio.to_thread(self.scan):
                    await self._deliver(cwd, scope, block)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow window clock tick failed")

    def scan(self) -> List[Tuple[str, str, str]]:
        """Release every due hold on the machine. Blocking (it writes run
        state); call it in a thread. Public for the tests."""
        released: List[Tuple[str, str, str]] = []
        for cwd, scope in cflow_state.known_runs():
            try:
                moved = cflow_engine.release_window(cwd=cwd, scope=scope)
            except Exception as exc:
                # Includes the slot being locked by the agent mid-transition:
                # the window stays open, so the next tick is soon enough.
                log.debug("cflow window release skipped for %s/%s: %s", cwd, scope, exc)
                continue
            if moved:
                log.info(
                    "cflow window opened for %s/%s: %r at step %s -> %s",
                    cwd, scope, moved.get("option"), moved.get("step"),
                    moved.get("now_at") or moved.get("status"),
                )
                released.append((cwd, scope, window_block(moved)))
        return released

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        session = session_for(self.manager, cwd, scope)
        if session is None:
            # A CLI-driven run, or a driver that exited: the run has moved
            # regardless, and whoever picks it up reads the new position.
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow window notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow window notice delivered to %r (%s)", scope, cwd)


def window_block(moved: dict) -> str:
    """The text the driver hears when its held choice went through.

    Framed like the stall ping, for the same reason: it lands in a session
    that ended its turn, and an unframed line reads as a user message. It
    says what was released and that the run has ALREADY moved — the reader's
    next act is to read the new step, not to choose again.
    """
    position = (
        "the run finished"
        if moved.get("status") == "done"
        else f"step '{moved.get('now_at')}'"
    )
    return "\n".join(
        [
            "---",
            "# claunch cflow: window opened -- machine-generated, not typed by "
            "the user",
            f"workflow: {moved.get('workflow')}",
            f"released: your held choice {moved.get('option')!r} at step "
            f"'{moved.get('step')}' (held since {moved.get('held_since')}, "
            f"window opened {moved.get('opens_at')})",
            f"position: {position}",
            "protocol: the run has moved on the choice you recorded -- nothing "
            "was decided for you, and choosing again is not the next act. Call "
            "the cflow 'status' tool for the step you are now on and continue "
            "per the /cflow protocol.",
            "---",
        ]
    )


class TimerClock:
    """Fires a timed wait: a step's ``timer:`` moves the run on schedule.

    A timer step reports ``waiting_timer`` — a wait the run performs on its
    own, no agent action between fires. The one agent that would notice a
    fire is the one that ended its turn to wait for it, so the daemon
    carries the clock, the same shape as :class:`WindowClock`: each due
    fire MOVES the run (:func:`cflow.engine.fire_timer`) to the step's
    ``timer.then`` — a paid visit, delivered like any arrival — counting
    against the budget per round, and the fire past the budget moves the
    run to ``timer.after`` and closes the inner loop.

    Nothing here decides anything: the schedule and the budget are the
    workflow's, and the run's own state is the only reader, so no in-memory
    table needs to survive a restart — a fire that the daemon did not get to
    is simply late, delivered by the driver's own next ``next`` past the
    moment (the no-daemon path in the engine). Without a daemon nothing
    fires: a timed wait holds, which is the safe direction.
    """

    def __init__(self, manager, *, poll: float = DEFAULT_INTERVAL) -> None:
        self.manager = manager
        self.poll = poll
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                for cwd, scope, block in await asyncio.to_thread(self.scan):
                    await self._deliver(cwd, scope, block)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow timer clock tick failed")

    def scan(self) -> List[Tuple[str, str, str]]:
        """Fire every due timer on the machine. Blocking (it writes run
        state); call it in a thread. Public for the tests."""
        fired: List[Tuple[str, str, str]] = []
        for cwd, scope in cflow_state.known_runs():
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                # Includes the slot being locked by the agent mid-transition:
                # the fire stays armed, so the next tick is soon enough.
                log.debug("cflow timer skipped %s/%s: %s", cwd, scope, exc)
                continue
            if payload.get("status") != "waiting_timer":
                continue
            moved = cflow_engine.fire_timer(cwd=cwd, scope=scope)
            if not moved:
                continue
            log.info(
                "cflow timer fired %s/%s: %s %s/%s -> %s",
                cwd, scope, moved.get("step"), moved.get("fires"),
                moved.get("max"), moved.get("moved_to"),
            )
            fired.append((cwd, scope, timer_block(moved)))
        return fired

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        """The wake-up after a fire — the run has ALREADY moved."""
        session = session_for(self.manager, cwd, scope)
        if session is None:
            # A CLI-driven run, or a driver that exited: the run has moved
            # regardless, and whoever picks it up reads the new position.
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow timer notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow timer notice delivered to %r (%s)", scope, cwd)


def timer_block(moved: dict) -> str:
    """The text the driver hears when a timed wait moved the run.

    Framed like the window block, for the same reason: it lands in a session
    that ended its turn, and an unframed line reads as a user message. It
    says what fired and that the run has ALREADY moved — the reader's next
    act is to read the new step, not to answer the timer.
    """
    spent = int(moved.get("fires") or 0) > int(moved.get("max") or 0)
    count = "budget spent" if spent else f"{moved.get('fires')}/{moved.get('max')}"
    position = (
        "the run finished"
        if moved.get("moved_to") == "end"
        else f"step '{moved.get('moved_to')}'"
    )
    return "\n".join(
        [
            "---",
            "# claunch cflow: timer fired -- machine-generated, not typed by "
            "the user",
            f"workflow: {moved.get('workflow')}",
            f"fired: {moved.get('step')!r} ({count})",
            f"position: {position}",
            "protocol: the timed wait has moved the run on its own -- nothing "
            "was decided for you and there is nothing to confirm. Call the "
            "cflow 'status' tool for the step you are now on and continue per "
            "the /cflow protocol.",
            "---",
        ]
    )


class RoundStartClock:
    """Starts a recurring run's next round when its workflow opted in.

    ``recur: {auto: true}`` makes the repetition the daemon's: a finished
    round files its own next-round request (``by: recur``), and THIS clock
    performs the start (:func:`cflow.engine.auto_start_next_round`) instead
    of the driving agent — the loop keeps going while nobody is looking.
    What lands in the driver's terminal is a machine-generated frame saying
    the loop moved on, so a session that ended its turn learns a new round
    is running.

    Idempotent by construction, so there is no in-memory table to survive a
    restart: the request channel is single-use, a start consumes it, and a
    scan that finds nothing pending starts nothing. A request the clock did
    not get to between scans (daemon down, slot locked) survives and is
    performed by the next scan; a human's request, and a plain
    ``recur: true`` request, are never touched — those keep the
    driver-performs-start flow.
    """

    def __init__(self, manager, *, poll: float = DEFAULT_INTERVAL) -> None:
        self.manager = manager
        self.poll = poll
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                for cwd, scope, block in await asyncio.to_thread(self.scan):
                    await self._deliver(cwd, scope, block)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow round-start clock tick failed")

    def scan(self) -> List[Tuple[str, str, str]]:
        """Start every due auto-recur round on the machine. Blocking (it
        writes run state); call it in a thread. Public for the tests."""
        started: List[Tuple[str, str, str]] = []
        for cwd, scope in cflow_state.known_runs():
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                # Locked by the agent mid-transition: the pending request
                # survives, so the next tick is soon enough.
                log.debug("cflow round start skipped %s/%s: %s", cwd, scope, exc)
                continue
            pending = payload.get("pending_start") or {}
            if pending.get("by") != "recur" or not pending.get("auto"):
                continue
            started_round = cflow_engine.auto_start_next_round(cwd=cwd, scope=scope)
            if not started_round:
                continue
            log.info(
                "cflow round %s started for %s/%s: %s",
                started_round.get("round"), cwd, scope,
                started_round.get("workflow"),
            )
            started.append((cwd, scope, round_block(started_round)))
        return started

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        """The wake-up after a round started — the run has ALREADY moved."""
        session = session_for(self.manager, cwd, scope)
        if session is None:
            # A CLI-driven run, or a driver that exited: the new round is
            # running regardless, and whoever picks it up reads its position.
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow round notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow round notice delivered to %r (%s)", scope, cwd)


def round_block(started: dict) -> str:
    """The text the driver hears when the daemon started its next round.

    Framed like the window block, for the same reason: it lands in a session
    that ended its turn, and an unframed line reads as a user message. It
    says the round IS RUNNING — the reader's next act is to read the new
    position, not to start anything.
    """
    return "\n".join(
        [
            "---",
            "# claunch cflow: round started -- machine-generated, not typed "
            "by the user",
            f"workflow: {started.get('workflow')}",
            f"round: {started.get('round')}",
            f"position: step '{started.get('step')}'",
            "protocol: the daemon started this round for you -- it is "
            "already running and there is nothing to confirm. Call the cflow "
            "'status' tool for the step you are now on and continue per the "
            "/cflow protocol.",
            "---",
        ]
    )


# --------------------------------------------------------------------------- #
# the run event clock
# --------------------------------------------------------------------------- #
#: How often the event clock looks. The transitions it reports move at human
#: and agent speed (minutes), so the reminder clock's cadence is plenty.
EVENT_POLL = 20.0

#: Statuses in which the run is somebody's to advance — the complement of the
#: blocked/parked positions the events below report entering.
_RUNNING = ("step", "select")

#: Kill-on-end's timing (the same wind-down shape the beads hook uses — no
#: immediate kill). The done run's own final turn may still be running (the
#: agent advanced to end and is crossing the finish line), so the clock waits
#: out a busy driver before terminating, capped at ``cflow_kill_on_end_grace``
#: (below the beads default, this is the only clock reading it, read live).
_END_MAX = 120.0


class RunEventClock:
    """Tells a run's overseer when the run stops being its own agent's.

    A leader steering worker sessions has no push channel for their runs: the
    cflow MCP tools read only the caller's own run, and a worker that has
    stopped moving is silent in exactly the same way whether it is working,
    parked on a human gate, waiting for its next round's goal, or dead. This
    clock closes that gap: it watches every run on the machine (the same
    registry scan as the other clocks) and, when one crosses a transition
    worth an overseer's attention, types a short machine-generated fyi into
    the overseer's session.

    Three events, deliberately few:

    * **human-gate** — the run entered ``waiting_approval``/``waiting_selection``:
      blocked on a person, and the overseer may need to surface that to one.
    * **round-done** — a recurring run finished its round and filed the next
      one: the driver is out of work until somebody gives it a goal.
    * **orphaned** — the run is active but its driving session has exited:
      nobody is driving, and no transition will ever come.

    The overseer is the driver's spawn-tree parent when it is alive — in a
    leader/worker fleet the parent *is* the leader, and the daemon's manager
    already knows it. A driver with no live parent falls back to the local
    ``leader``-role member of its mesh; with neither, the event is logged and
    dropped (the web dashboard remains the human's view).

    Transitions are detected by diffing each run's position between polls, so
    the first sight of a run only arms it — a daemon restart does not replay
    events, and one that happens while a run sits at a gate misses that
    entry (the overseer's pull channel, ``claunch cflow status -t``, is the
    safety net; ``orphaned`` is state, not a transition, so it alone still
    fires after a restart). Delivery is a debt like the reminder's: a failed
    type-in is retried every poll until it lands. Unlike the reminder there
    is no busy-only hold — the point is to WAKE an idle overseer, not to
    steer a working one.

    The machine switch is ``cflow_events`` (``store.daemon_config()``), read
    fresh every pass like the reminder's. While it is off, positions are
    still tracked — silently — so turning it back on does not replay every
    transition that happened in the dark.

    One more duty, gated by its own switch (``cflow_kill_on_end``, default
    on): a finished ``done`` run of a **one-shot** workflow gets its session
    reaped. Nobody tells the clock the work is over — the wrap-up prose used
    to ask the agent itself to run ``kill-session``, and the most common
    incompletion in this fleet was the agent finishing the report and then
    stopping short of the kill. So the clock does it: on sight of the done
    position it writes a final "session ended" block into the session's own
    transcript (durably append+flush — a record that must survive the kill),
    then waits out the driver's current turn and terminates the session.
    Recurring workflows are exempt by construction (they file the next round
    instead of ending), a run that reached done with a pending next start is
    exempt (its driver is expected to perform it), and a session whose record
    carries ``keep_alive`` — the user said keep it — is recorded but not
    killed. A record that could not be written is reported loudly and the
    session is left alive: killing behind a record that did not land is the
    exact thing this mechanical end exists to prevent.

    Scan-budget note (five clocks already share one sequential pass over
    ``known_runs()``): this detection adds no pass of its own and no per-run
    cost beyond the orphaned branch's own two lookups — one dict get for the
    run state, one ``manager.get(scope)``. The idle-wait runs once per
    finished run as one bounded task, at most ``cflow_kill_on_end_grace``.
    """

    def __init__(self, manager, mesh=None, *, poll: float = EVENT_POLL) -> None:
        self.manager = manager
        self.mesh = mesh
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        #: (cwd, scope) -> last observed position key. In memory only, same
        #: trade as the reminder's timers.
        self._seen: Dict[Tuple[str, str], tuple] = {}
        #: (cwd, scope, run) whose orphaning was already reported.
        self._orphaned: Set[Tuple[str, str, str]] = set()
        #: (cwd, scope, run) whose ending was already recorded. Like the
        #: orphaned set: state fires on sight, once per run — a daemon restart
        #: that first-sees a done run still mops it up, and one that was in
        #: the middle of an ending does not re-record it. The switch off is
        #: still tracked (the ledger below gets the mark) so toggling it back
        #: on replays nothing.
        self._end_done: Set[Tuple[str, str, str]] = set()
        #: One-shot done runs whose session is queued for the end-sequence.
        #: scan() fills this; the loop drains it into :meth:`_finish_end`
        #: tasks. Same in-memory trade as the reminder's timers.
        self._end_pending: List[Tuple[str, str, str]] = []
        #: In-flight end-sequences — cancelled at shutdown, resumed by the
        #: state-on-sight rule on the next boot.
        self._end_tasks: Set[asyncio.Task] = set()
        #: Events found but not yet delivered — retried every poll.
        self._debt: List[dict] = []

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # In-flight end-sequences are dropped, not finished: the sessions
        # come back on restart with the same names and the done runs the
        # same positions, so the state-on-sight rule re-runs the sequence —
        # record, wait, kill — and a kill that never landed is completed.
        for t in list(self._end_tasks):
            t.cancel()
        if self._end_tasks:
            await asyncio.gather(*self._end_tasks, return_exceptions=True)
        self._end_tasks.clear()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                self._debt.extend(await asyncio.to_thread(self.scan))
                remaining: List[dict] = []
                for event in self._debt:
                    if not await self._deliver(event):
                        remaining.append(event)
                self._debt = remaining
                self._drain_ends()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow run event clock tick failed")

    def scan(self) -> List[dict]:
        """One pass over the registry: the transitions since the last one.
        Blocking (config + every run's state); call it in a thread. Public
        for the tests."""
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("cflow run events: config unreadable, skipping: %s", exc)
            return []
        enabled = bool(cfg.get("cflow_events"))
        kill_on_end = bool(cfg.get("cflow_kill_on_end", True))
        events: List[dict] = []
        live = set()
        for cwd, scope in cflow_state.known_runs():
            key = (cwd, scope)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                log.debug("cflow run events skipped %s/%s: %s", cwd, scope, exc)
                continue
            pending_by = str((payload.get("pending_start") or {}).get("by") or "")
            status = payload.get("status")
            pos = (
                payload.get("run"), status,
                payload.get("step_id"), payload.get("visit"), pending_by,
            )
            prev, self._seen[key] = self._seen.get(key), pos
            run_id = payload.get("run")
            # --- kill-on-end -------------------------------------------- #
            # A finished ONE-SHOT run returns its session's slot: done is
            # not a position an operator should have to notice. State, not a
            # transition — it fires on sight, once per run (a daemon restart
            # that first-sees the run at done still mops it up) — and is
            # tracked silently while the switch is off, so toggling it back
            # on replays nothing. The durable record goes FIRST, right here:
            # the kill stands behind it, and a record that did not land must
            # not end a session. Only the idle-wait and the kill are handed
            # to the loop as a task. Recurring workflows and runs done with a
            # pending next start both keep their driver.
            if (
                run_id and status == "done"
                and not payload.get("recur")
                and not pending_by
                and (cwd, scope, run_id) not in self._end_done
            ):
                self._end_done.add((cwd, scope, run_id))
                if kill_on_end:
                    session = session_for(self.manager, cwd, scope)
                    if session is not None:
                        block = end_block(
                            scope, run_id,
                            str(payload.get("workflow") or "?"),
                            keep_alive=bool(session.sdef.keep_alive),
                        )
                        if not session.append_wal(block):
                            # The one failure the contract forbids killing
                            # through — recorded, so this is not retried into
                            # an endless loop; the session stays alive and the
                            # operator can see why in the log.
                            log.error(
                                "cflow kill-on-end: could not durably record "
                                "the ending of %r (run %s); leaving the "
                                "session alive", scope, run_id,
                            )
                        else:
                            log.info(
                                "cflow kill-on-end: recorded the ending of %r "
                                "(run %s)", scope, run_id,
                            )
                            self._end_pending.append((cwd, scope, run_id))
                continue
            if (
                enabled and run_id and status not in ("done", "aborted")
                and (cwd, scope, run_id) not in self._orphaned
                and self._driver_gone(cwd, scope)
            ):
                # State, not a transition — fires on sight, once per run.
                self._orphaned.add((cwd, scope, run_id))
                events.append(self._event(cwd, scope, "orphaned", payload))
            if prev is None or prev == pos or not enabled:
                continue
            if (
                status in ("waiting_approval", "waiting_selection")
                or _ask_reached_nobody(payload)
            ) and prev[1] != status:
                # An ask nobody holds belongs here too. `approve()` resolves
                # it — `_blocked` calls the step "ask"-blocked whether or not
                # the question was ever opened — so a human really can lift
                # it. And they may be the only one who can: the reminder that
                # would otherwise poke the driver is typed only into a busy
                # session, so an idle or dead driver leaves this run silent
                # in the one state that never times out.
                events.append(self._event(cwd, scope, "human-gate", payload))
            elif (
                pending_by == "recur"
                and status in ("idle", "done")
                and prev[1] in _RUNNING
            ):
                events.append(self._event(cwd, scope, "round-done", payload))
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return events

    def _event(self, cwd: str, scope: str, kind: str, payload: dict) -> dict:
        return {
            "cwd": cwd,
            "scope": scope,
            "kind": kind,
            "block": event_block(scope, kind, payload),
        }

    def _driver_gone(self, cwd: str, scope: str) -> bool:
        """True when the run's scope names a managed session that has exited.

        Same containment rule as :meth:`ReminderClock._session_for`: the
        scope IS the session name, and the cwd must match. A scope no manager
        knows is a standalone/CLI run — not driven by a session, so never
        orphaned by one — and a matching name in another directory is
        somebody else's session.
        """
        try:
            session = self.manager.get(scope)
        except Exception:
            return False
        if not session.sdef.cwd:
            return False
        try:
            if cflow_state.resolve_cwd(session.sdef.cwd) != cwd:
                return False
        except Exception:
            return False
        return bool(session.exited)

    def _drain_ends(self) -> None:
        """Hand the queued end-sequences to the loop as tasks.

        Called from :meth:`_run` only — it needs a running loop. Each
        sequence is detached (a kill can wait out a turn, seconds); failures
        are caught inside :meth:`_finish_end`.
        """
        while self._end_pending:
            cwd, scope, run_id = self._end_pending.pop(0)
            task = asyncio.get_running_loop().create_task(
                self._finish_end(cwd, scope, run_id)
            )
            self._end_tasks.add(task)
            task.add_done_callback(self._end_tasks.discard)

    async def _finish_end(self, cwd: str, scope: str, run_id: str) -> None:
        """End a finished one-shot run's session: wait out its current turn
        (bounded), then kill unless it was kept alive.

        The durable record is already in the session's transcript — the scan
        that queued this wrote it first, and would not have queued a kill it
        could not record. What remains is timing and the flag: no immediate
        kill (the driver may still be finishing the turn that produced the
        done position), and the keep-alive flag re-read right beside the kill,
        so one set while the wait ran still protects the session. An exited
        session is left alone at every step — the kill verbs are idempotent,
        but nothing here needs to call one twice.
        """
        session = session_for(self.manager, cwd, scope)
        if session is None:
            return  # exited (or unmapped) while waiting — nothing to end
        cfg = store.daemon_config()
        grace = float(cfg.get("cflow_kill_on_end_grace", _END_MAX) or 0)
        if grace > 0:
            # The done run's own final turn may still be running — the agent
            # advanced to end and is crossing the finish line, and killing
            # mid-turn would cut whatever it is flushing. Wait for idle,
            # capped by grace. An already-idle driver has no turn to wait out
            # (nothing was delivered into its PTY; there is nothing to pick
            # up), so there the wait is zero.
            started = time.monotonic()
            while (
                not session.exited
                and session.status() == STATUS_BUSY
                and time.monotonic() - started < grace
            ):
                await asyncio.sleep(0.5)
        if session.exited:
            return
        if session.sdef.keep_alive:
            log.info(
                "cflow kill-on-end: %r recorded but left running (keep-alive)",
                scope,
            )
            return
        try:
            session.kill(force=False)
            self.manager.persist()
            log.info("cflow kill-on-end: ended %r (run %s)", scope, run_id)
        except Exception as exc:
            log.warning("cflow kill-on-end: ending %r failed: %s", scope, exc)

    async def _deliver(self, event: dict) -> bool:
        """Type the event into its overseer. True = settled (delivered, or
        dropped for want of anyone to tell); False keeps the debt."""
        target = self._recipient(event["cwd"], event["scope"])
        if target is None:
            log.info(
                "cflow run event (%s) about %r dropped: no overseer to tell",
                event["kind"], event["scope"],
            )
            return True
        try:
            delivered = await target.deliver(event["block"])
        except Exception:
            log.exception(
                "cflow run event delivery to %r failed", target.sdef.name
            )
            return False
        if delivered:
            log.info(
                "cflow run event (%s) about %r delivered to %r",
                event["kind"], event["scope"], target.sdef.name,
            )
        return bool(delivered)

    def _recipient(self, cwd: str, scope: str):
        """The live session to tell: spawn parent first, mesh leader after.

        The parent is answered by the manager alone and is the leader in the
        fleet shape this clock exists for. The fallback needs the mesh: the
        driver's memberships, disambiguated by the run's own ``mesh`` field
        when it has one, then the local ``leader``-role member. Ambiguity is
        a skip, never a guess.
        """
        try:
            parent = self.manager.get(scope).sdef.parent or ""
        except Exception:
            parent = ""
        if parent:
            try:
                candidate = self.manager.get(parent)
                if not candidate.exited:
                    return candidate
            except Exception:
                pass
        if self.mesh is None:
            return None
        try:
            memberships = self.mesh.meshes_for_session(scope)
        except Exception:
            return None
        names = sorted({m["mesh"] for m in memberships})
        if len(names) > 1:
            wanted = self._run_mesh(cwd, scope)
            names = [n for n in names if n == wanted] or names
        if len(names) != 1:
            if names:
                log.debug(
                    "cflow run event for %r: member of several meshes (%s) "
                    "and the run names none; skipping",
                    scope, ", ".join(names),
                )
            return None
        try:
            mesh = self.mesh.get(names[0])
        except Exception:
            return None
        for handle in sorted(mesh.members):
            member = mesh.members[handle]
            if member.role != "leader" or member.session == scope:
                continue
            if not self.mesh._is_local(mesh, member):
                continue
            try:
                candidate = self.manager.get(member.session)
            except Exception:
                continue
            if not candidate.exited:
                return candidate
        return None

    def _run_mesh(self, cwd: str, scope: str) -> str:
        """The mesh the run was started against, or ''. Best-effort — the
        field exists only when ``start`` was told one."""
        token = cflow_state.push_scope(scope)
        try:
            return str(cflow_state.load_state(cwd).get("mesh") or "")
        except Exception:
            return ""
        finally:
            cflow_state.pop_scope(token)


def end_block(scope: str, run_id: str, workflow: str, *, keep_alive: bool) -> str:
    """The final record a finished one-shot run's session carries.

    Appended to the session's own transcript (:meth:`Session.append_wal`)
    before the daemon ends it — durably, so the ending reads like part of the
    session itself for every future reader, whichever viewer or restart
    happens to look. That is the point of the block: the web view's injected
    ``[session exited (code N)]`` line only shows while someone is attached;
    this one is in the record.
    """
    lines = [
        "---",
        "# claunch: session ended -- machine-generated, not typed by the user",
        f"session: {scope}",
        f"run: {run_id}",
        f"workflow: {workflow} (one-shot — no next round)",
    ]
    if keep_alive:
        lines += [
            "outcome: keep-alive is set — the round's record is closed above "
            "and the session was left running",
            "resume: `claunch keep-alive " + scope + " off` to permit ending",
        ]
    else:
        lines += [
            "outcome: the run finished, this record was written first, and "
            "the session is ended to return its slot",
            "resume: `claunch respawn " + scope + "`",
        ]
    lines.append("---")
    return "\n".join(lines)


def event_block(scope: str, kind: str, payload: dict) -> str:
    """The text an overseer hears about a watched run, composed per event.

    An fyi, not an order: the overseer's own workflow says what (if anything)
    to do about it, so the block reports the fact, points at the pull channel
    for the rest, and — for the gate — restates whose the gate is, because
    the reader is an agent and the gate is not its to clear.
    """
    workflow = payload.get("workflow") or "?"
    step = payload.get("step_id")
    lines = [
        "---",
        "# claunch cflow: run event -- machine-generated. A run you oversee "
        "changed state; fyi, your own protocol decides what to do with it.",
        f"session: {scope}",
    ]
    if kind == "human-gate":
        unrouted = _ask_reached_nobody(payload)
        what = (
            "approval"
            if payload.get("status") == "waiting_approval" or unrouted
            else "selection"
        )
        if unrouted:
            lines.append(
                f"event: parked at {workflow}/{step} on a delegated {what} "
                "that was never put to anyone -- it reads as delegated, but "
                "no responder holds it"
            )
        else:
            lines.append(f"event: waiting on a human {what} at {workflow}/{step}")
        prompt = str(payload.get("gate") or payload.get("prompt") or "").strip()
        if prompt:
            lines.append(f"prompt: {prompt.splitlines()[0]}")
        if unrouted:
            lines.append(
                "note: two things end this and neither is you -- the run's "
                "own agent calling 'next' (which opens the question and "
                "routes it), or a person approving it outright. Nudge the "
                "driver, or surface it to the user; do not answer it for "
                "them."
            )
        else:
            lines.append(
                "note: the gate is a person's to answer and the wait is that "
                "run's protocol -- do not clear it for them. If it stays "
                "unanswered, surface it to the user."
            )
    elif kind == "round-done":
        lines.append(
            f"event: finished its round of {workflow}; recur filed the next "
            "one, so the session is waiting for a goal"
        )
    elif kind == "orphaned":
        lines.append(
            f"event: run {payload.get('run')} of {workflow} is active at "
            f"step '{step}' but its session has exited -- nobody is driving"
        )
    lines.append(
        f"read it yourself: claunch cflow status -t {scope} --json "
        f"(details: claunch cflow journal -t {scope})"
    )
    lines.append("---")
    return "\n".join(lines)


def run_summary(name: str, cwd: str) -> Optional[dict]:
    """The cflow run session ``name`` drives in ``cwd``, compactly — or None.

    The same containment rule as the clocks, read in the other direction: the
    scope IS the session name and the run must live in the session's own
    directory. Serves the ``children`` API view, so an overseer's roster can
    carry each child's run position without a second tool."""
    if not name or not cwd:
        return None
    try:
        resolved = cflow_state.resolve_cwd(cwd)
        if name not in cflow_state.scopes_in(resolved):
            return None
        payload = cflow_engine.status(resolved, scope=name)
    except Exception:
        return None
    pending = payload.get("pending_start")
    if payload.get("status") == "idle" and not pending:
        return None
    out: dict = {"status": payload.get("status")}
    for src, dst in (
        ("workflow", "workflow"), ("run", "run"),
        ("step_id", "step"), ("started_at", "started_at"),
    ):
        if payload.get(src):
            out[dst] = payload[src]
    if pending:
        out["pending_start"] = {
            "workflow": pending.get("workflow"), "by": pending.get("by"),
        }
    return out
