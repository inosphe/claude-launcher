"""The staggered restore: bring a restart's sessions back one at a time.

A daemon start used to relaunch every session it owed in one burst, before it
listened. On 2026-10-13 that burst was 51 harness processes at once, and seven
codex sessions (s144 s287 s289 s291 s292 s294 s311) died inside it with
``account/read failed during TUI bootstrap: ... workspace routing discovery
timed out`` — filed as killed, their issues swept back to open (claunch-0j6gb).

So :meth:`SessionManager.restore_all` now files each session that would come
back with its conversation as *delayed* — a hold this queue owns
(``Session.delayed_at`` beside ``held_at``) — and this module relaunches them
:attr:`SessionManager.restore_stagger` seconds apart, in :func:`rank` order.

Being a hold is what makes "use it and it comes first" free: every path that
wakes a held record — a viewer attaching (the web terminal, ``claunch
attach``), a mesh message, a cflow notice, a checks request, a line typed to
it — wakes a delayed one the same way, ahead of the queue. The queue skips a
name that is no longer delayed when its turn comes, and does not wait the
interval for it.

One shot per daemon start, like the resume nudge: the list is fixed at
restore, and the task ends when it has walked it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

from .manager import RESTORE_QUEUE, ManagerError

log = logging.getLogger(__name__)

#: ``role_of(name) -> roles`` — every mesh role the session holds on this
#: daemon (:meth:`MeshManager.meshes_for_session`); empty when unknown.
RolesOf = Callable[[str], Sequence[str]]


def _stamp(value: Optional[str]) -> float:
    """An ISO timestamp as epoch seconds; 0 for a missing or bad one."""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def rank(manager, names: Iterable[str], *, roles_of: Optional[RolesOf] = None,
         busy: Iterable[str] = ()) -> List[str]:
    """``names`` in the order the queue starts them.

    Highest first, each key breaking the previous one's ties:

    1. a mesh leader — the session that hands out and lands everyone
       else's work;
    2. mid-turn when the previous daemon went down (``busy``) — the restart
       cut a turn short, and every interval it waits is that work standing
       still (claunch-z6c4b: a worker mid-landing came back 41st, behind 40
       idle parents, 4m48s after the restart);
    3. a parent, more descendants first — its children answer to it; this
       orders the busy ones among themselves as well as the idle ones;
    4. most recent output — the nearest thing to "was working" for an idle
       record.

    Registry order breaks what is left, so the order is stable.
    """
    names = list(names)
    working = set(busy)

    def leader(name: str) -> bool:
        if roles_of is None:
            return False
        try:
            return "leader" in [str(r).lower() for r in roles_of(name) or ()]
        except Exception:  # noqa: BLE001 -- an unreadable roster ranks nobody
            log.exception("restore queue: roles of %r unreadable", name)
            return False

    def descendants(name: str) -> int:
        try:
            return len(manager.descendants(name))
        except Exception:  # noqa: BLE001
            return 0

    def output(name: str) -> float:
        try:
            return _stamp(manager.get(name).last_output_at)
        except ManagerError:
            return 0.0

    keyed = [
        ((not leader(n), n not in working, -descendants(n), -output(n), i), n)
        for i, n in enumerate(names)
    ]
    return [n for _, n in sorted(keyed)]


def _delayed(manager, name: str) -> bool:
    try:
        session = manager.get(name)
    except ManagerError:
        return False
    return bool(session.exited and getattr(session, "delayed_at", None))


class RestoreQueue:
    """Wakes :attr:`SessionManager.restore_queue` in :func:`rank` order."""

    def __init__(self, manager, *, interval: Optional[float] = None,
                 roles_of: Optional[RolesOf] = None,
                 names: Optional[Iterable[str]] = None) -> None:
        self.manager = manager
        #: The delayed names this queue wakes: the boot's
        #: :attr:`SessionManager.restore_queue` (read at :meth:`start`) unless
        #: given -- a host relaunch passes its own pass's (claunch-hor3b.1).
        self._names: Optional[List[str]] = None if names is None else list(names)
        self.interval = (manager.restore_stagger if interval is None
                         else max(0.0, float(interval)))
        self.roles_of = roles_of
        #: Fixed at :meth:`start`: ranking reads the mesh, which is up by then.
        self.order: List[str] = []
        #: Names this queue started (the rest were woken by use first, or
        #: had gone) — the tests' handle on the outcome.
        self.started: List[str] = []
        #: Names :meth:`_run` has gone past, by any way. A name past is not
        #: waiting here even if it is delayed again later -- a host relaunch
        #: files it delayed for its own queue to start, and this one never
        #: comes back to it (claunch-hor3b.2).
        self._passed: Set[str] = set()
        #: True once :meth:`_run` has walked its whole list.
        self.finished = False
        #: Monotonic time of :meth:`start` and of the queue's last relaunch:
        #: what :meth:`status` paces its estimate by. The configured interval
        #: is a floor, not the pace -- a relaunch itself takes seconds, and on
        #: 2026-10-14 84 sessions 3s apart took about 7 minutes.
        self._began: Optional[float] = None
        self._last_start: Optional[float] = None
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        names = self.manager.restore_queue if self._names is None else self._names
        if self._task is not None or not names:
            return
        self.order = rank(
            self.manager, names,
            roles_of=self.roles_of, busy=self.manager.resumed_busy,
        )
        log.info(
            "restore queue: %d session(s) delayed, %.1fs apart: %s",
            len(self.order), self.interval, ", ".join(self.order),
        )
        self._began = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._run())

    # ---- progress, for the dashboard (claunch-v4uyt) ----------------------- #

    def waiting(self, without: Iterable[str] = ()) -> List[str]:
        """The ranked names still delayed that this queue will still start,
        next first; ``without`` are names another queue counts."""
        without = set(without)
        return [n for n in self.order
                if n not in self._passed and n not in without
                and _delayed(self.manager, n)]

    def pace(self, now: Optional[float] = None) -> float:
        """Seconds per relaunch as measured so far; the interval until the
        first one lands."""
        now = time.monotonic() if now is None else now
        if self._began is None or not self.started or self._last_start is None:
            return self.interval
        return max(self.interval, (self._last_start - self._began) / len(self.started))

    def status(self, now: Optional[float] = None,
               without: Iterable[str] = ()) -> Optional[dict]:
        """The queue as a whole: ``None`` once nothing is left in it.

        ``total`` is the boot's count, ``done`` how many have left the queue
        by any way (started by it, woken by use, killed), ``remaining`` the
        rest, ``next`` the one it starts next, ``eta_seconds`` an estimate of
        when the last one starts at the pace measured so far. Names in
        ``without`` are another queue's to count and drop out of all of
        these (:class:`Queues`)."""
        without = set(without)
        order = [n for n in self.order if n not in without]
        if not order:
            return None
        now = time.monotonic() if now is None else now
        waiting = self.waiting(without)
        if not waiting:
            return None
        pace = self.pace(now)
        return {
            "total": len(order),
            "done": len(order) - len(waiting),
            "started_by_queue": len(self.started),
            "remaining": len(waiting),
            "next": waiting[0],
            "interval": self.interval,
            "pace_seconds": round(pace, 1),
            "eta_seconds": round(self._eta(len(waiting) - 1, pace, now)),
        }

    def positions(self, now: Optional[float] = None,
                  without: Iterable[str] = ()) -> Dict[str, dict]:
        """Every still-delayed name's place: ``rank`` in the boot's order out
        of ``total``, how many still wait ``ahead`` of it, and an estimate of
        when it starts (``eta_seconds``); ``without`` as in :meth:`status`."""
        now = time.monotonic() if now is None else now
        without = set(without)
        order = [n for n in self.order if n not in without]
        waiting = self.waiting(without)
        pace = self.pace(now)
        rank_of = {n: i + 1 for i, n in enumerate(order)}
        return {
            name: {
                "rank": rank_of[name],
                "total": len(order),
                "ahead": ahead,
                "eta_seconds": round(self._eta(ahead, pace, now)),
            }
            for ahead, name in enumerate(waiting)
        }

    def _eta(self, ahead: int, pace: float, now: float) -> float:
        since = now - (self._last_start or self._began or now)
        return max(0.0, (ahead + 1) * pace - since)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        first = True
        for name in self.order:
            try:
                if not _delayed(self.manager, name):
                    continue  # woken by use, killed or removed: not ours any more
                if not first and self.interval:
                    await asyncio.sleep(self.interval)
                    if not _delayed(self.manager, name):
                        continue
                first = False
                try:
                    await self.manager.wake_async(name, by=RESTORE_QUEUE)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- one must not stop the rest
                    log.exception("restore queue: relaunch of %r failed", name)
                    with contextlib.suppress(ManagerError):
                        self.manager.fail_delayed(name, reason=str(exc))
                    continue
                self.started.append(name)
                self._last_start = time.monotonic()
            finally:
                self._passed.add(name)
        self.finished = True
        log.info("restore queue: done (%d started by the queue)", len(self.started))


class Queues:
    """Several :class:`RestoreQueue` runs read as one, for the dashboard.

    A host relaunch starts a queue of its own (claunch-hor3b.1) while the
    boot's may still be walking its list; putting the new one in the boot
    queue's place hid the boot's progress until the relaunch's was done
    (claunch-hor3b.2). This answers :meth:`RestoreQueue.status` and
    :meth:`RestoreQueue.positions` over every queue that still has names
    waiting, so neither is lost.

    A name can wait in two queues at once: woken by use before the boot's
    queue reached it, then filed delayed again by a host loss. It is counted
    once, in the first queue that holds it; later queues leave it out of
    their totals and places (s511-rev)."""

    def __init__(self, *queues: RestoreQueue) -> None:
        self.queues: List[RestoreQueue] = list(queues)

    def add(self, queue: RestoreQueue) -> None:
        """Join ``queue``; queues that have walked their whole list leave,
        so a long-lived daemon's host relaunches do not pile up here."""
        self.queues = [q for q in self.queues if not getattr(q, "finished", False)]
        self.queues.append(queue)

    def status(self, now: Optional[float] = None) -> Optional[dict]:
        """The live queues summed; ``next``, ``interval`` and
        ``pace_seconds`` are the first live queue's, ``eta_seconds`` the
        latest of theirs. ``None`` once none has anything left."""
        live = []
        counted: Set[str] = set()
        for queue in self.queues:
            status = queue.status(now, without=counted)
            counted.update(queue.waiting())
            if status is not None:
                live.append(status)
        if not live:
            return None
        if len(live) == 1:
            return live[0]
        out = dict(live[0])
        for key in ("total", "done", "started_by_queue", "remaining"):
            out[key] = sum(s[key] for s in live)
        out["eta_seconds"] = max(s["eta_seconds"] for s in live)
        return out

    def positions(self, now: Optional[float] = None) -> Dict[str, dict]:
        """Each still-delayed name's place in the queue that holds it."""
        out: Dict[str, dict] = {}
        for queue in self.queues:
            out.update(queue.positions(now, without=out))
        return out
