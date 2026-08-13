"""The clock behind a delegated decision's ``timeout``.

Everything else in cflow happens because somebody called a tool: the agent
advances, a human approves, a responder answers. An expiry has nobody — the
one agent that would notice is the one stopped waiting for the answer. So the
daemon carries it, scanning the same machine-local run registry the dashboard
lists runs from and escalating whatever has gone past its deadline.

Two consequences worth stating plainly, because both are deliberate:

**Without a daemon, a timeout simply does not fire.** The question keeps
waiting for its responder, exactly as a human gate has always waited. That is
the safe direction: the alternative — a run that proceeds because nobody was
watching the clock — is the one thing a delegated approval must never do.

**The tick runs off the event loop.** Escalating re-reads the roster and
re-delivers, and those go through this daemon's own HTTP API; doing that from
the loop would have it waiting on a request only it can serve. A worker
thread keeps the self-call honest, and keeps a slow filesystem off the loop
besides.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import List, Optional

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
