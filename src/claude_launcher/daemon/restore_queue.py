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
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Sequence

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
                 roles_of: Optional[RolesOf] = None) -> None:
        self.manager = manager
        self.interval = (manager.restore_stagger if interval is None
                         else max(0.0, float(interval)))
        self.roles_of = roles_of
        #: Fixed at :meth:`start`: ranking reads the mesh, which is up by then.
        self.order: List[str] = []
        #: Names this queue started (the rest were woken by use first, or
        #: had gone) — the tests' handle on the outcome.
        self.started: List[str] = []
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is not None or not self.manager.restore_queue:
            return
        self.order = rank(
            self.manager, self.manager.restore_queue,
            roles_of=self.roles_of, busy=self.manager.resumed_busy,
        )
        log.info(
            "restore queue: %d session(s) delayed, %.1fs apart: %s",
            len(self.order), self.interval, ", ".join(self.order),
        )
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        first = True
        for name in self.order:
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
        log.info("restore queue: done (%d started by the queue)", len(self.started))
