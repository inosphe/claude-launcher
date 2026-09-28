"""The measurement window: one machine, one arbiter, two classes.

What this replaces, and why it had to be replaced, is on the board
(``claunch-8y5j``, 2026-08-29): the sweep-window protocol stood on process
scans and mesh chat, and four holes were measured -- a scan blind spot of one
to three seconds at startup (``claunch-rwq``, round 12: an entrant 46s after
the holder's start, 33s of overlap), a stale holder blocking the whole queue
until a human noticed (s286's slot 6b), a protocol that never reaches sessions
outside the mesh (``claunch-fnhu``, round 13), and a queue that lived in the
leader's messages. The leader's ruling on ``claunch-rwq`` fixed the direction
-- an occupied/released *state*, not declarations -- and this module is the
state.

The arbiter is the daemon because of what the daemon already is: a single
process (so two acquisitions racing are serialised by construction, and the
scan blind spot stops existing), always up while any session is (every session
is daemon-managed), and the one component that *knows* when a session ends --
:meth:`WindowManager.session_exited` rides the same ``manager.exit_hooks``
funnel as the board's sweep (``daemon/beads.py``), so a holder that dies
releases itself instead of waiting to be noticed.

Two classes, because the resource has two shapes (operator-set values,
2026-08-29, board ``claunch-8y5j``; the measured revision belongs to
``claunch-bl0e``):

* ``sweep`` -- a full-suite run. Capacity 1 and **exclusive against
  everything**: while a sweep holds, no targeted run is granted, and once a
  sweep is *queued* no new targeted grant is made (writer preference -- under
  rotating targeted runs an exclusive requester would starve). Exclusivity is
  the measured requirement, not caution: a targeted run overlapping a full
  sweep flipped red to green in 78 seconds on an identical tree (the
  ``claunch-1dre`` table, 2026-08-27), so a grant that admitted the pair would
  be minting measurements nobody may cite.
* ``targeted`` -- a nodeid-selected run. Capacity 3 (5 until claunch-8kald),
  shared. The cap is enforced by *not granting*, which is what
  ``claunch-95fa``'s third fix asked for: the per-session scan becomes the
  arbiter's knowledge, and eight concurrent targeted runs (where s155/s148
  met xdist node-down) cannot assemble.

The caps are deliberately NOT derived from the CPU count: this suite is not
CPU-bound (32 cores at 15% under 22 pytest processes -- s159's measurement in
``claunch-95fa``); its cost axes are process spawn and PTY/daemon waits. What
the CPU count does govern is the width of *one* run, so each grant carries
``advisory_n`` -- cores divided by active runs, clamped -- which is the fair
scheduling the arbiter is the only component positioned to do, because the
arbiter is the one that knows how many runs are active.

Since claunch-8kald (2026-09-28, user-direct) the width is also *spent*, not
only advised. Three limits bind a grant besides the caps, all read live from
the daemon config:

* a per-class width ceiling (``window_sweep_width`` 8,
  ``window_targeted_width`` 4) -- a targeted run selects a few modules and
  gains nothing from eight workers;
* a machine worker budget (``window_worker_budget`` 12): each holder records
  the width it was granted, and a targeted request waits while the budget is
  spent. The requester states the width it wants (``workers``), so a
  one-module run costs one worker, not four;
* one targeted grant per session at a time (``window_targeted_per_session``).
  A session's subagents run under the same ``CLAUNCH_SESSION``, and parallel
  subagent pytest runs were the uncounted load behind several flaky rounds.

``tests/conftest.py`` clamps the xdist worker count to the granted width, so
a ``-n 16`` typed by hand spends what the arbiter granted and no more.

What is deliberately NOT here:

* a wall-clock TTL. The repository has already paid for that confusion once
  (s143's 19 minutes, cut by a tool ceiling and lost silently): a clock cannot
  tell a stuck holder from a slow legitimate run, and the honest reapers --
  the session exit hook and pid liveness -- already cover death. A holder
  whose session lives and whose pid lives is holding.
* preemption. A forced grant (below) is added next to the running holders;
  nothing running is stopped.

Operator overrides (claunch-8kald): the queue is FIFO with writer preference
until the operator says otherwise. ``prioritize`` gives a waiting request a
priority (higher first, FIFO within a priority; the default is 0), and
``force`` grants a waiting request -- or a fresh operator acquisition --
immediately, past every cap, budget and exclusivity rule. A forced holder
still counts against the caps and the budget for everyone after it. Both are
operator actions: the CLI refuses them inside a managed session (the
``claunch daemon restart`` gate's rule), and the API refuses to force a grant
that names a session.
* enforcement against a process that never asks. That half lives at the point
  of consumption: ``tools/sweep.py run`` acquires before it runs, and
  ``tests/conftest.py`` holds the window for any full-suite pytest however
  launched. This module is the state those two consult.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import atomic, store
from . import paths

log = logging.getLogger(__name__)

#: The two resource classes. See the module docstring for why sweep is
#: exclusive and targeted is not, and why the caps are not CPU-derived.
SWEEP = "sweep"
TARGETED = "targeted"
CLASSES = (SWEEP, TARGETED)

#: ``advisory_n`` bounds: a run narrower than 2 pays xdist's fixed costs for
#: nothing; wider than 8 buys nothing this suite can spend (the bottleneck is
#: spawn and PTY/daemon waits, not cores).
ADVISORY_MIN = 2
ADVISORY_MAX = 8

#: The spending limits (see the module docstring), used when no daemon config
#: is consulted -- tests that inject ``caps`` get these, not the machine's.
DEFAULT_LIMITS = {
    "sweep_width": 8,
    "targeted_width": 4,
    "worker_budget": 12,
    "targeted_per_session": 1,
}

#: Server-side ceiling on an acquire's wait.  A client may choose a shorter
#: wait at assignment time, but no request may keep a daemon connection open
#: for more than 30 minutes.
MAX_WAIT = 1800.0

#: A holder reminder is deliberately independent from the session reminder
#: service.  It concerns a machine-wide resource, including sessions without
#: a cflow run, and must still reach an idle session after a failed test.
REMINDER_INTERVAL = 180.0
REMINDER_POLL = 15.0

_STATE_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    """Is there a live process with this pid? (Best effort, no dependencies.)

    Windows has no ``kill(pid, 0)`` -- ``os.kill`` there *terminates* -- so
    the check is OpenProcess + GetExitCodeProcess. POSIX takes the signal-0
    path. A pid that cannot be queried at all (access denied) counts as
    alive: reaping on an unreadable pid would reap strangers' processes on a
    multi-user machine.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # handle opened; unreadable exit code is not death
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class WindowManager:
    """Holds and grants the measurement window; persists it across restarts.

    ``caps`` returns ``(sweep_cap, targeted_cap)`` and is read on every
    operation, so an operator's edit lands without a restart -- the pattern
    the cflow reminder keys already use. ``manager`` may be None in tests;
    session-liveness reaping then has nothing to consult.

    Holder changes are pull-based observability through :meth:`status` and
    the window API. They are expected high-frequency state transitions, so
    they do not enter the mesh delivery path, where every delivery is an
    agent-facing user message.
    """

    def __init__(
        self,
        manager=None,
        *,
        caps: Optional[Callable[[], Tuple[int, int]]] = None,
        limits: Optional[Callable[[], dict]] = None,
        state_path: Optional[Path] = None,
        cores: Optional[int] = None,
    ) -> None:
        self._manager = manager
        self._caps = caps or self._caps_from_config
        if limits is not None:
            self._limits = limits
        elif caps is not None:
            # A caller that injects its caps is a test fixing the window's
            # shape; reading the machine's config under it would make the
            # test depend on whoever runs it.
            self._limits = lambda: dict(DEFAULT_LIMITS)
        else:
            self._limits = self._limits_from_config
        self._state_path = state_path or (paths.daemon_dir() / "window.json")
        self._cores = cores or (os.cpu_count() or 4)
        self._holders: List[dict] = []
        self._queue: List[dict] = []
        self._granted_events: Dict[str, asyncio.Event] = {}
        self._load()

    # ---- configuration --------------------------------------------------- #

    @staticmethod
    def _caps_from_config() -> Tuple[int, int]:
        cfg = store.daemon_config()
        return (
            int(cfg.get("window_sweep_cap", 1)),
            int(cfg.get("window_targeted_cap", 3)),
        )

    @staticmethod
    def _limits_from_config() -> dict:
        cfg = store.daemon_config()
        return {
            key: int(cfg.get(f"window_{key}", default))
            for key, default in DEFAULT_LIMITS.items()
        }

    def _cap(self, cls: str) -> int:
        sweep_cap, targeted_cap = self._caps()
        return max(1, sweep_cap if cls == SWEEP else targeted_cap)

    def _class_width(self, cls: str) -> int:
        key = "sweep_width" if cls == SWEEP else "targeted_width"
        return max(1, int(self._limits().get(key, DEFAULT_LIMITS[key])))

    def _held_width(self, holder: dict) -> int:
        # A holder persisted before widths were recorded is charged its
        # class ceiling: over-counting delays a grant, under-counting
        # overloads the machine.
        workers = holder.get("workers")
        return int(workers) if workers else self._class_width(holder.get("cls", TARGETED))

    def workers_in_use(self) -> int:
        return sum(self._held_width(h) for h in self._holders)

    def _budget(self) -> int:
        return int(self._limits().get("worker_budget", DEFAULT_LIMITS["worker_budget"]))

    def _budget_left(self) -> Optional[int]:
        """Workers the budget still allows; None when the budget is off."""
        budget = self._budget()
        if budget <= 0:
            return None
        return budget - self.workers_in_use()

    def _width(self, cls: str, requested: Optional[int]) -> int:
        """The xdist width a grant made now would carry."""
        width = min(self._class_width(cls), self.advisory_n(extra=1))
        if requested:
            width = min(width, max(1, int(requested)))
        left = self._budget_left()
        if left is not None:
            width = min(width, max(1, left))
        return max(1, width)

    # ---- persistence ------------------------------------------------------ #

    def _load(self) -> None:
        """Read the persisted window, if any. Reaping is deliberately NOT done
        here: on boot the sessions that held grants may still be restoring, so
        death is judged lazily at the next acquire/status instead."""
        try:
            doc = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("version") != _STATE_VERSION:
            return
        self._holders = list(doc.get("holders") or [])
        self._queue = list(doc.get("queue") or [])

    def _save(self) -> None:
        doc = {
            "version": _STATE_VERSION,
            "saved_at": _utcnow(),
            "holders": self._holders,
            "queue": self._queue,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._state_path) as tmp:
                tmp.write_text(
                    json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8"
                )
                atomic.replace(tmp, self._state_path)
        except OSError as exc:
            # The in-memory window is the arbiter; the file is its survivor.
            # A failed save must not fail the grant it was recording.
            log.warning("window: state not saved: %s", exc)

    # ---- reaping ------------------------------------------------------------ #

    def _session_gone(self, name: str) -> bool:
        if self._manager is None:
            return False
        try:
            session = self._manager.get(name)
        except Exception:
            return True  # the daemon never heard of it: gone
        return bool(getattr(session, "exited", False))

    def _dead(self, entry: dict) -> bool:
        """A holder/waiter nobody is behind: dead session, or a sessionless
        (manual) entry whose pid is gone."""
        session = entry.get("session")
        if session:
            return self._session_gone(session)
        pid = entry.get("pid") or 0
        return not pid_alive(pid)

    def _reap(self) -> bool:
        """Drop dead holders and waiters. Returns True if anything changed."""
        before_h = len(self._holders)
        before_q = len(self._queue)
        self._holders = [h for h in self._holders if not self._dead(h)]
        self._queue = [q for q in self._queue if not self._dead(q)]
        dropped = before_h - len(self._holders) + before_q - len(self._queue)
        if dropped:
            log.info("window: reaped %d dead holder/waiter entr(ies)", dropped)
        return dropped > 0

    # ---- granting ----------------------------------------------------------- #

    def _blocker(
        self, cls: str, session: Optional[str], ahead: List[dict]
    ) -> Optional[str]:
        """Why a request of this class cannot be granted now, or None.

        ``ahead`` is the part of the queue that outranks the request. The
        reason is returned to the requester, so a refused caller learns which
        rule it met instead of guessing from the holder list.
        """
        if cls == SWEEP:
            if self._holders:
                return "the window is held and a sweep is exclusive"
            return None
        if any(h["cls"] == SWEEP for h in self._holders):
            return "a sweep holds the window"
        if any(q["cls"] == SWEEP for q in ahead):
            return "a sweep is queued ahead (writer preference)"
        cap = self._cap(TARGETED)
        if sum(1 for h in self._holders if h["cls"] == TARGETED) >= cap:
            return f"the targeted cap ({cap}) is reached"
        per_session = int(
            self._limits().get(
                "targeted_per_session", DEFAULT_LIMITS["targeted_per_session"]
            )
        )
        if session and per_session > 0:
            mine = sum(
                1
                for h in self._holders
                if h["cls"] == TARGETED and h.get("session") == session
            )
            if mine >= per_session:
                return (
                    f"session {session} already holds {mine} targeted grant(s); "
                    f"the limit is {per_session} per session"
                )
        left = self._budget_left()
        if left is not None and left < 1:
            return f"the worker budget ({self._budget()}) is spent"
        return None

    def _grantable(
        self, cls: str, session: Optional[str] = None, ahead: Optional[List[dict]] = None
    ) -> bool:
        return self._blocker(cls, session, self._queue if ahead is None else ahead) is None

    def _grant(self, entry: dict, *, forced: bool = False) -> dict:
        holder = dict(entry)
        requested = holder.pop("requested", None)
        if forced:
            # Past every limit by definition; the width still honours the
            # class ceiling and the requester's own ask.
            width = self._class_width(holder["cls"])
            holder["workers"] = min(width, int(requested)) if requested else width
            holder["forced"] = True
        else:
            holder["workers"] = self._width(holder["cls"], requested)
        holder["acquired_at"] = _utcnow()
        holder.pop("enqueued_at", None)
        holder.pop("priority", None)
        self._holders.append(holder)
        event = self._granted_events.pop(entry["grant_id"], None)
        if event is not None:
            event.set()
        return holder

    @staticmethod
    def _priority(entry: dict) -> int:
        try:
            return int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    def _sort_queue(self) -> None:
        # Stable: FIFO order survives inside each priority.
        self._queue.sort(key=lambda q: -self._priority(q))

    def _ahead_of(self, priority: int) -> List[dict]:
        """The waiting entries a new request of this priority queues behind."""
        return [q for q in self._queue if self._priority(q) >= priority]

    def _process_queue(self) -> List[dict]:
        """Grant everything the queue allows, in priority-then-FIFO order.

        Writer preference falls out of the predicates: a queued sweep makes
        every targeted entry behind it ungrantable, and a sweep grants only
        into an empty window. An entry that stays blocked joins ``ahead`` for
        the entries after it.
        """
        granted = []
        ahead: List[dict] = []
        for entry in list(self._queue):
            if self._blocker(entry["cls"], entry.get("session"), ahead) is not None:
                ahead.append(entry)
                continue
            self._queue.remove(entry)
            granted.append(self._grant(entry))
        return granted

    def advisory_n(self, extra: int = 0) -> int:
        """The width one run should take, given how many are running.

        The one number the arbiter knows that no scanning session can: how
        many runs are actually active. ``extra`` counts a grant about to be
        made, so the answer a requester gets already includes itself.
        """
        active = max(1, len(self._holders) + extra)
        return max(ADVISORY_MIN, min(ADVISORY_MAX, self._cores // active))

    # ---- the public surface ------------------------------------------------- #

    async def acquire(
        self,
        cls: str,
        *,
        session: Optional[str],
        pid: int = 0,
        label: str = "",
        wait: float = 0.0,
        workers: int = 0,
        force: bool = False,
    ) -> dict:
        """Grant the window, or queue for it up to ``wait`` seconds.

        A ``wait`` of 0 asks without queueing and jumps nobody: with a
        non-empty queue the answer is ``granted: False`` even into a grantable
        slot, because the slot belongs to the queue's head.

        ``workers`` is the xdist width the requester wants (0 = the class
        ceiling); the grant's ``advisory_n`` never exceeds it. ``force`` is the
        operator's immediate grant, and is refused for a session-held request.
        """
        if cls not in CLASSES:
            return {"granted": False, "error": f"unknown window class {cls!r}"}
        if force and session:
            return {
                "granted": False,
                "error": "force is an operator action; a session-held request "
                "cannot be forced (ask the operator to run `claunch window force`)",
            }
        changed = self._reap()
        if changed:
            self._process_queue()
            self._save()
        entry = {
            "grant_id": secrets.token_hex(6),
            "cls": cls,
            "session": session,
            "pid": pid,
            "label": label,
            "enqueued_at": _utcnow(),
            "priority": 0,
        }
        if workers:
            entry["requested"] = max(1, int(workers))
        if force:
            holder = self._grant(entry, forced=True)
            self._save()
            log.info("window: operator forced a %s grant (%s)", cls, holder["grant_id"])
            return {
                "granted": True,
                "grant_id": holder["grant_id"],
                "advisory_n": holder["workers"],
                "forced": True,
            }
        ahead = self._ahead_of(0)
        blocker = self._blocker(cls, session, ahead)
        if not ahead and blocker is None:
            holder = self._grant(entry)
            self._save()
            return {
                "granted": True,
                "grant_id": holder["grant_id"],
                "advisory_n": holder["workers"],
            }
        reason = blocker or f"{len(ahead)} request(s) are queued ahead"
        if wait <= 0:
            if changed:
                self._save()
            return {
                "granted": False,
                "position": len(ahead) + 1,
                "reason": reason,
                "window": self.status(),
            }
        event = asyncio.Event()
        self._granted_events[entry["grant_id"]] = event
        self._queue.append(entry)
        self._sort_queue()
        # Entries ahead may be blocked by a rule that does not bind this one
        # (another session's per-session limit); the queue decides, in order.
        self._process_queue()
        self._save()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, MAX_WAIT))
        except asyncio.TimeoutError:
            if entry in self._queue:
                self._queue.remove(entry)
                self._save()
            return {
                "granted": False,
                "timeout": True,
                "window": self.status(),
            }
        except asyncio.CancelledError:
            # A disconnected long-poll must not leave an orphan that can be
            # granted later without a client to receive the grant id.
            if entry in self._queue:
                self._queue.remove(entry)
                self._save()
            raise
        finally:
            self._granted_events.pop(entry["grant_id"], None)
        holder = next(
            (h for h in self._holders if h["grant_id"] == entry["grant_id"]), {}
        )
        result = {
            "granted": True,
            "grant_id": entry["grant_id"],
            "advisory_n": holder.get("workers") or self._width(cls, workers or None),
        }
        if holder.get("forced"):
            result["forced"] = True
        return result

    def release(self, grant_id: str) -> bool:
        """Hand the window back. False = no such grant (already reaped?)."""
        holder = next((h for h in self._holders if h["grant_id"] == grant_id), None)
        if holder is None:
            return False
        self._holders.remove(holder)
        self._process_queue()
        self._save()
        return True

    def release_session(self, session: str) -> int:
        """Everything this session holds, back. Returns how many."""
        ids = [h["grant_id"] for h in self._holders if h.get("session") == session]
        return sum(1 for gid in ids if self.release(gid))

    def cancel(self, grant_id: str) -> bool:
        """Remove one waiting request.  A holder cannot be cancelled here."""
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return False
        self._queue.remove(entry)
        self._granted_events.pop(grant_id, None)
        self._save()
        return True

    def cancel_session(self, session: str) -> int:
        """Drop every waiting request from this session (a waiter giving up)."""
        entries = [q for q in self._queue if q.get("session") == session]
        for entry in entries:
            self._queue.remove(entry)
            self._granted_events.pop(entry["grant_id"], None)
        if entries:
            self._save()
        return len(entries)

    # ---- operator overrides (claunch-8kald) ------------------------------------ #

    def prioritize(self, grant_id: str, priority: Optional[int] = None) -> Optional[dict]:
        """Give one waiting request a priority; None = move it to the top.

        Higher priorities are granted first and FIFO holds inside a priority.
        The queue is processed right away, so a request moved above a queued
        sweep may be granted on the spot. Returns None for no such request.
        """
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return None
        if priority is None:
            others = [self._priority(q) for q in self._queue if q is not entry]
            priority = max(others + [self._priority(entry) - 1]) + 1
        entry["priority"] = int(priority)
        self._sort_queue()
        self._process_queue()
        self._save()
        granted = any(h["grant_id"] == grant_id for h in self._holders)
        position = (
            None
            if granted
            else next(i for i, q in enumerate(self._queue, 1) if q["grant_id"] == grant_id)
        )
        log.info(
            "window: operator set priority %d on %s (%s)",
            entry["priority"], grant_id, "granted" if granted else f"position {position}",
        )
        return {"priority": entry["priority"], "granted": granted, "position": position}

    def force(self, grant_id: str) -> Optional[dict]:
        """Grant one waiting request now, past every limit. None = no such request.

        The running holders keep running; the forced holder is added next to
        them and counts against the caps and the budget for everyone after.
        """
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return None
        self._queue.remove(entry)
        holder = self._grant(entry, forced=True)
        self._save()
        log.info("window: operator forced %s (%s)", grant_id, holder["cls"])
        return holder

    def status(self) -> dict:
        """The window as state anyone can read -- the whole point.

        Deliberately does not reap: status is a read, and a read with side
        effects is how a quiet ``status`` call ends up releasing somebody's
        grant. Reaping happens where grants are decided (``acquire``).
        """
        sweep_cap, targeted_cap = self._caps()
        return {
            "holders": list(self._holders),
            "queue": list(self._queue),
            "caps": {"sweep": sweep_cap, "targeted": targeted_cap},
            "max_wait": MAX_WAIT,
            "reminder_interval": REMINDER_INTERVAL,
            "cores": self._cores,
            # The width a targeted request made now would be granted.
            "advisory_n_now": self._width(TARGETED, None),
            "limits": dict(self._limits()),
            "workers_in_use": self.workers_in_use(),
        }

    # ---- the exit hook -------------------------------------------------------- #

    def session_exited(self, session) -> None:
        """The manager's exit funnel: a session that ends gives the window back.

        Same funnel, same contract as ``daemon/beads.py``'s hook: called
        synchronously in the loop, never awaited. A session whose pytest is
        mid-run when its terminal dies is exactly the stale-holder case this
        hook exists for -- the child pytest dying with its session is what
        makes releasing right, and if it somehow outlives the session the pid
        reaper at the next acquire finishes the job.
        """
        name = session.sdef.name
        held = [h for h in self._holders if h.get("session") == name]
        queued = [q for q in self._queue if q.get("session") == name]
        if not held and not queued:
            return
        self._holders = [h for h in self._holders if h.get("session") != name]
        self._queue = [q for q in self._queue if q.get("session") != name]
        self._process_queue()
        self._save()


def reminder_block(holder: dict) -> str:
    """The independent reminder sent while a session keeps a test grant."""
    grant_id = holder.get("grant_id") or "?"
    cls = holder.get("cls") or "unknown"
    label = holder.get("label") or "no label"
    acquired = holder.get("acquired_at") or "unknown time"
    return "\n".join(
        [
            "---",
            "# claunch window: release reminder -- machine-generated, not typed by "
            "the user; repeats every 3 minutes while this grant is held",
            f"grant: {grant_id} ({cls})",
            f"held since: {acquired}",
            f"label: {label}",
            "protocol: this test window is still held. If the test completed or "
            "failed, release it now with `claunch window release --grant-id "
            f"{grant_id}`. If it is still running, keep the grant and continue "
            "with its result.",
            "---",
        ]
    )


class WindowReminderClock:
    """Remind live session holders to release the measurement window.

    The clock has no authority to release a live holder: a process can still
    be running after its terminal stops producing output, and releasing such
    a grant would permit an overlapping measurement.  Session exit and PID
    reaping remain the release mechanisms.  This clock supplies the missing
    prompt after failed tests leave a live session holding a grant.
    """

    def __init__(
        self,
        manager,
        window: WindowManager,
        *,
        interval: float = REMINDER_INTERVAL,
        poll: float = REMINDER_POLL,
    ) -> None:
        self.manager = manager
        self.window = window
        self.interval = interval
        self.poll = poll
        self._seen: Dict[str, float] = {}
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
                for holder in self.scan(time.monotonic()):
                    await self._deliver(holder)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("window reminder clock tick failed")

    def scan(self, now: Optional[float] = None) -> List[dict]:
        """Return session-held grants whose next 3-minute reminder is due."""
        stamp = time.monotonic() if now is None else now
        holders = self.window.status().get("holders") or []
        live = set()
        due: List[dict] = []
        for holder in holders:
            grant_id = str(holder.get("grant_id") or "")
            session = holder.get("session")
            if not grant_id or not session:
                continue
            live.add(grant_id)
            armed_at = self._seen.setdefault(grant_id, stamp)
            if stamp - armed_at >= self.interval:
                due.append(holder)
        for grant_id in list(self._seen):
            if grant_id not in live:
                del self._seen[grant_id]
        return due

    async def _deliver(self, holder: dict) -> None:
        """Deliver one due reminder; only a successful delivery re-arms it."""
        session_name = str(holder.get("session") or "")
        grant_id = str(holder.get("grant_id") or "")
        if not session_name or not grant_id:
            return
        try:
            session = self.manager.get(session_name)
        except Exception:
            return
        if getattr(session, "exited", False):
            return
        try:
            delivered = await session.deliver(reminder_block(holder))
        except Exception:
            log.exception("window reminder delivery to %r failed", session_name)
            return
        if delivered:
            self._seen[grant_id] = time.monotonic()
            log.info("window release reminder delivered to %r (%s)", session_name, grant_id)
