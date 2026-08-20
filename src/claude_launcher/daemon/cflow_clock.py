"""The clocks cflow cannot carry itself.

Everything else in cflow happens because somebody called a tool: the agent
advances, a human approves, a responder answers. Three things have nobody to
call them, so the daemon carries all three, scanning the same machine-local
run registry the dashboard lists runs from:

* :class:`AskClock` — a delegated decision's ``timeout``. The one agent that
  would notice an expiry is the one stopped waiting for the answer.
* :class:`ReminderClock` — the step instructions an agent has drifted away
  from. The agent that would notice it has forgotten the protocol is,
  definitionally, the one that forgot it.
* :class:`RunEventClock` — the moment a run stops being its own agent's: a
  human gate entered, a recurring round finished, a driver that exited. The
  session that would want to know — the overseer that spawned the driver —
  is precisely not the one anything happens in, so nothing else tells it.

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
from ..cflow import engine as cflow_engine, state as cflow_state
from .session import STATUS_BUSY

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
    same instruction again every interval until it moves.

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
            if not _actionable(payload):
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
        if session.status() != STATUS_BUSY:
            # Not working: an idle agent has ended its turn, a suspended or
            # wedged one is not reading, and a paste into either would open
            # a fresh turn just to restate a protocol nobody is mid-way
            # through forgetting. Held, not dropped — the debt stays due and
            # is retried each poll, so the reminder lands the moment the
            # session is working again.
            log.debug("cflow reminder held for %r: session is not working", scope)
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("cflow reminder delivery to %r failed", scope)
            return
        if delivered:
            # Rearm only on success: a delivery that failed past deliver's
            # holds keeps its debt and is tried again next poll.
            entry = self._seen.get((cwd, scope))
            if entry is not None:
                entry["at"] = time.monotonic()
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


# --------------------------------------------------------------------------- #
# the run event clock
# --------------------------------------------------------------------------- #
#: How often the event clock looks. The transitions it reports move at human
#: and agent speed (minutes), so the reminder clock's cadence is plenty.
EVENT_POLL = 20.0

#: Statuses in which the run is somebody's to advance — the complement of the
#: blocked/parked positions the events below report entering.
_RUNNING = ("step", "select")


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
