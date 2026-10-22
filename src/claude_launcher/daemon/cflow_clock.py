"""The clocks cflow cannot carry itself.

Everything else in cflow happens because somebody called a tool: the agent
advances, a human approves, a responder answers. Six things have nobody to
call them, so the daemon carries all six, scanning the same machine-local
run registry the dashboard lists runs from:

* :class:`AskClock` — a delegated decision's ``timeout``. The one agent that
  would notice an expiry is the one stopped waiting for the answer.
* :class:`CflowReminderSource` — the cflow contribution to the daemon's
  session reminder service: the current step instructions after a position
  stops moving, and the opposite ``awaits`` signal when a measured condition
  changes.  It owns cflow's position key and full/repeat state; delivery and
  the other session-level sources live in :mod:`.session_reminder`.
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
* :class:`TriggerClock` — the daemon side effects a step declared with
  ``triggers:``. Two capabilities of this daemon — re-taking a session's Y/N
  status checks, recomposing its LLM briefing — have no tool a run could
  call, so a workflow that wanted one used to ask its agent in prose. The
  step names the capability and the moment instead, and this performs it.

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
import os
import platform as platform_mod
import subprocess
import tempfile
import time
from collections import deque
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from .. import store
from ..cflow import engine as cflow_engine, landing as cflow_landing, model as cflow_model, state as cflow_state
from ..cflow2 import ir as cflow2_ir, runtime as cflow2_runtime, store as cflow2_store
from . import actors, briefing, instance_manifest, landing_route, session_procs, status_checks
from . import paths as daemon_paths
from .session import STATUS_BUSY, STATUS_IDLE, delivery_wake

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
                    if moved.get("moved_to"):
                        log.info(
                            "cflow ask %s at step %s expired with %s; the "
                            "declared default took it and the run moved to %s",
                            moved.get("ask"),
                            moved.get("step"),
                            moved.get("expired") or "nobody",
                            moved["moved_to"],
                        )
                    else:
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
    for cwd, scope, run in cflow_state.known_slots():
        try:
            result = cflow_engine.expire_ask(cwd=cwd, scope=scope, run=run)
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

#: How much work after a delivered reminder still counts as "the session only
#: answered the reminder". A terminal that took the message, rendered it,
#: replied and went quiet again spends seconds here; a session that actually
#: worked on the step spends minutes. Measured on the fleet: the codex
#: sessions whose reminders repeated for 21 hours answered each one in
#: 0.4–7.2 s of turn time (session s602, 2026-09-19).
REMINDER_WORK_GRACE = 60.0

#: How long after a delivery its own effects on the terminal keep landing.
#: ``deliver`` returns once the paste has been rendered and the Enter keys
#: written, so the submit repaint and the turn the message provokes are both
#: still ahead of it. A baseline for "has this terminal moved since I last
#: spoke" that is taken at that moment is therefore always one repaint stale,
#: and the comparison it feeds can never come out equal. Re-anchoring the
#: baseline this long after the delivery is what makes the comparison mean
#: what it says.
REMINDER_SETTLE = 30.0

#: How long a terminal has to have been moving, since the last reminder
#: settled, before the next one is allowed to fire. This is a condition on
#: sending, not another way of suppressing: the timer says a reminder is due,
#: and this says the session it would go to is one that has been working.
#:
#: Counted in whole scans (:data:`REMINDER_POLL`), so 60 s means the session
#: was seen moving on four separate looks — long enough that a single burst
#: cannot carry it, short enough that a minute of real work does.
REMINDER_MIN_MOVEMENT = 60.0

#: How long a session's screen has to have stood still, measured at delivery
#: time, before its repeating reminder is held instead of typed in
#: (claunch-pumne).
#:
#: The rule above asks whether a *repeat* has been earned; this one asks,
#: for every delivery of every source, whether the terminal has anything new
#: to read. The busy/idle gate cannot answer that: a turn parked on a long
#: command, or a TUI animating a spinner, reads as busy for as long as the
#: command or the spinner runs, so reminders kept landing in terminals that
#: had not drawn a line in minutes. A held reminder takes the same
#: ``held_at`` a stopped session gets, so the existing resume rule re-arms a
#: fresh interval when the screen moves again instead of replaying it
#: (:class:`SessionReminderService`).
#:
#: Seconds; ``daemon.reminder_stall_hold`` overrides it and 0 turns the hold
#: off. Read live, like the reminder intervals.
REMINDER_STALL = 15.0


def _movement_readings(session) -> tuple:
    """This terminal's two independent "did it move" readings.

    ``(marker, output)``, either of which may be ``None`` when the session
    does not expose it. They fail in opposite directions, which is why both
    are taken: the screen marker ignores animated rows but is restamped by a
    resize, and the PTY stamp cannot be moved by a resize but advances on
    every animation frame.
    """
    marker = None
    reader = getattr(session, "last_activity_at", None)
    if callable(reader):
        try:
            marker = reader()
        except Exception:  # noqa: BLE001 - activity is decoration only
            marker = None
    return marker, session_output_at(session)


def observe_movement(session, entry: dict, now: float) -> None:
    """Add this scan's interval to the entry's movement total, if it moved.

    A scan counts as movement only when *every* reading the session offers
    has changed since the previous scan. Requiring all of them is what keeps
    the two false positives out: an animating TUI moves the PTY stamp while
    the screen marker stands still, and a resize moves the marker while the
    PTY stamp stands still. Neither is the session doing work.

    Accumulated rather than sampled, because the question is how long the
    terminal has been moving since the last reminder, and a single look
    cannot answer that.
    """
    # ``moved_for`` is the tracking switch as well as the total: ``None``
    # means there is no delivery to measure from -- nothing has been sent
    # here yet, or its effects are still landing, or the position moved and
    # the next reminder is a first one rather than a repeat.
    if session is None or entry.get("moved_for") is None:
        return
    marker, output = _movement_readings(session)
    seen_at = entry.get("seen_at")
    changed = []
    if marker is not None:
        changed.append(marker != entry.get("seen_activity"))
    if output is not None:
        changed.append(output != entry.get("seen_output_at"))
    if changed and all(changed) and seen_at is not None:
        # One look credits at most one scan's worth, however long ago the
        # previous look was. A gap larger than that is the scan loop having
        # stalled or the machine having slept, and neither is evidence that
        # the terminal was moving the whole time -- a suspended machine that
        # came back to a changed screen would otherwise clear the threshold
        # in a single step (the daemon log has such a gap on 2026-09-19).
        entry["moved_for"] += min(max(0.0, now - seen_at), REMINDER_POLL)
    entry["seen_activity"] = marker
    entry["seen_output_at"] = output
    entry["seen_at"] = now


def moved_enough(session, entry: dict) -> bool:
    """Whether this terminal has moved long enough to be worth speaking to.

    Waived in the two cases where the total cannot mean anything: there is
    no delivery to measure from (``moved_for`` is ``None`` -- see
    :func:`observe_movement`), and a session that reports neither reading
    (older or fake implementations keep their original cadence rather than
    falling silent).
    """
    moved_for = entry.get("moved_for")
    if moved_for is None:
        return True
    marker, output = _movement_readings(session)
    if marker is None and output is None:
        return True
    return moved_for >= REMINDER_MIN_MOVEMENT


def session_output_at(session) -> Optional[str]:
    """When the session's PTY last produced a byte, or ``None``.

    The screen marker and this answer different questions. The marker is
    taken off the *rendered grid*, and the grid can move without the program
    writing anything — a resize restamps every row, an attaching viewer
    renders a parked tail, a restored session is seeded from its log. Any of
    those reads as activity and, for the two seconds after, as a busy
    session. The PTY stamp cannot be moved that way: it advances only when
    the program under the terminal actually wrote something.

    So a terminal whose PTY has not moved has done nothing, whatever the
    grid and the busy/idle heuristic say about it.
    """
    value = getattr(session, "last_output_at", None)
    if callable(value):  # a double may expose it as a method
        try:
            value = value()
        except Exception:  # noqa: BLE001 - the stamp is decoration only
            return None
    return value if isinstance(value, str) and value else None


def nothing_moved_since_settle(session, entry: dict) -> bool:
    """Whether this terminal has been still since the last reminder landed.

    Two independent readings, either of which settles it. The screen marker
    says no non-animated row has changed; the PTY stamp says no byte has been
    written at all. The second catches what the first cannot — a session that
    reads busy because its grid was restamped by something other than the
    program, such as a resize.
    """
    if entry.get("fired_at") is None or not entry.get("settled", False):
        # Never spoken to here, or the baseline is still the pre-delivery
        # one. Either way there is no "since the last reminder" to measure.
        return False
    marker = entry.get("activity")
    reader = getattr(session, "last_activity_at", None)
    if marker is not None and callable(reader):
        try:
            if reader() == marker:
                return True
        except Exception:  # noqa: BLE001 - activity is decoration only
            pass
    output = entry.get("output_at")
    return output is not None and session_output_at(session) == output


def settle_activity(session, entry: dict, now: float, *, due: bool) -> None:
    """Re-anchor one entry's no-progress baseline once a delivery has landed.

    Called from the scan, which runs every :data:`REMINDER_POLL` seconds —
    far more often than any reminder interval, so in the daemon this always
    happens on a scan of its own, minutes before the entry comes due again.

    ``due`` is the guard that keeps it that way. Re-anchoring on the very
    scan that is about to compare against the baseline would compare a
    reading with itself and suppress every repeat, including the ones a
    working session has earned. A baseline that was never settled stays as
    it was, which errs toward delivering.

    Idempotent per delivery: ``settled`` is cleared when the reminder is
    marked as fired and set here, so the baseline moves once and then stays
    put until the next delivery. That is the point — the screen the session
    was left on after taking the message is what later scans compare against.
    """
    if due or entry.get("settled", True):
        return
    fired_at = entry.get("fired_at")
    if fired_at is None or (now - fired_at) < REMINDER_SETTLE:
        return
    if session is None:
        return
    reader = getattr(session, "last_activity_at", None)
    if callable(reader):
        try:
            activity = reader()
        except Exception:  # noqa: BLE001 - activity is decoration only
            activity = None
        if activity is not None:
            entry["activity"] = activity
    entry["output_at"] = session_output_at(session)
    entry["settled"] = True
    # Movement is counted from here, not from the delivery: what the reminder
    # itself made the terminal do is not the session working.
    entry["seen_activity"] = entry.get("activity")
    entry["seen_output_at"] = entry["output_at"]
    entry["seen_at"] = now
    entry["moved_for"] = 0.0


def session_idle_for(session) -> Optional[float]:
    """Seconds this session's screen has been still, or ``None``.

    ``None`` covers both "the session is working right now" and "this session
    object does not expose the API" (older/fake implementations), and callers
    treat both the same way: no suppression is inferred from a number that
    was never measured.
    """
    reader = getattr(session, "idle_since", None)
    if not callable(reader):
        return None
    try:
        value = reader()
    except Exception:  # noqa: BLE001 - idle time is decoration only
        return None
    return None if value is None else float(value)


def stall_hold_seconds(cfg: Optional[dict] = None) -> float:
    """The configured stall-hold threshold in seconds; 0 means off.

    ``reminder_stall_hold`` is read live on every delivery, like the reminder
    intervals. A value that cannot be read as a number falls back to the
    default rather than to 0: a broken config must not silently switch the
    hold off, which is the state that floods the terminals.
    """
    try:
        value = float((cfg or {}).get("reminder_stall_hold", REMINDER_STALL))
    except (TypeError, ValueError):
        return REMINDER_STALL
    return max(0.0, value)


def session_screen_idle_for(session) -> Optional[float]:
    """Seconds the session's screen has been still, or ``None``.

    Read off :meth:`Session.screen_idle_for`, which answers while the session
    is busy as well — the state this gate is about, and the one
    :func:`session_idle_for` cannot see. ``None`` means nothing was measured
    (no sample yet, a parked grid, or a session object that does not expose
    the API), and no suppression is inferred from it.
    """
    reader = getattr(session, "screen_idle_for", None)
    if not callable(reader):
        return None
    try:
        value = reader()
    except Exception:  # noqa: BLE001 - idle time is decoration only
        return None
    return None if value is None else float(value)


def stalled_for_reminder(session, hold: float) -> Optional[float]:
    """The still seconds that hold this session's reminder, or ``None``.

    Returns the measurement only when it clears ``hold`` (``hold <= 0``
    disables the gate), so a caller can decide and log the number without
    reading the clock twice.
    """
    if hold <= 0:
        return None
    idle = session_screen_idle_for(session)
    if idle is None or idle < hold:
        return None
    return idle


def answered_only_the_reminder(
    session, now: float, fired_at: Optional[float]
) -> bool:
    """Whether nothing but the last reminder itself has moved this terminal.

    The activity marker alone cannot answer this. A reminder is typed into
    the session, rendered there and usually answered, so the marker moves
    every single time one is delivered — which is why comparing it against a
    baseline taken when the delivery returned never suppresses anything in
    production (the submit repaint and the turn both land after that
    baseline).

    Durations do answer it. ``now - fired_at`` is how long ago the session
    was last spoken to, and ``idle_since`` is how long its screen has been
    still; the difference is how long the terminal moved in between. When
    that is under :data:`REMINDER_WORK_GRACE`, the only thing that happened
    since the last reminder was the session taking delivery of it.
    """
    if fired_at is None:
        return False
    idle_for = session_idle_for(session)
    if idle_for is None:
        return False  # working right now, or a session that cannot say
    elapsed = now - fired_at
    if elapsed <= 0:
        return False
    return (elapsed - idle_for) <= REMINDER_WORK_GRACE


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

    Split out of :meth:`CflowReminderSource.scan` because the dashboard has to
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


class CflowReminderSource:
    """The cflow source consumed by the session reminder service.

    Sessions forget the /cflow protocol the way they forget everything else —
    compaction, distraction, a long side quest — and a forgotten run does not
    fail, it just sits. This source watches every run on the machine and, when
    one has held the same agent-actionable position for its reminder interval,
    yields that position's reminder to the session-level coordinator.

    *No progress* is the trigger, not the calendar: the position key
    (run, status, step, visit) resets the timer whenever it changes, so an
    advancing run yields nothing, while a stalled position becomes due once
    its interval elapses.  Repeats at that same position are not automatic:
    a repeat is delivered only while the session's meaningful screen
    activity proves it is still working, and a terminal that has not moved
    since the last reminder is re-armed instead — the same no-progress rule
    the Role source applies.

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

    :class:`daemon.session_reminder.SessionReminderService` applies the busy
    gate, batches this source with Role, and advances ``restated`` only after
    the combined delivery succeeds.

    Configuration is read fresh on every pass: the machine defaults
    (``cflow_reminder`` / ``cflow_reminder_interval``) come from
    ``store.daemon_config()`` — so a ``claunch daemon config`` edit or the web
    UI's PUT applies by the next tick, no restart — and each run may override
    both in its own state (:func:`cflow.engine.set_reminder`).
    """

    def __init__(self, manager) -> None:
        self.manager = manager
        #: (cwd, scope) -> {"pos": position key, "at": monotonic seconds,
        #: "activity": meaningful-screen marker or None} — in memory only.
        #: A daemon restart forgets the timers, which merely delays each
        #: run's next reminder by one interval; persisting them would buy
        #: nothing worth a state write per tick.
        self._seen: Dict[Tuple[str, str], dict] = {}

    def scan(self, now: float) -> List[Tuple[str, str, str, str]]:
        """Decide who is due, as ``(cwd, scope, block, kind)``.

        Blocking — config, every run's state, and any probe whose poll
        interval has elapsed — so call it in a thread. Public for the tests,
        which own ``now`` there (and, through it, the probe spacing).

        ``kind`` is ``"reminder"`` or ``"signal"``. The session-level
        service applies their different delivery rules.
        """
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("cflow reminder: config unreadable, skipping: %s", exc)
            return []
        due: List[Tuple[str, str, str, str]] = []
        live = set()
        for cwd, scope, payload, v2 in self._runs(live):
            key = (cwd, scope)
            if not (_waits_on_driver(payload) if v2 else _actionable(payload)):
                self._seen.pop(key, None)
                continue
            enabled, interval = reminder_policy(payload, cfg)
            awaits = {} if v2 else payload.get("awaits") or {}
            if not enabled or (interval <= 0 and not awaits.get("probe")):
                # `enabled` is this clock's master switch: off, it says
                # neither of the two things it can say. An interval of zero
                # turns off only the clock half, which leaves the one
                # configuration in which this clock speaks nothing but news —
                # a step that declares what it waits for, and silence until
                # that changes.
                self._seen.pop(key, None)
                continue
            pos = v2_position(payload) if v2 else (
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
                session = self._session_for(cwd, scope)
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
                    # The meaningful-screen marker when this position was
                    # reached — the baseline the no-progress suppression
                    # compares against. ``None`` when the session does not
                    # expose the activity API, which disables suppression.
                    "activity": (
                        None if session is None else self._session_activity(session)
                    ),
                    # A position the run just reached is its own baseline;
                    # there is no delivery of ours still landing on it, and
                    # its first reminder is not a repeat to be earned.
                    "settled": True,
                    "moved_for": None,
                }
                self._seen[key] = entry
            if awaits.get("probe"):
                before, after = entry["probe"], self._measure(
                    cwd, scope, awaits, entry, now
                )
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
            # The previous reminder's own repaint and the turn it provoked
            # land after ``deliver`` returned, so the baseline recorded then
            # is stale.  Move it onto the screen the delivery actually left
            # behind, once, before any comparison is made against it.
            due_now = interval > 0 and now - entry["at"] >= interval
            session = self._session_for(cwd, scope)
            settle_activity(session, entry, now, due=due_now)
            observe_movement(session, entry, now)
            if due_now and not moved_enough(session, entry):
                # Due, but this terminal has not been working. Left due
                # rather than re-armed, so it speaks as soon as it is.
                continue
            if due_now:
                # A repeat is useful after the session has made progress, but
                # restating a position a second time on a terminal that has
                # not moved since the last reminder only feeds the pending
                # queue (the same no-progress rule the Role source applies).
                # Re-arm the timer when the screen marker proves nothing
                # changed. ``None`` here means the session does not expose
                # the activity API (older/fake implementations), and those
                # retain the original repeat cadence.
                if entry.get("restated"):
                    activity = (
                        None if session is None else self._session_activity(session)
                    )
                    unmoved = activity is not None and activity == entry.get("activity")
                    if not unmoved and session is not None:
                        # The settled baseline (screen marker and PTY stamp),
                        # then the duration reading as a third opinion.
                        unmoved = nothing_moved_since_settle(
                            session, entry
                        ) or answered_only_the_reminder(
                            session, now, entry.get("fired_at")
                        )
                    if unmoved:
                        entry["at"] = now
                        entry["held_at"] = None
                        continue
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
                short = entry.get("restated") and (v2 or not _ask_reached_nobody(payload))
                # ``or entry["at"]`` rather than a bare lookup: the arming is
                # not the only writer of this table any more, and a staleness
                # figure that falls back to the arming is off by at most one
                # interval, where a KeyError here would silence the clock.
                since = now - (entry.get("arrived_at") or entry["at"])
                if v2:
                    due.append((cwd, scope, v2_reminder_block(
                        payload, interval, since if short else None), "reminder"))
                    continue
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
        for cwd, scope, run in cflow_state.known_sub_runs():
            key = (cwd, scope, run)
            live.add(key)
            self._scan_sub(cwd, scope, run, key, now, due)
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return due

    def _runs(self, live: set):
        """Every run this clock reminds about, as ``(cwd, scope, payload, v2)``.

        v1 runs come from their registry. A v2 run has none, so each live
        session's own slot (scope = the session's name) is read for one --
        before claunch-ccoof this clock read only v1 and no v2 driver was
        ever reminded. Like every reminder this one is typed only into a
        busy session (session_reminder holds it otherwise): it re-aims an
        agent mid-turn, and an idle driver is woken by the cflow2 clock's
        moved_block or the resume nudge instead. A run whose recorded driver
        is another session is left out -- the reminder goes to the slot's
        own session, which is not the one to move it. A slot holding both is
        v1's while v1 has the agent's position, and v2's otherwise: yielding
        both would arm and drop the same timer on every pass.
        """
        v2_runs: Dict[Tuple[str, str], dict] = {}
        try:
            sessions = list(self.manager.list())
        except Exception:  # noqa: BLE001 -- a manager without a listing
            sessions = []
        for session in sessions:
            if getattr(session, "exited", False) or not session.sdef.cwd:
                continue
            scope = session.sdef.name
            cwd = session.sdef.cwd
            try:
                cwd = cflow_state.resolve_cwd(session.sdef.cwd)
                if not cflow2_store.RunStore(cwd, scope).exists():
                    continue
                payload = cflow2_runtime.status(cwd=cwd, scope=scope, tick=False)
            except Exception as exc:  # noqa: BLE001 -- FlowError, StoreError, OSError
                log.debug("cflow reminder skipped v2 %s/%s: %s", cwd, scope, exc)
                continue
            if (payload.get("status") in cflow2_runtime.LIVE_STATUSES
                    and (payload.get("driver") or scope) == scope):
                v2_runs[(cwd, scope)] = payload
        for cwd, scope in cflow_state.known_runs():
            key = (cwd, scope)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                log.debug("cflow reminder skipped %s/%s: %s", cwd, scope, exc)
                continue
            if key in v2_runs:
                if not _actionable(payload):
                    continue
                del v2_runs[key]
            yield cwd, scope, payload, False
        for (cwd, scope), payload in v2_runs.items():
            live.add((cwd, scope))
            yield cwd, scope, payload, True

    def _scan_sub(
        self, cwd: str, scope: str, run: str, key: tuple, now: float, due: list
    ) -> None:
        """A sub run's share of the scan: its ``awaits`` signal, and nothing
        else. A sub run shares its terminal with the main run, and the step
        reminder is the main run's to give — a second reminder stream for a
        side track (a stack that stands for the whole session) would double
        the restatements this clock works to keep down. What a sub run does
        need is to be told when what it waits for arrives — the other half of
        a milestone handshake (``awaits: {main}``) is the case in point."""
        try:
            payload = cflow_engine.status(cwd, scope=scope, run=run)
        except Exception as exc:
            log.debug("cflow signal skipped %s/%s/%s: %s", cwd, scope, run, exc)
            return
        awaits = payload.get("awaits") or {}
        if not _actionable(payload) or not awaits.get("probe"):
            self._seen.pop(key, None)
            return
        pos = (payload.get("run"), payload.get("status"),
               payload.get("step_id"), payload.get("visit"))
        entry = self._seen.get(key)
        if entry is None or entry["pos"] != pos:
            # A new position's first sample is its baseline, never a signal.
            entry = {"pos": pos, "at": now, "probed_at": None, "probe": None}
            self._seen[key] = entry
        before = entry["probe"]
        after = self._measure(cwd, scope, awaits, entry, now, run=run)
        if after is None:
            return
        entry["probe"] = after
        if before is not None and before["code"] != after["code"]:
            entry["at"] = now
            due.append((cwd, scope, signal_block(payload, before, after), "signal"))
        try:
            stuck = cflow_engine.sync_deadlock(cwd=cwd, scope=scope, run=run)
        except Exception as exc:
            log.debug("cflow deadlock check skipped %s/%s/%s: %s", cwd, scope, run, exc)
            stuck = None
        if stuck:
            log.warning("cflow sync deadlock %s/%s: %s", cwd, scope, stuck)
            due.append((cwd, scope, deadlock_block(stuck), "signal"))

    def _measure(
        self, cwd: str, scope: str, awaits: dict, entry: dict, now: float,
        run: Optional[str] = None,
    ) -> Optional[dict]:
        """This position's standing measurement, re-taken when due.

        Three returns, and only one of them is a fresh subprocess:

        * between samples — the previous answer, unchanged. Returning it
          rather than ``None`` is what keeps the clock quiet in the gaps: to
          the caller ``None`` means *broken*, and a gap is not that.
        * a fresh dict — the poll interval elapsed and the probe ran.
        * ``None`` — it ran and could not answer (see
          :func:`cflow.engine.run_probe`).

        ``scope`` is threaded in for the probe's environment, not for anything
        this method decides: the subprocess has to run as the run's own session
        or it resolves "which checkout am I" to the daemon's
        (:func:`cflow.engine.probe_env`).

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
            awaits["probe"],
            cwd,
            min(timeout, cflow_model.MAX_AWAITS_TIMEOUT),
            # Whose run this is, spelled out because this process cannot be
            # asked. The daemon holds one `CLAUNCH_SESSION` -- the terminal's
            # that started it -- and a probe inheriting it measures that
            # session's checkout for every run on the machine
            # (:func:`cflow.engine.probe_env`).
            scope=scope,
            # And which of its runs: a sub run's `awaits: {main}` probe reads
            # its own consumption record through this.
            run=run,
        )

    @staticmethod
    def _session_activity(session) -> Optional[str]:
        """The session's meaningful-screen activity marker, or ``None``.

        Mirrors the Role source's reader.  ``None`` means the session does
        not expose the API (older/fake session implementations), and there
        the no-progress suppression is disabled rather than guessing.
        """
        reader = getattr(session, "last_activity_at", None)
        if not callable(reader):
            return None
        try:
            return reader()
        except Exception:  # noqa: BLE001 - activity is decoration only
            return None

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
        whose entire prize is one extra reminder at a position that has
        already been reminded.
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


def ReminderClock(manager, mesh=None, *, poll: float = REMINDER_POLL):
    """Compatibility constructor for the session-level reminder service.

    Older callers and downstream tests imported ``ReminderClock`` from this
    module.  The runtime owner now lives in :mod:`.session_reminder`; keeping
    this lazy constructor preserves that import without pulling session-level
    delivery back under cflow or creating an import cycle at module load.
    """
    from .session_reminder import SessionReminderService

    return SessionReminderService(manager, mesh, poll=poll)


def session_name(scope: str) -> str:
    """The session name a run's scope stands for: a v2 slot of this daemon
    (``s15@main``) without its daemon; another daemon's v2 slot names no
    session here (""). A v1 scope is the name itself."""
    if cflow2_store.DAEMON_SEP not in str(scope or ""):
        return scope
    return cflow2_store.own_name(scope) or ""


def session_for(manager, cwd: str, scope: str, *, held: bool = False):
    """The live session a run maps 1:1 to, or None.

    Same containment rule as the dashboard's ``_scope_sessions``: the scope
    IS the session name, and the cwd must match so a name reused in another
    directory is never typed into by that directory's run. Shared by every
    clock here that types into a driver, so the containment rule is stated
    once and cannot drift between them.

    ``held=True`` also answers with a held record (asleep, not gone): for a
    caller that wakes it to deliver (:func:`waking_deliver`, claunch-t1r9j).
    """
    try:
        session = manager.get(session_name(scope))
    except Exception:
        return None
    if session.exited and not (held and is_held(session)):
        return None
    if not session.sdef.cwd:
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


def _waits_on_driver(payload: dict) -> bool:
    """A v2 run's "the agent is the one who can move it": the cflow2
    clock's own rule, so the two clocks cannot disagree about it."""
    from . import cflow2_clock

    return cflow2_clock._waits_on_driver(payload)


def v2_position(payload: dict) -> tuple:
    """A v2 run's position key: what it waits on the driver for. Instance
    ids carry their visit (``a@3``), so a loop's next lap is a new position."""
    tasks = sorted(str(t.get("instance")) for t in payload.get("tasks") or [])
    choices = sorted(str(c.get("instance")) for c in payload.get("choices") or []
                     if c.get("chooser") == cflow2_ir.CHOOSER_AGENT and not c.get("held"))
    return ("v2", payload.get("run"), payload.get("status"), tuple(tasks), tuple(choices))


def v2_reminder_block(payload: dict, interval: float,
                      stalled_for: Optional[float] = None) -> str:
    """What a stalled v2 run's driver hears (claunch-ccoof).

    The first one at a position names each task with its prompt, cut at
    :data:`_INSTRUCTIONS_LIMIT` like :func:`reminder_block`; a repeat
    (``stalled_for`` given) names the position and how long it has not
    moved, and points at ``flow_status`` for the rest.
    """
    tasks = payload.get("tasks") or []
    choices = [c for c in payload.get("choices") or []
               if c.get("chooser") == cflow2_ir.CHOOSER_AGENT and not c.get("held")]
    at = [str(t.get("label") or t.get("instance")) for t in tasks]
    at += [str(c.get("label") or c.get("instance")) for c in choices]
    head = ("# claunch cflow2: reminder -- machine-generated, not typed by the user "
            f"(every {interval:.0f}s while the run does not move)")
    lines = ["---", head,
             f"run: {payload.get('scope')} ({payload.get('workflow')}, {payload.get('run')})",
             f"waiting on you: {', '.join(at)}"]
    if stalled_for is not None:
        lines.append(f"unmoved for ~{max(1, int(stalled_for // 60))} min; this is "
                     "the short form, the position was restated here once")
    else:
        for t in tasks:
            prompt = str(t.get("prompt") or "").strip()
            if len(prompt) > _INSTRUCTIONS_LIMIT:
                prompt = prompt[:_INSTRUCTIONS_LIMIT] + " ... (cut; flow_status has it in full)"
            lines.append(f"task {t.get('label') or t.get('instance')}: {prompt}")
            if t.get("done_when"):
                lines.append(f"  done when: {t.get('done_when')}")
        for c in choices:
            lines.append(f"choice {c.get('label') or c.get('instance')}: "
                         f"{str(c.get('prompt') or '').strip()}")
    lines.append(
        "protocol: the run is waiting on you and has not moved. If this is the "
        "work you are on, keep going. If you have lost the thread, call "
        "flow_status, then finish the task with flow_report, or the choice "
        "with flow_select.")
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


#: Where a claimed trigger carries the sub run that queued it.
RUN_KEY = "_run"


def for_run(block: str, run: Optional[str]) -> str:
    """Name the sub run a notice is about, right under its header line.

    A notice about a sub run lands in the same terminal as the main run's,
    and without the name its reader would call the cflow tools on the main
    run. The main run's notices are returned unchanged.
    """
    if not run:
        return block
    lines = block.split("\n")
    at = next((i for i, line in enumerate(lines) if line.startswith("# ")), 0)
    lines.insert(at + 1, f"run: {run} (a sub run -- pass run: {run} to the cflow tools)")
    return "\n".join(lines)


def deadlock_block(stuck: dict) -> str:
    """The text a session hears when its main run and a sub run wait on
    each other's milestones and neither can move."""
    return "\n".join([
        "---",
        "# claunch cflow: sync deadlock -- machine-generated, not typed by the user",
        f"main run: step '{stuck['main_step']}' waits for {stuck['main_waits_for']}",
        f"sub run {stuck['sub']}: step '{stuck['sub_step']}' waits for {stuck['sub_waits_for']}",
        "protocol: neither side has anything new to consume, so neither will "
        "publish what the other waits for. Nothing was moved for you. Read "
        "both positions (cflow 'status', and 'status' with run: "
        f"{stuck['sub']}), decide which side should act first, and move it; "
        "if the workflows themselves are wrong, say so to whoever owns them.",
        "---",
    ])


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
        *([f"run: {payload['sub']} (a sub run -- pass run: {payload['sub']} to the cflow tools)"]
          if payload.get("sub") else []),
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
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            return
        try:
            with delivery_wake("cflow"):
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
        """Whether the tick is alive — the shared daemon-clock contract."""
        return self._task is not None and not self._task.done()

    def timers(self, now: Optional[float] = None) -> Dict[Tuple[str, str], dict]:
        """What this clock is holding for each run, in plain seconds.

        Same contract as the session reminder's cflow timer view, plus ``working``:
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
        for cwd, scope, run in cflow_state.known_slots():
            try:
                moved = cflow_engine.release_window(cwd=cwd, scope=scope, run=run)
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
                released.append((cwd, scope, for_run(window_block(moved), run)))
        return released

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            # A CLI-driven run, or a driver that exited: the run has moved
            # regardless, and whoever picks it up reads the new position.
            return
        try:
            with delivery_wake("cflow"):
                delivered = session.queue_delivery(block)
        except Exception:
            log.exception("cflow window notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow window notice queued for %r (%s)", scope, cwd)


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
        for cwd, scope, run in cflow_state.known_slots():
            try:
                payload = cflow_engine.status(cwd, scope=scope, run=run)
            except Exception as exc:
                # Includes the slot being locked by the agent mid-transition:
                # the fire stays armed, so the next tick is soon enough.
                log.debug("cflow timer skipped %s/%s: %s", cwd, scope, exc)
                continue
            if payload.get("status") != "waiting_timer":
                continue
            moved = cflow_engine.fire_timer(cwd=cwd, scope=scope, run=run)
            if not moved:
                continue
            log.info(
                "cflow timer fired %s/%s: %s %s/%s -> %s",
                cwd, scope, moved.get("step"), moved.get("fires"),
                moved.get("max"), moved.get("moved_to"),
            )
            fired.append((cwd, scope, for_run(timer_block(moved), run)))
        return fired

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        """The wake-up after a fire — the run has ALREADY moved."""
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            # A CLI-driven run, or a driver that exited: the run has moved
            # regardless, and whoever picks it up reads the new position.
            return
        try:
            with delivery_wake("cflow"):
                delivered = session.queue_delivery(block)
        except Exception:
            log.exception("cflow timer notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow timer notice queued for %r (%s)", scope, cwd)


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


#: How often the checklist clock LOOKS. Each run is measured no more often
#: than its own ``checklist.poll``, so this only bounds how late a due
#: measurement can be — a third of the shortest poll the schema allows.
CHECKLIST_TICK = 5.0


class ChecklistClock:
    """Measures checklist gates, and moves the run when every item is true.

    A ``checklist:`` step reports ``waiting_checklist``: the run sits there
    while conditions somebody else controls become true — the parent merging
    a branch, the live server picking up a deploy. The agent that would
    notice is the one that ended its turn to wait, so the daemon carries the
    measurement, the same shape as :class:`TimerClock`.

    Each pass runs every item's command (:func:`cflow.engine.check_checklist`)
    and writes the result into the run, which is what makes the gate legible
    from outside: ``claunch cflow status`` and the dashboard render the same
    list. When every item exits 0 AND the step's report has been filed, the
    run MOVES to ``checklist.then`` and the driver is woken with a frame
    naming what passed.

    Nothing here decides anything: the items, the destination and the poll
    are the workflow's, and the engine owns the two conditions. Spacing is
    the only state kept in memory, so a restart merely re-measures early —
    and without a daemon a checklist holds, which is the safe direction (the
    open door is a person's ``claunch cflow checklist --recheck``).
    """

    def __init__(self, manager, *, poll: float = CHECKLIST_TICK) -> None:
        self.manager = manager
        self.poll = poll
        #: (cwd, scope) -> wall clock at that run's last measurement.
        self._checked: Dict[Tuple[str, str], float] = {}
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
                for cwd, scope, frame in await asyncio.to_thread(self.scan):
                    await self._deliver(cwd, scope, frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow checklist clock tick failed")

    def scan(self, now: Optional[float] = None) -> List[Tuple[str, str, str]]:
        """Measure every due checklist and report the runs that MOVED.

        Blocking — it runs each item's command and writes run state — so call
        it in a thread. Public for the tests, which own ``now`` there (and
        through it the poll spacing). Only a move is returned: a measurement
        that changed nothing, or changed an item without opening the gate, is
        written into the run for the dashboard to show and is not spoken.
        """
        now = time.time() if now is None else now
        moved_runs: List[Tuple[str, str, str]] = []
        live = set()
        for cwd, scope, run in cflow_state.known_slots():
            key = (cwd, scope, run)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope, run=run)
            except Exception as exc:
                # Includes the slot being locked by the agent mid-transition:
                # the gate is unchanged, so the next tick is soon enough.
                log.debug("cflow checklist skipped %s/%s: %s", cwd, scope, exc)
                continue
            if payload.get("status") != "waiting_checklist":
                self._checked.pop(key, None)
                continue
            poll = float(
                (payload.get("checklist") or {}).get("poll")
                or cflow_model.DEFAULT_CHECKLIST_POLL
            )
            last = self._checked.get(key)
            if last is not None and now - last < poll:
                continue
            self._checked[key] = now
            try:
                result = cflow_engine.check_checklist(cwd=cwd, scope=scope, run=run)
            except Exception as exc:
                log.debug("cflow checklist measure failed %s/%s: %s", cwd, scope, exc)
                continue
            if not result:
                continue
            if not result.get("moved_to"):
                log.info(
                    "cflow checklist %s/%s: %s %s/%s (changed: %s)",
                    cwd, scope, result.get("step"), result.get("passed"),
                    result.get("total"), ", ".join(result.get("changed") or []),
                )
                continue
            log.info(
                "cflow checklist %s %s/%s: %s -> %s",
                "expired" if result.get("expired") else "passed",
                cwd, scope, result.get("step"), result.get("moved_to"),
            )
            moved_runs.append((cwd, scope, for_run(checklist_block(result), run)))
        for stale in set(self._checked) - live:
            self._checked.pop(stale, None)
        return moved_runs

    async def _deliver(self, cwd: str, scope: str, frame: str) -> None:
        """The wake-up after a gate opened — the run has ALREADY moved."""
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            # A CLI-driven run, or a driver that exited: the run has moved
            # regardless, and whoever picks it up reads the new position.
            return
        try:
            with delivery_wake("cflow"):
                delivered = session.queue_delivery(frame)
        except Exception:
            log.exception("cflow checklist notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow checklist notice queued for %r (%s)", scope, cwd)


def checklist_block(result: dict) -> str:
    """The text the driver hears when a checklist gate opened the run's way.

    Framed like the timer block, for the same reason: it lands in a session
    that ended its turn. It names every item and the code it answered with,
    because that evidence is what the agent would otherwise go and collect by
    hand — and the run has ALREADY moved, so there is nothing to confirm.
    """
    position = (
        "the run finished"
        if result.get("moved_to") == "end"
        else "step " + repr(result.get("moved_to"))
    )
    expired = bool(result.get("expired"))
    lines = [
        "---",
        "# claunch cflow: checklist {0} -- machine-generated, not typed by "
        "the user".format("expired" if expired else "passed"),
        "workflow: " + str(result.get("workflow")),
        "gate: {0!r} ({1}/{2} items true)".format(
            result.get("step"), result.get("passed"), result.get("total")
        ),
    ]
    marks = {True: "[x]", False: "[ ]", None: "[?]"}
    for entry in result.get("items") or []:
        lines.append(
            "  {0} {1}: {2} (exit {3}, measured {4})".format(
                marks.get(entry.get("ok"), "[?]") if expired else "[x]",
                entry.get("id"),
                entry.get("describe"),
                entry.get("exit_code"),
                entry.get("measured_at"),
            )
        )
    if expired:
        protocol = (
            "protocol: the gate did not open within {0:.0f}s of being "
            "presented, so the workflow's 'otherwise' edge moved the run on "
            "its own -- this is the gate's 'no', not an override, and no "
            "report was filed for the step you left. The item states above "
            "are journalled as 'checklist_expired'. Call the cflow 'status' "
            "tool for the step you are now on: it is where the retry is "
            "decided, and re-entering the gate is a fresh visit (its "
            "'restart:' runs again).".format(float(result.get("after") or 0))
        )
    else:
        protocol = (
            "protocol: every item measured true and your report was on file, "
            "so the gate opened and the run moved on its own -- nothing was "
            "decided for you and there is nothing to confirm. The per-item "
            "evidence above is journalled as 'checklist_passed'. Call the "
            "cflow 'status' tool for the step you are now on and continue per "
            "the /cflow protocol."
        )
    lines.extend(["position: " + position, protocol, "---"])
    return "\n".join(lines)


#: How often a restart command past its ``restart.timeout`` is looked at
#: again while the approval gate still holds its session's request.
RESTART_HOLD_POLL = 15.0


def _gate_holding(session: str) -> bool:
    """Whether the daemon's restart gate holds ``session``'s request. Any
    failure to tell reads as False: the step's timeout then applies as it
    always did."""
    try:
        from . import restart_gate

        return restart_gate.holding(session)
    except Exception:  # noqa: BLE001 - a guess must not hold a shell forever
        return False


class RestartClock:
    """Run a checklist step's project-local restart command once per visit.

    Commands run from the driving session's CWD and are selected by the
    workflow's platform mapping.  This clock never treats the cflow daemon as
    the target service; a project script owns that decision.
    """

    def __init__(self, manager, *, boot_id: str, poll: float = 5.0) -> None:
        self.manager = manager
        self.boot_id = boot_id
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
                actions = await asyncio.to_thread(self.scan)
                for cwd, scope, action in actions:
                    await self._deliver(cwd, scope, restart_started_block(action))
                    if action["kind"] != "run":
                        continue
                    result = await asyncio.to_thread(self._execute, cwd, action, scope)
                    recorded = await asyncio.to_thread(
                        cflow_engine.complete_restart,
                        cwd=cwd, scope=scope, step_id=action["step"],
                        visit=action["visit"], exit_code=result["exit_code"],
                        output=result["output"],
                    )
                    if recorded:
                        await self._deliver(cwd, scope, restart_finished_block(recorded))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cflow restart clock tick failed")

    def scan(self) -> List[Tuple[str, str, dict]]:
        actions: List[Tuple[str, str, dict]] = []
        system = platform_mod.system()
        for cwd, scope in cflow_state.known_runs():
            try:
                action = cflow_engine.claim_restart(
                    cwd=cwd, scope=scope, platform=system, boot_id=self.boot_id
                )
            except Exception as exc:
                log.debug("cflow restart scan skipped %s/%s: %s", cwd, scope, exc)
                continue
            if action:
                actions.append((cwd, scope, action))
        return actions

    @staticmethod
    def _execute(cwd: str, action: dict, scope: str = "", *,
                 holding=_gate_holding, hold_poll: float = RESTART_HOLD_POLL,
                 instance: Optional[str] = None) -> dict:
        # Output goes to a file, not a pipe. The restart command is a shell
        # whose descendants (this repository's tools/restart_live.ps1 runs
        # `claunch daemon restart`) inherit stdout; with a pipe, the reader
        # thread subprocess.run() spawns on Windows cannot finish until every
        # inheritor has exited — and a timeout only kills the shell, then
        # calls communicate() again with no timeout at all. That thread runs
        # in the daemon's default executor and blocked its shutdown for
        # minutes (2026-09-11). A file has no reader thread: wait() returns
        # the moment the shell exits, timeout or not.
        #
        # The command carries the run's scope in CLAUNCH_SESSION, because the
        # restart it performs belongs to that session and not to the person
        # at the keyboard. A `claunch daemon restart` inside the script reads
        # the same variable to tell the two apart: set, and the CLI files the
        # request with the web UI's approval gate and waits for the answer
        # (`cli_sessions._gated_restart`); unset, and the restart goes out on
        # the spot, which is the operator's own door. A child of the daemon
        # inherits the daemon's environment, where the variable is unset, so
        # every restart this clock ran went out with nobody asked -- the
        # precondition `claunch-028r` wrote down as C-3 and the implementation
        # did not meet. Scopes that are not a managed session (`default`)
        # answer to no name, so the variable is removed rather than set to one.
        #
        # The step's timeout does not cut short a shell that is waiting on the
        # approval gate (claunch-mm8zv). The person at the web UI may extend
        # the gate's deadline any number of times; killing the shell at the
        # timeout would journal "timed out" while the gate, still pending,
        # restarts the daemon later -- the journal and the machine disagreeing.
        # So when the timeout comes, the clock asks the gate whether it still
        # holds this session's request, and keeps waiting while it does.
        #
        # What the command starts and leaves running (a dev server) is the
        # session's to see and to stop (claunch-ihwel): the shell is adopted by
        # :mod:`.session_procs`, the owner tag rides in its environment, and
        # the command's process tree is re-read every TRACK_POLL seconds while
        # it runs so a grandchild is recorded before its parent exits. Only
        # the tree is read then (``refresh_tree``, claunch-b6mnl); the whole
        # table -- where a detached process is found by its owner tag -- is
        # read at adoption, when the command exits, and by the tracker's own
        # RefreshClock.
        #
        # The daemon's own instance identity is not inherited
        # (claunch-7iub9): the command gets the instance of the session it
        # runs for -- ``instance``, by default this daemon's, which is the
        # driver's own once the v2 clock only ticks the runs of its sessions
        # (``Cflow2Clock.owns``) -- and none of the daemon's manifest
        # bookkeeping or port (``instance_manifest.engine_env``).
        if instance is None:
            instance = daemon_paths.instance()
        env = instance_manifest.engine_env(dict(os.environ), instance)
        session = ""
        if scope and scope != cflow_state.DEFAULT_SCOPE:
            env[cflow_state.SESSION_ENV] = scope
            session = scope
        else:
            env.pop(cflow_state.SESSION_ENV, None)
        env = session_procs.owner_env(env, session)
        procs = _tracker() if session else None
        try:
            with tempfile.TemporaryFile(mode="w+b") as out:
                proc = subprocess.Popen(
                    action["command"], cwd=cwd, shell=True, env=env,
                    stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                )
                if procs is not None:
                    _quietly(procs.adopt, session, proc.pid,
                             label="restart:" + str(action.get("step") or action.get("label")
                                                    or action.get("node") or ""),
                             command=str(action["command"]))
                deadline = time.monotonic() + float(action["timeout"])
                held = False
                while True:
                    left = deadline - time.monotonic()
                    try:
                        if procs is not None and left > 0:
                            code = proc.wait(timeout=min(left, TRACK_POLL))
                        else:
                            code = proc.wait(timeout=max(left, 0))
                        break
                    except subprocess.TimeoutExpired:
                        if procs is not None:
                            _quietly(procs.refresh_tree, session)
                        if time.monotonic() < deadline:
                            continue
                        if session and holding(session):
                            held = True
                            deadline = time.monotonic() + hold_poll
                            continue
                        proc.kill()
                        proc.wait()
                        output = _read_back(out) + "\nrestart command timed out"
                        return {"exit_code": None, "output": output}
                if procs is not None:
                    _quietly(procs.refresh)
                output = _read_back(out)
                if held:
                    output += ("\nrestart command outlived its timeout while the "
                               "approval gate held the request")
                return {"exit_code": code, "output": output}
        except OSError as exc:
            return {"exit_code": None, "output": f"could not run restart command: {exc}"}

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            return
        try:
            with delivery_wake("cflow"):
                session.queue_delivery(block)
        except Exception:
            log.exception("cflow restart notice delivery to %r failed", scope)


#: How often a running restart command's process tree is re-read.
TRACK_POLL = 1.0


def _quietly(fn, *args, **kwargs) -> None:
    """Process tracking is a view: a failure there must not fail a restart."""
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("session process tracking failed")


def _tracker():
    try:
        return session_procs.tracker()
    except Exception:
        log.exception("session process tracker unavailable")
        return None


def _read_back(fh) -> str:
    """Everything a restart command wrote so far, decoded leniently."""
    try:
        fh.flush()
        fh.seek(0)
        return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def restart_started_block(action: dict) -> str:
    if action["kind"] == "run":
        detail = ("the daemon is executing the step's project-local restart command from "
                  "this session's CWD on the workflow's behalf -- do not run it yourself: "
                  "an agent session has no authority to restart or stop the daemon")
    elif action["kind"] == "interrupted":
        detail = "the prior boot stopped while the restart command was running; it will not be run twice"
    else:
        detail = f"no restart command is declared for platform {action.get('platform')!r}"
    return "\n".join(["---", "# claunch cflow: restart -- machine-generated, not typed by the user",
                      f"workflow: {action.get('workflow')}", f"step: {action.get('step')!r}",
                      f"event: {detail}",
                      "protocol: read cflow status after this message; the checklist remains the deployment gate.", "---"])


def restart_finished_block(result: dict) -> str:
    return "\n".join(["---", "# claunch cflow: restart result -- machine-generated, not typed by the user",
                      f"workflow: {result.get('workflow')}", f"step: {result.get('step')!r}",
                      f"exit code: {result.get('exit_code')}",
                      "protocol: the restart result is journaled; the checklist decides whether deployment can advance.", "---"])


class TriggerClock:
    """Perform the daemon side effects a workflow declared with ``triggers:``.

    Two capabilities live in this daemon that a run has no tool to call for
    itself, and both were reachable only by a person clicking in the web UI:
    re-taking a session's Y/N status checks, and recomposing its LLM
    briefing. A workflow that wanted either asked for it in prose — the
    driving agent read "call `status_checks`, then `report_status_checks`"
    in a step's instructions and did it, or did not. This clock is the
    declarative half: the step names the capability and the moment
    (``triggers: [{do: checks, at: leave}]``), the engine records the moment
    as it performs the move, and this drains what it recorded.

    **Nothing here can hold a run.** Unlike :class:`RestartClock`, whose
    outcome a checklist is required to gate on, a trigger is a side effect
    with no reader: the run has already moved by the time the entry is
    claimed. So every "could not" is an ordinary outcome — no enabled status
    check, no ``llm:`` block, a driving session that exited, an endpoint that
    refused — journalled as ``trigger_skipped`` with its reason, and the run
    carries on. That is the whole failure policy, and it is why a trigger is
    allowed on any step including a ``select``.

    Claims are single-use (:func:`cflow.engine.claim_triggers` removes what
    it hands back), so a daemon that dies mid-action does not repeat it on
    the next boot: a second refresh request typed into a terminal, or a
    second paid LLM call, for a position the run left is worse than the
    silence. The journal keeps the evidence either way — a
    ``trigger_queued`` with no ``trigger_done`` or ``trigger_skipped`` beside
    it names exactly the one that did not happen.
    """

    #: How often a run's landing queue is measured against git between
    #: moves (the move to ``end`` measures it regardless).
    LANDING_REFRESH = 60.0

    def __init__(self, manager, mesh=None, *, poll: float = 5.0) -> None:
        self.manager = manager
        self.mesh = mesh
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        self._landing_checked: Dict[Tuple[str, str, Optional[str]], float] = {}
        self._mergers_checked = float("-inf")

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
                # Sequential on purpose. A `briefing` action waits on the
                # user's own endpoint (up to `briefing.LLM_TIMEOUT`), so a
                # slow one delays the rest of this pass — which is the right
                # trade at this volume (a handful per round) and keeps one
                # misconfigured endpoint from opening N concurrent calls.
                await asyncio.to_thread(self.refresh_landing)
                await asyncio.to_thread(self.grant_mergers)
                for cwd, scope, action in await asyncio.to_thread(self.scan):
                    performed, detail = await self._perform(cwd, scope, action)
                    await asyncio.to_thread(
                        cflow_engine.complete_trigger,
                        cwd=cwd, scope=scope, run=action.get(RUN_KEY), do=action["do"],
                        step_id=action.get("step"), visit=action.get("visit"),
                        performed=performed, detail=detail,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cflow trigger clock tick failed")

    def scan(self) -> List[Tuple[str, str, dict]]:
        """One pass over the registry: every trigger queued since the last.
        Blocking (every run's state); call it in a thread. Public for the
        tests, like the other clocks' scans."""
        out: List[Tuple[str, str, dict]] = []
        for cwd, scope, run in cflow_state.known_slots():
            try:
                claimed = cflow_engine.claim_triggers(cwd=cwd, scope=scope, run=run)
            except Exception as exc:
                log.debug("cflow trigger scan skipped %s/%s: %s", cwd, scope, exc)
                continue
            for action in claimed or []:
                # The run rides on the action so its completion is written
                # back to the slot that queued it.
                out.append((cwd, scope, {**action, RUN_KEY: run} if run else action))
        return out

    def refresh_landing(self, now: Optional[float] = None) -> List[Tuple[str, str, List[str]]]:
        """Mark landed tips on every running landing queue, at most once a minute each.

        Blocking (git, per entry); call it in a thread. Returns the runs whose
        queue moved, with the issues marked. Public for the tests.
        """
        now = time.monotonic() if now is None else now
        moved: List[Tuple[str, str, List[str]]] = []
        for cwd, scope, run in cflow_state.known_slots():
            key = (cwd, scope, run)
            if now - self._landing_checked.get(key, float("-inf")) < self.LANDING_REFRESH:
                continue
            self._landing_checked[key] = now
            try:
                token = cflow_state.push_scope(scope)
                run_token = cflow_state.push_run(run)
                try:
                    state = cflow_state.load_state(cwd)
                finally:
                    cflow_state.pop_run(run_token)
                    cflow_state.pop_scope(token)
            except Exception:
                continue
            if not state.get("landing_queue") or state.get("status") in ("done", "aborted"):
                continue
            try:
                result = cflow_engine.refresh_landing(cwd=cwd, scope=scope, run=run)
            except Exception as exc:
                log.debug("landing refresh skipped %s/%s: %s", cwd, scope, exc)
                continue
            if result.get("landed"):
                moved.append((cwd, scope, list(result["landed"])))
        return moved

    def grant_mergers(self, now: Optional[float] = None) -> List[Tuple[str, str, str]]:
        """Give the ``merger`` subrole to each live session that drives a run
        holding a landing queue, at most once a minute (claunch-nie6o).

        A worker's landing request goes only to a session holding ``merger``
        (:meth:`.landing_route.Router.merger_role`); a session that took on a
        landing queue (a v1 ``landing_queue:`` run, the ``stack`` sub run
        opened when its first child appears, a v2 ``landing`` collection) is
        given it here rather than waiting for a person. The grant goes
        through :meth:`.mesh.MeshManager.grant_auto_subrole`: the member is
        typed why and how to take it back, the roster marks it as the
        daemon's, and a person's removal sticks. Blocking (every run's
        state); call it in a thread. Returns ``(session, mesh, handle)`` per
        grant. Public for the tests.
        """
        now = time.monotonic() if now is None else now
        if self.mesh is None or now - self._mergers_checked < self.LANDING_REFRESH:
            return []
        self._mergers_checked = now
        from . import cflow2_triggers as v2

        try:
            sessions = [s for s in self.manager.list() if not getattr(s, "exited", True)]
        except Exception:
            return []
        holders = None
        granted: List[Tuple[str, str, str]] = []
        for session in sessions:
            name = getattr(getattr(session, "sdef", None), "name", "") or ""
            try:
                wanting = [
                    m for m in self.mesh.meshes_for_session(name)
                    if landing_route.MERGER not in (m.get("roles") or [])
                    # A leader counts as a merger already (claunch-lc2n3).
                    and m.get("role") != landing_route.LEADER
                ]
            except Exception:
                continue
            if not name or not wanting:
                continue
            if holders is None:
                holders = v2.landing_holders(self.manager, v2.session_cwds(self.manager))
            try:
                holder = landing_route.pick(holders(name, None))
            except Exception as exc:  # noqa: BLE001 - an unreadable slot is no holder
                log.debug("merger grant skipped %s: %s", name, exc)
                continue
            if holder is None:
                continue
            run = holder.run or "main run"
            reason = (
                f"it drives a run that holds a landing queue ({holder.version} "
                f"{run} at {holder.cwd}), so workers' landing requests can be "
                "routed to it"
            )
            for mesh_name, handle in self.mesh.grant_auto_subrole(
                name, landing_route.MERGER, reason
            ):
                log.info("mesh %r: gave %r (%s) merger: %s", mesh_name, handle, name, reason)
                granted.append((name, mesh_name, handle))
        return granted

    def v1_holders(self, name: str, repo: Optional[str]) -> List[landing_route.Holder]:
        """The v1 runs ``name`` drives in checkouts of ``repo`` that declare
        ``landing_queue:`` -- one per directory, its main run asked first
        (:func:`cflow.engine.landing_queue_run`), directories in order."""
        out: List[landing_route.Holder] = []
        for c in sorted({c for c, s in cflow_state.known_runs() if s == name}):
            if repo is not None and cflow_landing.common_dir(c) != repo:
                continue
            try:
                run = cflow_engine.landing_queue_run(cwd=c, scope=name)
            except Exception:
                continue
            if run is not None:
                main = run == cflow_state.MAIN_RUN
                out.append(landing_route.Holder(c, None if main else run, main))
        return out

    def landing_target(self, cwd: str, scope: str) -> landing_route.Target:
        """Whose landing queue this worker's request goes on
        (:meth:`.landing_route.Router.stacked`). Blocking; call it in a thread."""
        return landing_route.Router(self.manager, self.mesh, self.v1_holders).stacked(cwd, scope)

    async def _landing_facts(self, cwd: str, scope: str) -> Tuple[dict, Optional[str]]:
        """The worker's landing request as measured now, and why it must not
        be filed (None when it may).

        Measured in the checkout the session works in, the way the v2 trigger
        does (:func:`.cflow2_triggers.session_facts`): a run started at the
        main checkout whose session then moved into a worktree would
        otherwise file the integration branch and its tip, and the queue
        marks that landed at once (claunch-4zc51.1.2, claunch-4zc51.1.3).
        A request measured on a trunk, on a detached HEAD or at a tip the
        trunk already holds is refused (:func:`.cflow2_triggers.landing_refusal`).
        """
        from . import cflow2_triggers as v2

        facts = await asyncio.to_thread(v2.session_facts(self.manager), cwd, scope)
        if not facts.get("head"):
            return facts, f"no commit readable in {facts.get('checkout') or cwd}"
        if facts.get("in_review") is None:
            return facts, "the issue board could not be read"
        if not facts["in_review"]:
            return facts, f"{scope} holds no issue in_review (no landing request on the board)"
        refused = await asyncio.to_thread(v2.landing_refusal, facts)
        if refused:
            log.warning("cflow enqueue-landing from %s refused: %s", scope, refused)
        return facts, refused

    async def _enqueue_landing(self, cwd: str, scope: str) -> Tuple[bool, str]:
        """This worker's landing request, onto the landing queue of the
        nearest direct ancestor that can merge, else of the mesh leader
        beside it or one of its ancestors (:meth:`landing_target`). With neither it is an error: the
        reason is journalled and logged, and nothing is filed.

        Read, not reported: the tip and branch from the checkout the worker
        works in (the frozen tip its request names, :meth:`_landing_facts`), the issues from the
        board (what it moved to in_review). Runs whether or not the worker is
        still alive -- the request stands either way.
        """
        # A parent whose landing queue is a v2 ``landing`` collection
        # (docs/cflow-v2.md 10, 「v1 연결」): taken only when no v1 run of
        # the parent holds one, so every v1-only case below reads as before.
        bridged = await self._enqueue_landing_v2(cwd, scope)
        if bridged is not None:
            return bridged
        target = await asyncio.to_thread(self.landing_target, cwd, scope)
        if target.holder is None:
            log.warning("cflow enqueue-landing from %s: no merge target: %s", scope, target.why)
            return False, target.why
        # Who was passed over, so the journal says why the request is not on
        # the parent's queue.
        via = target.note()
        pcwd, parent = target.holder.cwd, target.owner
        facts, refused = await self._landing_facts(cwd, scope)
        if refused:
            return False, refused
        tip, branch, issues = facts["head"], facts["branch"], facts["in_review"]
        # The queue may live on one of the target's SUB runs (a stack that
        # manages the children); the holder names it.
        try:
            result = await asyncio.to_thread(
                cflow_engine.enqueue_landing, issues, branch, tip,
                by=scope, cwd=pcwd, scope=parent, run=target.holder.run,
            )
        except cflow_engine.CflowError as exc:
            return False, f"{via}{parent!r}: {exc}"
        added, renewed = result.get("added") or [], result.get("renewed") or []
        if not added and not renewed:
            return True, f"{via}already on {parent}'s landing queue at {tip[:8]}"
        return True, (
            f"{via}on {parent}'s landing queue at {tip[:8]}: "
            + ", ".join(
                [f"added {', '.join(added)}"] * bool(added)
                + [f"renewed {', '.join(renewed)}"] * bool(renewed)
            )
        )

    async def _enqueue_landing_v2(self, cwd: str, scope: str) -> Optional[Tuple[bool, str]]:
        """The landing request onto a v2 parent run's ``landing`` collection
        (principal ``child``), or None when the parent has no such run.

        Only a live parent's runs in a checkout of the worker's repository
        count, as in :class:`.landing_route.Router`; with none (or a parent
        that exited) this returns None and the v1 route decides. A parent that
        also holds a v1 ``landing_queue:`` run leaves this to the v1 path
        (None), and of several v2 runs a main run is taken first, then the
        first in order (:func:`.landing_route.pick`, docs 10).
        """
        from ..cflow2 import store as cstore
        from . import cflow2_triggers as v2

        parent = v2.parent_of(self.manager, scope)
        if not parent or v2.live_session(self.manager, parent) is None:
            return None
        repo = await asyncio.to_thread(cflow_landing.common_dir, cwd)
        holders = await asyncio.to_thread(
            lambda: [
                h for h in v2.v2_holders(v2.LANDING, parent, v2.session_cwds(self.manager, parent))
                if repo is None or cflow_landing.common_dir(h[0]) == repo
            ]
        )
        holders = sorted(set(holders))
        if not holders:
            return None
        if await asyncio.to_thread(v2.v1_landing_holders, parent):
            return None
        chosen = landing_route.pick([
            landing_route.Holder(c, sc, sc == cstore.main_scope(sc), "v2") for c, sc in holders
        ])
        facts, refused = await self._landing_facts(cwd, scope)
        if refused:
            return False, refused
        tip, branch, issues = facts["head"], facts["branch"], facts["in_review"]
        items = [
            {"issue": i, "branch": branch or "", "tip": tip, "requested_by": scope}
            for i in issues
        ]
        return await asyncio.to_thread(
            v2.push_v2, (chosen.cwd, str(chosen.run)), v2.LANDING, items, scope
        )

    async def _perform(self, cwd: str, scope: str, action: dict) -> Tuple[bool, str]:
        """Do one claimed action. Returns (performed, why/what)."""
        do = action.get("do")
        if do == cflow_model.TRIGGER_ENQUEUE_LANDING:
            if action.get(RUN_KEY):
                # A landing request is the SESSION's (its branch, its
                # in_review issues), and the session's main run files it.
                return False, "a sub run does not file the session's landing request"
            return await self._enqueue_landing(cwd, scope)
        if do == cflow_model.TRIGGER_CHECKS:
            # A held driver is asleep, not gone: the request wakes it and is
            # typed after the respawn (claunch-t1r9j).
            held = session_for(self.manager, cwd, scope, held=True)
            if held is not None and is_held(held):
                return await request_checks(held, waking_deliver(
                    self.manager, held.sdef.name, by="cflow-checks"))
        session = session_for(self.manager, cwd, scope)
        if session is None:
            return False, "no live session drives this run"
        if do == cflow_model.TRIGGER_CHECKS:
            return await self._checks(cwd, scope, session)
        if do == cflow_model.TRIGGER_BRIEFING:
            return await self._briefing(session)
        # A workflow snapshot taken by a newer version than this daemon.
        return False, f"this daemon has no trigger action named {do!r}"

    async def _checks(self, cwd: str, scope: str, session) -> Tuple[bool, str]:
        return await request_checks(
            session, lambda block: self._deliver(cwd, scope, block)
        )

    async def _deliver(self, cwd: str, scope: str, block: str) -> bool:
        """Queue text for the driving session, the way every clock here does.

        ``queue_delivery`` and not ``deliver``: the latter waits out a
        half-typed draft in the terminal, for as long as it stands, and a
        clock that awaits one stops ticking for every other run on the
        machine. Queueing defers behind the draft instead and returns now.
        """
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            return False
        try:
            with delivery_wake("cflow"):
                return bool(session.queue_delivery(block))
        except Exception:
            log.exception("cflow trigger delivery to %r failed", scope)
            return False

    @staticmethod
    async def _briefing(session) -> Tuple[bool, str]:
        return await recompose_briefing(session)


def is_held(session) -> bool:
    """Whether ``session`` is a held record: its program freed, the session
    kept, and brought back by :meth:`SessionManager.wake_async`."""
    return bool(getattr(session, "exited", False) and getattr(session, "held_at", None))


def waking_deliver(manager, name: str, *, by: str) -> Callable[[str], Awaitable[bool]]:
    """A ``deliver`` for a held session: wake it, then queue the block into
    the woken session (:meth:`SessionManager.wake_soon`). Returns whether the
    wake was scheduled -- the block rides behind the respawn, so acceptance
    is not a delivery receipt, the same as ``queue_delivery``'s."""
    async def deliver(block: str) -> bool:
        try:
            return bool(manager.wake_soon(name, by=by, deliver=block))
        except Exception:
            log.exception("wake of held session %r for delivery failed", name)
            return False
    return deliver


async def request_checks(
    session, deliver: Callable[[str], Awaitable[bool]]
) -> Tuple[bool, str]:
    """The ``checks`` trigger: ask ``session`` to re-take its enabled status
    checks, typed through ``deliver``. Shared by the v1 :class:`TriggerClock`
    and the v2 clock (:mod:`.cflow2_triggers`)."""
    try:
        checks = await asyncio.to_thread(
            status_checks.session_entries, session.sdef.name, enabled_only=True
        )
    except status_checks.StatusCheckError as exc:
        return False, f"status checks unreadable: {exc}"
    if not checks:
        return False, "no enabled status checks are configured"
    if not await deliver(status_checks.REFRESH_PROMPT):
        return False, "the refresh request could not be queued for the session"
    return True, f"asked for {len(checks)} status check(s)"


async def recompose_briefing(session) -> Tuple[bool, str]:
    """The ``briefing`` trigger: recompose ``session``'s LLM briefing. Shared
    by the v1 :class:`TriggerClock` and the v2 clock."""
    cfg = await asyncio.to_thread(briefing.llm_config)
    if not briefing.llm_configured(cfg):
        return False, "no llm endpoint is configured (the llm: block is empty)"
    try:
        payload = await briefing.compose(session, cfg, refresh=True)
    except briefing.BriefingError as exc:
        return False, f"briefing endpoint failed: {exc}"
    except Exception as exc:  # the endpoint is the user's own choice
        return False, f"briefing could not be composed: {exc}"
    state = ((payload.get("briefing") or {}).get("state")
             if isinstance(payload.get("briefing"), dict) else None)
    if state:
        return True, f"briefing recomposed (state {state!r})"
    return True, "briefing recomposed"


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
        session = session_for(self.manager, cwd, scope, held=True)
        if session is None:
            # A CLI-driven run, or a driver that exited: the new round is
            # running regardless, and whoever picks it up reads its position.
            return
        try:
            with delivery_wake("cflow"):
                delivered = session.queue_delivery(block)
        except Exception:
            log.exception("cflow round notice delivery to %r failed", scope)
            return
        if delivered:
            log.info("cflow round notice queued for %r (%s)", scope, cwd)


def landing_lines(payload: dict) -> List[str]:
    """The landing queue's state at a round's start, for the round block.

    Empty for a workflow without ``landing_queue:``. Otherwise it says what
    the last round's end dropped and carried (the reset the driver did not
    watch happen: the engine did it inside the move to ``end``), where the
    queue stands now, and any disagreement with the issue board.
    """
    if "landing_queue" not in payload:
        return []
    lines: List[str] = []
    reset = payload.get("landing_reset") or {}
    if reset:
        def ids(rows):
            return ", ".join(
                f"{r.get('issue')} ({r.get('status')}"
                + (", was deferred" if r.get("was") else "") + ")"
                for r in rows
            ) or "none"
        lines.append(
            f"landing queue reset at the last round's end (run {reset.get('run')}): "
            f"dropped {ids(reset.get('dropped') or [])}; "
            f"carried {ids(reset.get('carried') or [])}"
        )
        for warning in reset.get("warnings") or []:
            lines.append(f"landing queue warning: {warning}")
    queue = payload.get("landing_queue") or []
    lines.append(
        f"landing queue now: {len(queue)} entr{'y' if len(queue) == 1 else 'ies'}"
        + (
            " -- " + ", ".join(f"{e.get('issue')} {e.get('status')}" for e in queue)
            if queue else ""
        )
        + " (the 'landing_queue' tool lists and moves them)"
    )
    return lines


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
            *landing_lines(started),
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

#: Statuses in which a run is finished and can no longer be driven. A run that
#: has reached one of them is not orphaned by its session's exit -- there is
#: nothing left for anyone to drive.
_SETTLED = ("done", "aborted")


def driver_gone(manager, cwd: str, scope: str) -> bool:
    """True when the run's scope names a managed session that has exited.

    Same containment rule as :func:`session_for`: the scope IS the session
    name, and the cwd must match. A scope no manager knows is a standalone or
    CLI run -- not driven by a session, so never orphaned by one -- and a
    matching name in another directory is somebody else's session.
    """
    try:
        session = manager.get(session_name(scope))
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


def run_orphaned(manager, cwd: str, scope: str, payload: dict) -> bool:
    """The run at ``(cwd, scope)`` is unfinished and nobody is driving it.

    Read by ``/api/cflow``, which marks these runs so the dashboard's Orphans
    tab lists them. That tab is the whole view of the set: the run event
    clock no longer pushes it (it tells a parent only about the one exit
    nobody asked for, at the moment of the exit -- see
    :meth:`RunEventClock.session_exited`), because re-reporting the set on
    every daemon boot cost the overseer a turn per run per restart and
    changed nothing it did.
    """
    return bool(
        payload.get("run")
        and payload.get("status") not in _SETTLED
        and driver_gone(manager, cwd, scope)
    )


#: The ``signals`` record a workflow pushes when its run enters the settle
#: phase (improv-worker-v2's ``settle`` node, on enter), and the reason the
#: reviewer reap it triggers is filed under (claunch-5aysr).
SETTLE_SIGNAL = "settle"
SETTLE_REAP = "settle-reap"
#: The workflow node whose subtree is the settle phase -- the node that
#: pushes :data:`SETTLE_SIGNAL` (claunch-numgz).
SETTLE_SCOPE = "settle"


def _iso_ts(value) -> Optional[float]:
    """Seconds since the epoch from a run stamp: a collection record's
    ``at`` is already a number, the run's ``started`` an ISO string."""
    from datetime import datetime

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _in_settle_scope(instance) -> bool:
    """Whether an ``active`` instance id (``<node>@<n>``) is the settle node
    or one of the nodes below it."""
    node = str(instance).split("@", 1)[0]
    return (node == SETTLE_SCOPE
            or node.startswith(SETTLE_SCOPE + ".")
            or node.startswith(SETTLE_SCOPE + "/"))


def settle_signalled(state: dict) -> bool:
    """Whether a v2 run is in its settle phase: a ``settle`` record in its
    ``signals`` log written by *this* run (claunch-5aysr), while the run is
    still positioned inside the settle node (claunch-numgz).

    The log is declared ``lifecycle: {inherit: true}``, so a run started in
    the same slot (settle's ``new-task``) begins with the previous run's
    records -- including its ``settle``. A record counts only when it was
    written at or after this run started; an unreadable stamp does not count.

    The record is never removed, so a run a person moved out of settle back
    into its rounds still carries it; without the position check every poll
    ended the reviewer the next round spawned. A state with no ``active``
    list (an older run state) keeps the record-only answer.
    """
    active = state.get("active")
    if isinstance(active, list) and not any(
            _in_settle_scope(a) for a in active):
        return False
    started = _iso_ts(state.get("started"))
    if started is None:
        return False
    signals = ((state.get("collections") or {}).get("signals") or {})
    for rec in signals.get("items") or []:
        if not isinstance(rec, dict) or rec.get("name") != SETTLE_SIGNAL:
            continue
        at = _iso_ts(rec.get("at"))
        if at is not None and at >= started:
            return True
    return False


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
    * **orphaned** — the driving session exited while its run was
      unfinished, and nobody asked for that exit (no kill, no pause): a
      crash. Pushed once, at the exit itself, to the live spawn parent only
      (:meth:`session_exited`). Runs left behind by a deliberate kill are not
      pushed at all -- whoever killed the session knows -- and the standing
      set lives on the dashboard's Orphans tab (:func:`run_orphaned`).
    * **session-ended** — kill-on-end (below) reaped a finished one-shot
      run's session. Sent after the kill lands, and NOT gated on
      ``cflow_events``: the peers still messaging that session have no
      other way to learn the terminal is gone.

    The overseer is the driver's spawn-tree parent when it is alive — in a
    leader/worker fleet the parent *is* the leader, and the daemon's manager
    already knows it. A driver with no live parent falls back to the local
    ``leader``-role member of its mesh; with neither, the event is logged and
    dropped (the web dashboard remains the human's view).

    Transitions are detected by diffing each run's position between polls, so
    the first sight of a run only arms it — a daemon restart does not replay
    events, and one that happens while a run sits at a gate misses that
    entry (the overseer's pull channel, ``claunch cflow status -t``, is the
    safety net). ``orphaned`` comes from the exit hook instead of the scan,
    so a restart does not replay it either -- it used to be read off state on
    sight, and the same few dead runs were re-sent after every boot (one run
    244 times in ten days, s773 on claunch-4i1n2). Delivery is a debt like the reminder's: a failed
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
    carries ``keep_alive`` — the user said keep it, standing, until they say
    otherwise — is recorded but not killed. A session a person respawned by
    name after the run had already finished (:attr:`Session.resumed_by_human`)
    gets a one-time deference instead of a standing one: this kill is called
    off and the transcript corrected, but the next daemon restart judges a
    fresh incarnation from scratch — a restart's own unattended
    :meth:`SessionManager.restore_all` is not a person asking, so it does not
    set the flag. Before this, ``keep_alive`` was the only thing this method
    read to decide, and a session nobody had told to stay alive had no way to
    say "not yet" for even one boot's kill — it could only be resurrected
    after the fact, respawn after respawn, each one undone by the next
    restart's mop-up (the run stayed ``done`` throughout, so every restart
    that first-saw it queued another kill). A record that could not be
    written is reported loudly and the session is left alive: killing behind
    a record that did not land is the exact thing this mechanical end exists
    to prevent.

    Scan-budget note (six clocks already share one sequential pass over
    ``known_runs()``): this detection adds no pass of its own and no per-run
    cost; an exit costs one status read, on the next pass after it. The
    idle-wait runs once per
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
        #: Sessions that exited without being asked to, as (name, cwd,
        #: parent): filled by :meth:`session_exited` on the loop, drained by
        #: :meth:`scan` in its thread (a deque, so the two need no lock).
        self._exits: deque = deque()
        #: (cwd, scope, run) whose ending was already recorded. State that
        #: fires on sight, once per run — a daemon restart
        #: that first-sees a done run still mops it up, and one that was in
        #: the middle of an ending does not re-record it. The switch off is
        #: still tracked (the ledger below gets the mark) so toggling it back
        #: on replays nothing.
        self._end_done: Set[Tuple[str, str, str]] = set()
        #: One-shot done runs whose session is queued for the end-sequence.
        #: scan() fills this; the loop drains it into :meth:`_finish_end`
        #: tasks. Same in-memory trade as the reminder's timers.
        self._end_pending: List[Tuple[str, str, str, str]] = []
        #: In-flight end-sequences — cancelled at shutdown, resumed by the
        #: state-on-sight rule on the next boot.
        self._end_tasks: Set[asyncio.Task] = set()
        #: Events found but not yet delivered — retried every poll.
        self._debt: List[dict] = []
        #: Drivers whose running v2 run has entered its settle phase (a
        #: ``settle`` record in its ``signals`` log, claunch-5aysr), as
        #: (driver, run, reviewers to keep for now): scan() fills it, the loop
        #: ends their reviewers.
        self._settle_pending: List[Tuple[str, str, frozenset]] = []
        #: ``(driver, reviewer, reason) -> None``: told of every reviewer this
        #: clock ends, so the person hears of it on the dashboard's
        #: notification channel (kill_gate.KillGate.note_auto). Set by the
        #: daemon's main; None in tests that do not care.
        self.on_reviewer_ended: Optional[Callable[[str, str, str], None]] = None

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
            # pending next start both keep their driver. A run whose session
            # is not running yet is NOT marked (claunch-u9m6z): right after a
            # boot its session may still be held or queued for a delayed
            # restore, and marking it here exempted it until the next boot —
            # the session woke (the restore queue, a mesh delivery) and
            # stayed alive with its run done. Left unmarked, the next pass
            # judges it again; the v2 pass gets this by walking live
            # sessions only.
            if (
                run_id and status == "done"
                and not payload.get("recur")
                and not pending_by
                and (cwd, scope, run_id) not in self._end_done
            ):
                session = None
                if kill_on_end:
                    session = session_for(self.manager, cwd, scope)
                    if session is None:
                        continue                # not running: ask again next pass
                self._end_done.add((cwd, scope, run_id))
                if kill_on_end:
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
                            self._end_pending.append(
                                (cwd, scope, run_id,
                                 str(payload.get("workflow") or "?"))
                            )
                continue
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
        self._scan_v2_ends(kill_on_end)
        events.extend(self._exit_events(enabled))
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return events

    def session_exited(self, session) -> None:
        """Exit hook (``SessionManager.exit_hooks``): note an ending nobody
        asked for, for the next :meth:`scan` to judge.

        Runs on the loop, so it only records; reading the run's state is disk
        work and belongs to the scan's thread. An ending somebody asked for
        -- :meth:`Session.kill` (every kill verb, the beads wind-down,
        kill-on-end, pause) or :meth:`Session.shutdown` (redefine, migrate,
        archive, clear --running) -- is not recorded: whoever asked
        already knows, and in the live fleet those were almost every orphaned
        report the overseer got (s773 on claunch-4i1n2: 3 of 69 runs' first
        reports led to any action, all three a worker that crashed mid-work).
        A session with no parent has nobody this push is for.

        Every exit that reaches this hook also ends the session's own
        reviewer children (claunch-472wq.1), asked-for or not: a reviewer
        answers its parent's asks, and an exited parent asks nothing. The
        hook never sees a daemon shutdown, a relaunch, a hold or an exit
        Windows caused (``SessionManager._session_exited``), so those keep
        their reviewers; a pause is skipped here for the same reason — it is
        a stop the session comes back from.
        """
        if getattr(session, "paused_at", None):
            return
        self._end_reviewers(session.sdef.name, reason="parent-exited")
        if getattr(session, "kill_requested", False):
            return
        sdef = session.sdef
        if not sdef.parent or not sdef.cwd:
            return
        self._exits.append((sdef.name, sdef.cwd, sdef.parent))

    def _exit_events(self, enabled: bool) -> List[dict]:
        """The ``orphaned`` events for the exits :meth:`session_exited`
        recorded since the last pass: one per exit whose run was left
        unfinished. Drained whether or not the switch is on -- off is quiet,
        and an exit heard in the dark is not replayed when it comes back."""
        events: List[dict] = []
        while self._exits:
            name, cwd, parent = self._exits.popleft()
            if not enabled:
                continue
            if self._live(name) is not None:
                # Back already (redefine, respawn): the run has a driver.
                continue
            try:
                resolved = cflow_state.resolve_cwd(cwd)
                if name not in cflow_state.scopes_in(resolved):
                    continue
                payload = cflow_engine.status(resolved, scope=name)
            except Exception as exc:
                log.debug("cflow run events: exit of %r unreadable: %s", name, exc)
                continue
            if not payload.get("run") or payload.get("status") in _SETTLED:
                continue
            event = self._event(resolved, name, "orphaned", payload)
            event["to"] = parent
            events.append(event)
        return events

    def _event(self, cwd: str, scope: str, kind: str, payload: dict) -> dict:
        return {
            "cwd": cwd,
            "scope": scope,
            "kind": kind,
            "block": event_block(scope, kind, payload),
        }

    def _scan_v2_ends(self, kill_on_end: bool) -> None:
        """Kill-on-end for cflow v2 runs (claunch-du6mx).

        The v1 pass above reads the v1 registry only, so a v2 one-shot run
        that reached ``done`` left its driver alive -- while its wrap-up prose
        (improv-worker-v2's end-gate) tells the agent the daemon ends the
        session. Same contract as the v1 pass: on sight, once per run, record
        first, then queue the same :meth:`_finish_end` (idle wait, keep-alive
        and resumed-by-human re-read beside the kill, session-ended notice).

        v2 runs live under their driver's directory (``.cflow2/runs/<scope>``),
        so the live sessions' directories are walked, as the v2 landing
        holders are. Only a main run counts: a side run (``<session>+<side>``)
        and a nested delegation run (``parent`` set) end with their main run's
        session, never on their own. A run whose chart declares ``recur`` or
        that filed a next round keeps its driver. The queued ``(cwd, scope)``
        is the driver's own: ``session_for``'s canonical cwd and name, so
        every later lookup in the end-sequence finds the same session.
        """
        from ..cflow2 import runtime as v2_runtime, store as v2_store

        try:
            sessions = [s for s in self.manager.list() if not getattr(s, "exited", True)]
        except Exception:
            return
        for session in sessions:
            sdef = getattr(session, "sdef", None)
            name = str(getattr(sdef, "name", "") or "")
            cwd = str(getattr(sdef, "cwd", "") or "")
            if not name or not cwd:
                continue
            try:
                scopes = v2_store.list_scopes(cwd)
            except Exception:
                continue
            for scope in scopes:
                if v2_store.side_of(scope):
                    continue
                st = v2_runtime.RunStore(cwd, scope)
                try:
                    state = st.load_state()
                except Exception:
                    continue
                run_id = str(state.get("run_id") or "")
                if (
                    run_id
                    and state.get("status") != v2_runtime.ST_DONE
                    and not state.get("parent")
                    and v2_runtime.driver_of(state) == name
                    and settle_signalled(state)
                ):
                    self._queue_settle_reap(name, run_id)
                if (
                    not run_id
                    or state.get("status") != v2_runtime.ST_DONE
                    or state.get("parent")
                    or state.get("next_round")
                    or v2_runtime.driver_of(state) != name
                ):
                    continue
                key = (str(st.cwd), scope, "cflow2:" + run_id)
                if key in self._end_done:
                    continue
                self._end_done.add(key)
                try:
                    if (st.load_chart().meta or {}).get("recur"):
                        continue
                except Exception:
                    continue
                if not kill_on_end:
                    continue
                try:
                    v1_cwd = cflow_state.resolve_cwd(cwd)
                except Exception:
                    continue
                if session_for(self.manager, v1_cwd, name) is not session:
                    continue
                workflow = str(state.get("workflow") or "?")
                block = end_block(
                    name, run_id, workflow,
                    keep_alive=bool(session.sdef.keep_alive),
                )
                if not session.append_wal(block):
                    log.error(
                        "cflow kill-on-end: could not durably record "
                        "the ending of %r (v2 run %s); leaving the "
                        "session alive", name, run_id,
                    )
                    continue
                log.info(
                    "cflow kill-on-end: recorded the ending of %r (v2 run %s)",
                    name, run_id,
                )
                self._end_pending.append(
                    (v1_cwd, name, run_id, workflow, True, (str(st.cwd), scope))
                )

    def _drain_ends(self) -> None:
        """Hand the queued end-sequences to the loop as tasks.

        Called from :meth:`_run` only — it needs a running loop. Each
        sequence is detached (a kill can wait out a turn, seconds); failures
        are caught inside :meth:`_finish_end`.
        """
        while self._settle_pending:
            driver, run_id, keep = self._settle_pending.pop(0)
            self._end_reviewers(driver, run_id, reason=SETTLE_REAP, keep=keep)
        while self._end_pending:
            cwd, scope, run_id, workflow, *rest = self._end_pending.pop(0)
            task = asyncio.get_running_loop().create_task(
                self._finish_end(
                    cwd, scope, run_id, workflow,
                    reap_reviewers=bool(rest and rest[0]),
                    v2_slot=rest[1] if len(rest) > 1 else None,
                )
            )
            self._end_tasks.add(task)
            task.add_done_callback(self._end_tasks.discard)

    async def _finish_end(
        self, cwd: str, scope: str, run_id: str, workflow: str = "?",
        *, reap_reviewers: bool = False,
        v2_slot: Optional[Tuple[str, str]] = None,
    ) -> None:
        """End a finished one-shot run's session: wait out its current turn
        (bounded), then kill unless it was kept alive or a person respawned
        it since the run finished.

        The durable record is already in the session's transcript — the scan
        that queued this wrote it first, and would not have queued a kill it
        could not record. What remains is timing and the flag: no immediate
        kill (the driver may still be finishing the turn that produced the
        done position), and the keep-alive flag re-read right beside the kill,
        so one set while the wait ran still protects the session. An exited
        session is left alone at every step — the kill verbs are idempotent,
        but nothing here needs to call one twice.

        ``v2_slot`` is the finished v2 run's ``(run cwd, slot scope)``. The
        slot is re-read beside the kill as the flag is (claunch-5aysr): a run
        started in that slot since the scan (settle's ``new-task``, a few
        seconds after done) holds the session now, so the end is called off
        -- reviewers included, they may be the new run's -- and the end
        record gets its correction.
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
        successor = _v2_successor(v2_slot, run_id) if v2_slot else None
        if successor:
            log.info(
                "cflow kill-on-end: %r started run %s in slot %s after run %s "
                "finished; leaving it running", scope, successor, v2_slot[1], run_id,
            )
            session.append_wal(superseded_block(scope, run_id, successor))
            return
        if session.sdef.keep_alive:
            log.info(
                "cflow kill-on-end: %r recorded but left running (keep-alive)",
                scope,
            )
            # The driver stays, its run does not (claunch-472wq.1): the
            # reviewer was there for that run's peer-review asks, and a
            # kept-alive driver that starts new work spawns a new one.
            if reap_reviewers:
                self._end_reviewers(scope, run_id)
            return
        if session.resumed_by_human:
            # A person respawned this session, by name, after the run had
            # already finished -- unlike keep-alive (a standing "never"),
            # this is a one-time deference: it holds off THIS kill, and
            # lapses at the next daemon restart, when a fresh incarnation
            # (auto-restored, not asked for) is judged from scratch. The
            # earlier end_block already reads "the session is ended" from
            # the scan that queued this, so the record needs a correction
            # or a future reader sees a session alive past its own "ended"
            # notice with nothing explaining why.
            log.info(
                "cflow kill-on-end: %r was respawned by a person after "
                "run %s finished; leaving it running", scope, run_id,
            )
            session.append_wal(resumed_block(scope, run_id))
            # The deference is to the driver; the run is still done, so its
            # reviewer goes as it does under keep-alive (claunch-472wq.1).
            if reap_reviewers:
                self._end_reviewers(scope, run_id)
            return
        if reap_reviewers:
            self._end_reviewers(scope, run_id)
        try:
            actors.note(session, actors.make(actors.CFLOW, scope, reason="kill-on-end"))
            session.kill(force=False)
            self.manager.persist()
            log.info("cflow kill-on-end: ended %r (run %s)", scope, run_id)
        except Exception as exc:
            log.warning("cflow kill-on-end: ending %r failed: %s", scope, exc)
            return
        # The kill landed. Everyone still holding a conversation with this
        # session is now talking into a terminal that is not there, and the
        # only one positioned to say so is this clock: the session cannot
        # announce its own death after the fact, and the ``session ended``
        # block it wrote goes into ITS transcript, which nobody else reads.
        # So the overseer is told, through the same debt queue the other
        # events use (retried every poll until it lands).
        #
        # Deliberately NOT gated on ``cflow_events``: that switch mutes
        # transitions an overseer may reasonably not want typed at it, and
        # this is not a transition — it is the fleet losing a member, and
        # the switch that governs it is the one that caused the kill
        # (``cflow_kill_on_end``, already checked by the scan that queued
        # this). A daemon that ends sessions silently is the shape of the
        # complaint this notice answers.
        self._debt.append({
            "cwd": cwd,
            "scope": scope,
            "kind": "session-ended",
            "block": event_block(
                scope, "session-ended",
                {"run": run_id, "workflow": workflow},
            ),
        })

    def _queue_settle_reap(self, driver: str, run_id: str) -> None:
        """Queue the settle-phase reap of ``driver``'s reviewers
        (claunch-5aysr). Runs in scan()'s thread: it only reads. Nothing is
        queued while no reviewer child is alive, so a run that waits an hour
        in settle costs one children read per pass after its reviewer is
        gone. A reviewer with an open ask addressed to it is kept for this
        pass -- it is mid-answer, and the next pass looks again."""
        reviewers = self._reviewer_children(driver)
        if not reviewers:
            return
        keep = set()
        try:
            from ..cflow2 import asks as cflow2_asks
            for name in reviewers:
                if cflow2_asks.pending_for(name):
                    keep.add(name)
        except Exception:  # noqa: BLE001 -- unreadable asks: keep them all
            keep = set(reviewers)
        if keep == set(reviewers):
            return
        self._settle_pending.append((driver, run_id, frozenset(keep)))

    def _reviewer_children(self, driver: str) -> List[str]:
        """The live direct children of ``driver`` holding the ``reviewer``
        role on some mesh -- the sessions :meth:`_end_reviewers` ends."""
        if self.mesh is None:
            return []
        try:
            children = list(self.manager.children(driver))
        except Exception:
            return []
        out: List[str] = []
        for name in children:
            child = self._live(name)
            if child is None or child.sdef.parent != driver:
                continue
            try:
                roles = {
                    str(r)
                    for m in self.mesh.meshes_for_session(name)
                    for r in ([m.get("role")] + list(m.get("roles") or []))
                    if r
                }
            except Exception:
                continue
            if "reviewer" in roles:
                out.append(name)
        return out

    def _end_reviewers(
        self, driver: str, run_id: str = "", *, reason: str = "reviewer-reap",
        keep: frozenset = frozenset(),
    ) -> List[str]:
        """End the live reviewers ``driver`` spawned itself (claunch-du6mx.1,
        claunch-472wq.1). Returns the names ended.

        A reviewer child exists to answer its parent's peer-review asks
        (improv-worker-v2's reviewer-ensure spawns one and keeps it for the
        run); once that run is done, or its parent is gone, it has nothing
        left to review. The daemon ends it -- the agent does not (its kill
        waits for a person, claunch-5aysr) -- at four moments: when the run
        is in its settle phase (:func:`settle_signalled` -- not once it has
        moved back into its rounds -- the reviewer kept
        while an ask is still addressed to it), right
        before the kill-on-end of a done run's driver, when that kill is
        withheld (keep-alive, respawned by a person) because the run is done
        all the same, and when the parent exits for any other reason
        (:meth:`session_exited`). Only direct children (``sdef.parent``)
        holding the ``reviewer`` role on some mesh: a worker child is the
        settle/adopt rules' to dispose of, and a grandchild belongs to its
        own parent.
        """
        ended: List[str] = []
        for name in self._reviewer_children(driver):
            if name in keep:
                continue
            child = self._live(name)
            if child is None:
                continue
            try:
                actors.note(child, actors.make(
                    actors.CFLOW, driver, reason=reason))
                child.kill(force=False)
            except Exception as exc:
                log.warning(
                    "cflow %s: ending reviewer %r of %r failed: %s",
                    reason, name, driver, exc,
                )
                continue
            ended.append(name)
            log.info(
                "cflow %s: ended reviewer %r of %r (run %s)",
                reason, name, driver, run_id or "-",
            )
            if self.on_reviewer_ended is not None:
                with contextlib.suppress(Exception):
                    self.on_reviewer_ended(driver, name, reason)
        if ended:
            with contextlib.suppress(Exception):
                self.manager.persist()
        return ended

    async def _deliver(self, event: dict) -> bool:
        """Type the event into its overseer. True = settled (delivered, or
        dropped for want of anyone to tell); False keeps the debt."""
        if "to" in event:
            # Addressed at the exit: that parent, alive, or nobody -- no
            # mesh-leader fallback, which is where the reports about other
            # trees' dead runs used to come from.
            target = self._live(event["to"])
        else:
            target = self._recipient(event["cwd"], event["scope"])
        if target is None:
            log.info(
                "cflow run event (%s) about %r dropped: no overseer to tell",
                event["kind"], event["scope"],
            )
            return True
        try:
            with delivery_wake("cflow"):
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

    def _live(self, name: str):
        try:
            candidate = self.manager.get(name)
        except Exception:
            return None
        return None if candidate.exited else candidate

    def _recipient(self, cwd: str, scope: str):
        """The live session to tell: spawn parent first, mesh leader after.

        The parent is answered by the manager alone and is the leader in the
        fleet shape this clock exists for. The fallback needs the mesh: the
        driver's memberships, disambiguated by the run's own ``mesh`` field
        when it has one, then the local ``leader``-role member. Ambiguity is
        a skip, never a guess.
        """
        try:
            parent = self.manager.get(session_name(scope)).sdef.parent or ""
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
            if "leader" not in member.roles or member.session == scope:
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


def resumed_block(scope: str, run_id: str) -> str:
    """Correction to a session's own :func:`end_block`: it said the session
    was ending, and a person respawned it before that landed.

    Appended the same durable way, so a future reader of the transcript
    sees why a session marked "ended" above is running after all, instead
    of the ending simply not happening with no trace of why.
    """
    lines = [
        "---",
        "# claunch: session ended notice superseded -- machine-generated, "
        "not typed by the user",
        f"session: {scope}",
        f"run: {run_id}",
        "outcome: a person respawned this session after the notice above "
        "was written, so the pending end was called off",
        "note: this holds only until the daemon that saw the respawn goes "
        "down -- the next restart judges the session fresh, same as before",
        "---",
    ]
    return "\n".join(lines)


def _v2_successor(slot: Tuple[str, str], run_id: str) -> Optional[str]:
    """The id of the run now in v2 ``slot`` when it is not ``run_id`` --
    a later run took the slot -- else None. An unreadable slot is None: the
    end goes ahead as it did before this check (claunch-5aysr)."""
    from ..cflow2 import runtime as v2_runtime

    try:
        current = str(v2_runtime.RunStore(*slot).load_state().get("run_id") or "")
    except Exception:  # noqa: BLE001 -- no slot state: nothing to compare
        return None
    return current if current and current != run_id else None


def superseded_block(scope: str, run_id: str, successor: str) -> str:
    """Correction to a session's own :func:`end_block`: a new run started
    in the same slot before the end landed (claunch-5aysr), so the session
    keeps running for it. Appended the same durable way as
    :func:`resumed_block`."""
    lines = [
        "---",
        "# claunch: session ended notice superseded -- machine-generated, "
        "not typed by the user",
        f"session: {scope}",
        f"run: {run_id}",
        f"next run: {successor}",
        "outcome: a new run started in this session's slot after the notice "
        "above was written, so the pending end was called off",
        "---",
    ]
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
    elif kind == "session-ended":
        lines.append(
            f"event: finished run {payload.get('run')} of {workflow} and "
            "the session has been ENDED -- its slot is back and nothing "
            "is reading that terminal any more"
        )
        lines.append(
            "note: mesh messages addressed to it from here on are queued, "
            "not delivered, and the sender is told so. Its branch and its "
            "beads issue outlive the session -- take anything unfinished "
            "from those. `claunch respawn " + scope + "` brings it back if "
            "a person is really needed there."
        )
    elif kind == "orphaned":
        lines.append(
            f"event: session exited without a kill request while run "
            f"{payload.get('run')} of {workflow} was at step '{step}' -- "
            "nobody is driving it"
        )
        lines.append(
            "note: sent once, at the exit. Its branch and its beads issue "
            "outlive the session; `claunch respawn " + scope + "` resumes "
            "it where it stopped."
        )
    lines.append(
        f"read it yourself: claunch cflow status -t {scope} --json "
        f"(details: claunch cflow journal -t {scope})"
    )
    lines.append("---")
    return "\n".join(lines)


def run_summary(
    name: str, cwd: str, scopes: Optional[Dict[str, frozenset]] = None
) -> Optional[dict]:
    """The cflow run session ``name`` drives in ``cwd``, compactly — or None.

    The same containment rule as the clocks, read in the other direction: the
    scope IS the session name and the run must live in the session's own
    directory. Serves the ``children`` API view, so an overseer's roster can
    carry each child's run position without a second tool.

    ``scopes`` is a directory -> scope-set map that a caller asking about many
    sessions at once may pass, so the containment check lists each directory
    once instead of once per session; :func:`run_summarizer` builds one and
    owns it. Left out, the directory is listed on every call as before.
    """
    if not name or not cwd:
        return None
    try:
        resolved = cflow_state.resolve_cwd(cwd)
        if scopes is None:
            known = cflow_state.scopes_in(resolved)
        else:
            known = scopes.get(resolved)
            if known is None:
                known = scopes[resolved] = frozenset(cflow_state.scopes_in(resolved))
        if name not in known:
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


def run_summarizer() -> Callable[[str, str], Optional[dict]]:
    """:func:`run_summary` with the directory listing shared across its calls.

    ``beads.queues_view`` asks for one summary per lane, and the containment
    check lists the lane's directory each time. That answer is a property of
    the directory, so lanes sharing one paid for the same listing repeatedly:
    measured on the live daemon (s586, 2026-09-22 -- 703 sessions over 132
    directories, two of which held 307 and 252 of them), the listings cost
    2618 ms asked once per session and 31 ms asked once per directory. On the
    event loop that also pumps the terminals.

    The map lives exactly as long as the returned callable, so a caller gets
    one consistent reading per response. A run that starts inside that window
    is not seen by it, which is the window the response already was.
    """
    scopes: Dict[str, frozenset] = {}

    def one(name: str, cwd: str) -> Optional[dict]:
        return run_summary(name, cwd, scopes)

    return one
