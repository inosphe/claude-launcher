"""The clocks cflow cannot carry itself.

Everything else in cflow happens because somebody called a tool: the agent
advances, a human approves, a responder answers. Two things have nobody to
call them, so the daemon carries both, scanning the same machine-local run
registry the dashboard lists runs from:

* :class:`AskClock` — a delegated decision's ``timeout``. The one agent that
  would notice an expiry is the one stopped waiting for the answer.
* :class:`ReminderClock` — the step instructions an agent has drifted away
  from. The agent that would notice it has forgotten the protocol is,
  definitionally, the one that forgot it.

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
from typing import Dict, List, Optional, Tuple

from .. import store
from ..cflow import engine as cflow_engine, state as cflow_state

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

#: The two statuses where the *agent* is the one the run is waiting on. Every
#: other position is blocked on somebody else — a human's gate, a responder's
#: answer — and re-typing step instructions there would tell the agent to act
#: on a step it is not allowed to enter.
_ACTIONABLE = ("step", "select")

#: A reminder restates, it does not re-document: past this the step's own
#: `status` call is the readable copy, and the block says so.
_INSTRUCTIONS_LIMIT = 1200

#: Screen changes within this many seconds of a delivery are the delivery —
#: the paste echoing and the Enter rendering — not the agent responding.
REMINDER_ECHO_GRACE = 5.0


class ReminderClock:
    """Re-types the current step's instructions into runs that stopped moving.

    Sessions forget the /cflow protocol the way they forget everything else —
    compaction, distraction, a long side quest — and a forgotten run does not
    fail, it just sits. This clock watches every run on the machine and, when
    one has held the same agent-actionable position for its reminder interval,
    types that position's instructions back into the driving session.

    *No progress* is the trigger, not the calendar: the position key
    (run, status, step, visit) resets the timer whenever it changes, so an
    agent that is advancing hears nothing, and one that has stalled hears the
    same instruction again every interval until it moves. Delivery is
    :meth:`Session.deliver` — idle-gated, so the reminder also never lands
    mid-keystroke on an agent that is merely slow.

    And *one unanswered reminder at a time*: a repeat is sent only to a
    session that has shown meaningful screen activity since the previous one
    landed (beyond the paste's own echo). A session that is effectively
    suspended — its process stopped, its machine asleep, its TUI wedged —
    holds exactly one reminder, not an interval-paced pile of them; the
    moment it shows life again, the held reminder goes out and the cadence
    resumes.

    Configuration is read fresh on every pass: the machine defaults
    (``cflow_reminder`` / ``cflow_reminder_interval``) come from
    ``store.daemon_config()`` — so a ``claunch daemon config`` edit or the web
    UI's PUT applies by the next tick, no restart — and each run may override
    both in its own state (:func:`cflow.engine.set_reminder`).
    """

    def __init__(self, manager, *, poll: float = REMINDER_POLL) -> None:
        self.manager = manager
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
                for cwd, scope, block in due:
                    await self._deliver(cwd, scope, block)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One unreadable run must not stop the clock for the rest.
                log.exception("cflow reminder clock tick failed")

    def scan(self, now: float) -> List[Tuple[str, str, str]]:
        """Decide who is due. Blocking (config + every run's state); call it
        in a thread. Public for the tests, which own ``now`` there."""
        try:
            cfg = store.daemon_config()
        except store.StoreError as exc:
            log.warning("cflow reminder: config unreadable, skipping: %s", exc)
            return []
        default_on = bool(cfg.get("cflow_reminder"))
        default_interval = float(cfg.get("cflow_reminder_interval") or 0)
        due: List[Tuple[str, str, str]] = []
        live = set()
        for cwd, scope in cflow_state.known_runs():
            key = (cwd, scope)
            live.add(key)
            try:
                payload = cflow_engine.status(cwd, scope=scope)
            except Exception as exc:
                log.debug("cflow reminder skipped %s/%s: %s", cwd, scope, exc)
                continue
            if payload.get("status") not in _ACTIONABLE:
                self._seen.pop(key, None)
                continue
            override = payload.get("reminder") or {}
            enabled = bool(override.get("enabled", default_on))
            interval = float(override.get("interval", default_interval) or 0)
            if not enabled or interval <= 0:
                self._seen.pop(key, None)
                continue
            interval = max(interval, cflow_engine.REMINDER_MIN_INTERVAL)
            pos = (
                payload.get("run"), payload.get("status"),
                payload.get("step_id"), payload.get("visit"),
            )
            entry = self._seen.get(key)
            if entry is None or entry["pos"] != pos:
                # Progress (or first sight) arms the timer; it does not fire
                # it. An agent that just took this step from 'next' has the
                # instructions already.
                self._seen[key] = {"pos": pos, "at": now}
                continue
            if now - entry["at"] >= interval:
                due.append((cwd, scope, reminder_block(payload, interval)))
        for key in list(self._seen):
            if key not in live:
                del self._seen[key]
        return due

    async def _deliver(self, cwd: str, scope: str, block: str) -> None:
        session = self._session_for(cwd, scope)
        if session is None:
            return
        entry = self._seen.get((cwd, scope))
        if entry is not None and not _responded_since(
            session, entry.get("delivered_at")
        ):
            # Nothing has happened on that screen since the last reminder:
            # the session is not consuming input, and a second paste would
            # only queue behind the first. Held, not dropped — the debt stays
            # due and is retried each poll, so the first sign of life gets
            # the reminder at once.
            log.debug(
                "cflow reminder held for %r: no activity since the last one",
                scope,
            )
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow reminder delivery to %r failed", scope)
            return
        if delivered:
            # Rearm only on success: a session that was busy past deliver's
            # holds keeps its debt and is tried again next poll.
            entry = self._seen.get((cwd, scope))
            if entry is not None:
                entry["at"] = entry["delivered_at"] = time.monotonic()
            log.info("cflow reminder delivered to %r (%s)", scope, cwd)

    def _session_for(self, cwd: str, scope: str):
        """The live session this run maps 1:1 to, or None.

        Same containment rule as the dashboard's ``_scope_sessions``: the
        scope IS the session name, and the cwd must match so a name reused in
        another directory is never typed into by that directory's run.
        """
        try:
            session = self.manager.get(scope)
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


def _responded_since(session, delivered_at: Optional[float]) -> bool:
    """Whether the session has shown life since the last reminder landed.

    The signal is the idle tracker's *meaningful* screen change — the same
    one that decides busy/idle — so a claude spinner does not count as life
    any more than it counts as work. The grace window discounts the delivery
    itself: the paste and its Enter render on screen at delivery time, and a
    session whose only change since is that echo has not responded, it has
    merely received. ``delivered_at`` of ``None`` means no reminder has
    landed at this position yet, and the first one is always allowed —
    waking an idle-but-live agent is the feature.
    """
    if delivered_at is None:
        return True
    last = session.tracker.last_meaningful_change()
    return last is not None and last > delivered_at + REMINDER_ECHO_GRACE


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
        f"{interval:.0f}s while this step does not move",
        f"workflow: {payload.get('workflow')}",
    ]
    step = payload.get("step_id")
    visit = payload.get("visit")
    position = f"step '{step}'" + (f" (visit {visit})" if visit and visit > 1 else "")
    if payload.get("status") == "select":
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
        if payload.get("verify"):
            lines.append(f"verify: {payload['verify']}")
        lines.append(
            "protocol: this is the step you are on -- the same instruction, "
            "repeated because the run has not moved, not a new one. If you "
            "are mid-work, keep going. When it is done, file it with "
            "'report' and advance with 'next'; if you have lost the thread, "
            "call 'status' first -- it is the current truth."
        )
    lines.append("---")
    return "\n".join(lines)
