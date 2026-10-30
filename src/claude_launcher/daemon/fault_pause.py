"""The fault pause: pause a session the harness stopped on a usage limit (claunch-ijot0).

A session under an exhausted usage plan (:mod:`session_fault`, kind
``usage_limit``) cannot do anything until the plan resets -- often days
away. Left running it still takes typed input: every mesh message and cflow
notice is answered with the same "You've hit your weekly limit" line, and a
leader in that state keeps the dashboard's "leader stopped" card up
(:mod:`leader_fault`). This clock pauses such a session
(:meth:`SessionManager.pause`, recorded as the daemon's with reason
``usage-limit``), which ends that and takes the card down: a paused session
was stopped on purpose, and :data:`leader_fault.ALERT_CATEGORIES` leaves it
out.

Who is paused, measured on the live object every pass:

* ``fault_auto_pause`` is on in the daemon config;
* not exited, not being ended;
* idle -- a busy screen is a session still working, whatever its last fault
  reading says (the detector reads the screen, and a false positive is
  possible; see :mod:`session_fault`);
* its fault record is active and of a kind in :data:`KINDS`.

A session this clock has paused once for a fault is not paused again for
the same fault (the same reset deadline): a person who resumes it before the
reset has decided to, and the clock does not undo that.

Nothing resumes the session at the reset: whether to spend the new plan on it
is the person's decision. What the daemon can do is say when that decision
is due. A pause writes the fault it was made for into the session directory
(:data:`RECORD`), because a daemon restart turns the record into one that
no longer carries its screen; :func:`resets` lists the sessions still paused
for it whose reset time has passed, and the dashboard raises a card for each
(``GET /api/fault-resets``). Only a reset the harness *stated* on the screen
(``until_stated``) is told: a window guessed from the first sighting says
nothing about when the plan comes back, and the harnesses differ in whether
they print one at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from aiohttp import web

from .. import store
from . import actors, paths
from .session import STATUS_IDLE

log = logging.getLogger(__name__)

#: Seconds between passes. The fault scanner reads a screen once a second;
#: a usage limit lasts hours, so half a minute of slack is noise.
INTERVAL = 30.0

#: The fault kinds that pause a session. Only an exhausted plan: it lasts
#: until a reset hours or days away, while a rate limit, a network outage or
#: an API status error clears on its own within its window.
KINDS = ("usage_limit",)

#: The reason recorded on the pause (``Session.ended_by``).
REASON = "usage-limit"

#: The file in the session directory holding the fault a pause was made for.
RECORD = "fault-pause.json"

#: The fault fields kept in :data:`RECORD`.
RECORD_FIELDS = ("kind", "label", "harness", "message", "reset", "until", "until_stated")


def enabled(cfg: Optional[dict] = None) -> bool:
    """Whether the automatic pause is on (``fault_auto_pause``)."""
    cfg = store.daemon_config() if cfg is None else cfg
    return bool(cfg.get("fault_auto_pause", True))


def fault_to_pause(session) -> Optional[dict]:
    """The active fault that pauses ``session`` now, or None."""
    if session.exited or getattr(session, "kill_requested", False):
        return None
    try:
        record = session.fault()
    except Exception:  # noqa: BLE001 -- a broken reading pauses nothing
        return None
    if not record or not record.get("active") or record.get("kind") not in KINDS:
        return None
    if session.status() != STATUS_IDLE:
        return None
    return record


def sweep(manager, paused: Dict[str, str]) -> List[str]:
    """Pause every session :func:`fault_to_pause` names; the names paused.

    ``paused`` maps a session to the ``until`` of the fault it was paused
    for, kept across passes so a resumed session is not paused again for
    the same fault.
    """
    out: List[str] = []
    for session in list(manager.list()):
        name = session.sdef.name
        try:
            record = fault_to_pause(session)
            if record is None:
                continue
            until = str(record.get("until") or "")
            if paused.get(name) == until:
                continue
            manager.pause(name, by=actors.make(actors.DAEMON, reason=REASON))
        except Exception:  # noqa: BLE001 -- one session must not stop the pass
            log.exception("fault pause of %r failed", name)
            continue
        write_record(name, record)
        paused[name] = until
        out.append(name)
    if out:
        log.info("fault pause (usage limit): %s", ", ".join(out))
    return out


def write_record(name: str, record: dict) -> None:
    """Keep the fault a pause was made for (:data:`RECORD`)."""
    body = {k: record.get(k) for k in RECORD_FIELDS if k in record}
    body["paused_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        target = paths.session_dir(name) / RECORD
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("fault pause: could not record the fault of %r", name, exc_info=True)


def read_record(name: str) -> Optional[dict]:
    try:
        data = json.loads((paths.session_dir(name) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse(value) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def resets(manager, now: Optional[datetime] = None) -> List[dict]:
    """The sessions this clock paused whose stated reset time has passed.

    A row needs the session still paused (``paused_at``) by this clock
    (``ended_by`` reason :data:`REASON`) and a :data:`RECORD` whose
    ``until`` was stated by the harness and is not in the future. A resumed,
    killed or person-paused session, or a fault without a stated reset,
    contributes nothing.
    """
    now = datetime.now(timezone.utc) if now is None else now
    out: List[dict] = []
    for session in list(manager.list()):
        name = session.sdef.name
        if not getattr(session, "paused_at", None):
            continue
        if getattr(session, "archived_at", None):
            continue
        ender = getattr(session, "ended_by", None) or {}
        if ender.get("kind") != actors.DAEMON or ender.get("reason") != REASON:
            continue
        record = read_record(name)
        if not record or not record.get("until_stated"):
            continue
        until = _parse(record.get("until"))
        if until is None or until > now:
            continue
        out.append({
            "session": name,
            "paused_at": session.paused_at,
            "fault": {k: record.get(k) for k in RECORD_FIELDS if k in record},
        })
    out.sort(key=lambda r: r["session"])
    return out


def install(app) -> None:
    async def h_fault_resets(request: web.Request) -> web.Response:
        rows = resets(request.app["manager"])
        return web.json_response(
            {"resets": rows, "count": len(rows)},
            dumps=lambda d: json.dumps(d, ensure_ascii=False),
        )

    app.router.add_get("/api/fault-resets", h_fault_resets)


class FaultPauseClock:
    """Runs :func:`sweep` every :data:`INTERVAL` seconds while enabled."""

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
        self.paused: Dict[str, str] = {}
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

    def pass_once(self) -> List[str]:
        if not enabled(self.config()):
            return []
        return sweep(self.manager, self.paused)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                self.pass_once()
            except Exception:  # noqa: BLE001 -- the clock outlives a bad pass
                log.exception("fault pause pass failed")
