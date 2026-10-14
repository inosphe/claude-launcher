"""The idle hold: free the program of a session nobody is using (claunch-5l35i).

A session that has finished its work and sits at its prompt for hours still
holds a harness process -- node, its memory, its MCP servers -- for nothing.
This clock ends that process and files the record as *held*
(:meth:`SessionManager.hold`); the session is otherwise untouched (its issue,
its mesh row and its runs stay as they were, see ``Session.held_at``) and
comes back with its conversation on the first use:

* a viewer attaches -- the web terminal or ``claunch attach``
  (``daemon/ws.py``, ``daemon/channel.py``);
* a mesh message is due for it (``MeshManager._deliver_to``);
* a cflow v2 notice is typed for it (``Cflow2Clock._deliver``).

A wake that does not happen on its own is still one click away: a held record
is an exited one, so the respawn button and ``claunch respawn`` bring it back
like any other.

Who is held, all of it measured on the live object every pass:

* idle (not busy, not starting), and quiet for ``hold_idle_minutes``
  (:meth:`Session.quiet_for`: no screen change, keystroke or visit);
* no viewer attached and nothing queued to be typed into it;
* not ``keep_alive``;
* :meth:`SessionManager.resumable` -- a respawn reopens its conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable, List, Optional

from .. import store
from . import actors
from .session import STATUS_IDLE

log = logging.getLogger(__name__)

#: Seconds between passes. The threshold is in minutes, so a minute of slack
#: on top of it is noise.
INTERVAL = 60.0


def threshold_seconds(cfg: Optional[dict] = None) -> float:
    """The configured quiet threshold in seconds; 0 means off."""
    cfg = store.daemon_config() if cfg is None else cfg
    try:
        minutes = float(cfg.get("hold_idle_minutes") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, minutes * 60.0)


def eligible(manager, session, threshold: float, now: Optional[float] = None) -> bool:
    """Whether ``session`` should be held now (see the module docstring)."""
    if threshold <= 0 or session.exited or getattr(session, "held_at", None):
        return False
    if getattr(session, "kill_requested", False):
        return False
    if getattr(session.sdef, "keep_alive", False):
        return False
    if session.status() != STATUS_IDLE:
        return False
    if session.viewers() or session.pending_deliveries():
        return False
    if not manager.resumable(session):
        return False
    return session.quiet_for(now) >= threshold


def sweep(manager, threshold: float, now: Optional[float] = None) -> List[str]:
    """Hold every eligible session; the names held."""
    held: List[str] = []
    if threshold <= 0:
        return held
    for session in list(manager.list()):
        try:
            if not eligible(manager, session, threshold, now):
                continue
            manager.hold(session.sdef.name, reason="idle",
                         by=actors.make(actors.DAEMON))
        except Exception:  # noqa: BLE001 -- one session must not stop the pass
            log.exception("idle hold of %r failed", session.sdef.name)
            continue
        held.append(session.sdef.name)
    if held:
        log.info("idle hold: %s", ", ".join(held))
    return held


class HoldClock:
    """Runs :func:`sweep` every :data:`INTERVAL` seconds."""

    def __init__(
        self,
        manager,
        *,
        interval: float = INTERVAL,
        config: Callable[[], dict] = store.daemon_config,
    ) -> None:
        self.manager = manager
        self.interval = interval
        self.config = config
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                sweep(self.manager, threshold_seconds(self.config()),
                      time.monotonic())
            except Exception:  # noqa: BLE001 -- the clock outlives a bad pass
                log.exception("idle hold pass failed")
