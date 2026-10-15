"""Archive killed sessions once they have stayed killed long enough
(claunch-wz6a5).

A killed record stays in the working fleet until someone archives it by hand,
so the rail and every fleet query keep paying for sessions nobody will
resume. This clock files them away on its own: every
``auto_archive_interval_s`` seconds it looks for records whose partition is
``killed`` (:func:`session.session_category` -- paused, held, delayed and
failed records are not killed and are left alone) and whose ending is at
least ``auto_archive_killed_days`` days old, and archives at most
``auto_archive_batch`` of them, oldest ending first.

The batch cap is what makes the backlog safe to turn on: records that were
killed long before this clock existed are archived a batch per tick rather
than all at once, so a fleet with hundreds of old endings drains over a few
ticks instead of in one persist storm. Archive is :meth:`SessionManager.archive`
-- reversible through respawn, idempotent -- so a tick that races a person's
own archive or respawn loses nothing.

All three keys are read from the config file on every tick, so a change takes
effect on the next tick without a daemon restart. ``auto_archive_killed_days``
or ``auto_archive_batch`` at 0 switches the sweep off; the clock keeps
ticking so turning it back on needs no restart either.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from aiohttp import web

from .. import store
from .session import CATEGORY_KILLED, session_category

log = logging.getLogger(__name__)

#: Defaults; the live values are ``daemon.auto_archive_*`` in the config.
DAYS = 7.0
INTERVAL = 600.0
BATCH = 20

#: Seconds between config re-reads while the interval is 0 (clock off).
IDLE_RECHECK = 60.0

#: Reason written on the archive event, so a person reading the session's
#: history can tell this clock's archive from their own.
REASON = "auto-archive"


def _number(cfg: dict, key: str, default: float) -> float:
    try:
        value = float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(value, 0.0)


def settings(cfg: Optional[dict] = None) -> Tuple[float, float, int]:
    """``(days, interval_s, batch)`` from the daemon config, read now."""
    cfg = store.daemon_config() if cfg is None else cfg
    return (
        _number(cfg, "auto_archive_killed_days", DAYS),
        _number(cfg, "auto_archive_interval_s", INTERVAL),
        int(_number(cfg, "auto_archive_batch", BATCH)),
    )


def _parse(stamp) -> Optional[datetime]:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def ended_at(session) -> Optional[datetime]:
    """When ``session`` ended. ``exited_at`` is the stamp; a record that
    lacks it (written before the field was kept) falls back to its last
    output, then its creation, both of which are no later than the end."""
    for field in ("exited_at", "last_output_at", "created_at"):
        when = _parse(getattr(session, field, None))
        if when is not None:
            return when
    return None


def due(sessions: Iterable, *, days: float, now: datetime) -> List[str]:
    """Names of the killed records ended at least ``days`` ago, oldest
    ending first."""
    cutoff = now - timedelta(days=days)
    found: List[Tuple[datetime, str]] = []
    for session in sessions:
        if session_category(session) != CATEGORY_KILLED:
            continue
        when = ended_at(session)
        if when is None or when > cutoff:
            continue
        found.append((when, session.sdef.name))
    found.sort()
    return [name for _, name in found]


def sweep_once(manager, *, days: float, batch: int,
               now: Optional[datetime] = None) -> dict:
    """Archive up to ``batch`` due records. Returns what it did and how many
    due records are left for later ticks."""
    if days <= 0 or batch <= 0:
        return {"archived": [], "failed": [], "remaining": 0}
    now = now or datetime.now(timezone.utc)
    names = due(list(manager.list()), days=days, now=now)
    archived: List[str] = []
    failed: List[dict] = []
    for name in names[:batch]:
        try:
            manager.archive(name, reason=REASON)
        except Exception as exc:  # one bad record must not stop the rest
            failed.append({"name": name, "error": str(exc)})
        else:
            archived.append(name)
    return {
        "archived": archived,
        "failed": failed,
        "remaining": max(len(names) - len(archived) - len(failed), 0),
    }


class AutoArchiver:
    def __init__(self, manager) -> None:
        self.manager = manager
        self._task: Optional[asyncio.Task] = None
        #: The last tick's result plus its time, for a reader that asks.
        self.last: Optional[dict] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="auto-archive"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def tick(self, now: Optional[datetime] = None) -> dict:
        days, _, batch = settings()
        result = sweep_once(self.manager, days=days, batch=batch, now=now)
        self.last = {**result, "at": (now or datetime.now(timezone.utc))
                     .isoformat(timespec="seconds")}
        if result["archived"] or result["failed"]:
            log.info(
                "auto-archive: archived %d killed session(s) ended >= %g days "
                "ago (%d failed, %d left for later ticks): %s",
                len(result["archived"]), days, len(result["failed"]),
                result["remaining"], ", ".join(result["archived"]),
            )
        return result

    async def _run(self) -> None:
        while True:
            _, interval, _ = settings()
            if interval <= 0:
                await asyncio.sleep(IDLE_RECHECK)
                continue
            # Wait first: a daemon that just came back has its own work to
            # do, and nothing here is urgent.
            await asyncio.sleep(interval)
            try:
                self.tick()
            except Exception:
                log.exception("auto-archive tick failed")


async def _start(app: web.Application) -> None:
    app["auto_archive"].start()


async def _stop(app: web.Application) -> None:
    await app["auto_archive"].stop()


def install(app: web.Application, manager) -> AutoArchiver:
    archiver = AutoArchiver(manager)
    app["auto_archive"] = archiver
    app.on_startup.append(_start)
    app.on_shutdown.append(_stop)
    return archiver
