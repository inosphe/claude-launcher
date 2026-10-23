"""Read every profile's subscription usage once per period (claunch-f5w4x.3).

Each (profile, provider) key has its own timer: it is due when
:data:`profile_usage.PERIOD_SECONDS` have passed since that key's last
provider read, whoever asked for it -- this clock, a page's automatic read,
or a person's Refresh. Every ``TICK`` seconds the clock asks
:func:`profile_usage.due` which keys those are and reads them through the
same automatic path the page uses, so a key the page read a moment ago is
answered from the cache and costs nothing. A read that succeeds becomes one
history sample (:mod:`usage_history`); one that fails keeps its error in the
cache until the next period.

``daemon.usage_refresh`` false switches the reads off; it is read on every
tick, so turning it back on needs no restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Optional, Set

from aiohttp import web

from .. import profile_usage, store, usage
from . import pools

log = logging.getLogger(__name__)

#: Seconds between two looks at the timers.
TICK = 30.0
#: Seconds the clock waits after the daemon starts before its first look.
START_DELAY = 30.0
#: Account reads at once; a codex read starts a subprocess.
PARALLEL = 3


def enabled(cfg: Optional[dict] = None) -> bool:
    cfg = store.daemon_config() if cfg is None else cfg
    return bool(cfg.get("usage_refresh", True))


class UsageClock:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        #: Keys whose read is in flight, so a slow read is not started twice.
        self.reading: Set[str] = set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="usage-clock"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def tick(self) -> list:
        """Read the keys that are due; returns the keys it read."""
        if not enabled():
            return []
        rows = await pools.background(profile_usage.targets)
        rows = [row for row in profile_usage.due(rows) if row["key"] not in self.reading]
        gate = asyncio.Semaphore(PARALLEL)

        async def read(row: dict) -> None:
            async with gate:
                self.reading.add(row["key"])
                try:
                    await pools.background(profile_usage.fetch, row["profile"])
                except usage.UsageError as exc:
                    log.info("usage clock: %s: %s", row["key"], exc)
                finally:
                    self.reading.discard(row["key"])

        await asyncio.gather(*(read(row) for row in rows))
        return [row["key"] for row in rows]

    async def _run(self) -> None:
        await asyncio.sleep(START_DELAY)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("usage clock tick failed")
            await asyncio.sleep(TICK)


async def _start(app: web.Application) -> None:
    app["usage_clock"].start()


async def _stop(app: web.Application) -> None:
    await app["usage_clock"].stop()


def install(app: web.Application) -> UsageClock:
    clock = UsageClock()
    app["usage_clock"] = clock
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
    return clock
