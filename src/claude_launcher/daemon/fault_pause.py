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

A multi-key profile gets one chance to avoid the pause: :mod:`key_switch`
runs first in each pass and, for a session that asked for it, relaunches it
on another usable key. What this module pauses is what that switch could not
rescue.

Who is paused, measured on the live object every pass:

* ``fault_auto_pause`` is on in the daemon config;
* not exited, not being ended;
* idle -- a busy screen is a session still working, whatever its last fault
  reading says (the detector reads the screen, and a false positive is
  possible; see :mod:`session_fault`);
* its fault record is active and of a kind in :data:`KINDS`.

A session that was just brought back -- respawned or woken out of a pause,
a hold or a kill, or restarted on another key or borrowed profile
(:attr:`Session.resumed_at`, claunch-x9agn) -- is left alone for
:data:`GRACE` seconds (``daemon.fault_resume_grace``). Its screen can still
show the limit line the previous incarnation stopped on (a resumed
conversation is painted back), and read at once that line paused the session
again before the new key or profile had a chance to answer. The grace is
drawn on the session's timer line (:func:`grace_timer`) so a person can see
when the pause may come back.

A session this clock has paused once for a fault is not paused again for
the same fault (the same reset deadline): a person who resumes it before the
reset has decided to, and the clock does not undo that. That memory outlives
the daemon that made it: the pair is in :data:`RECORD` as well, so a restart
-- which empties the in-memory map -- does not pause again a session a person
resumed while the same limit was still on its screen (claunch-ijot0.1).

Nothing resumes the session at the reset: whether to spend the new plan on it
is the person's decision. What the daemon can do is say when that decision
is due. A pause writes the fault it was made for into the session directory
(:data:`RECORD`), because a daemon restart turns the record into one that
no longer carries its screen; :func:`resets` lists the sessions still paused
for it, both those whose reset time has passed (``due``) and those still
waiting for it, and the dashboard draws a card for each
(``GET /api/fault-resets``). Only a reset the harness *stated* on the screen
(``until_stated``) is told: a window guessed from the first sighting says
nothing about when the plan comes back, and the harnesses differ in whether
they print one at all.

The record is also the only thing left that can answer "why did this stop?"
for a paused session. The pause kills the process, so the live reading
(:mod:`session_fault`, taken off the screen) dies with it, and before
claunch-9djny.1 a paused session answered ``fault: None`` for the days
between the pause and its reset. :func:`paused_fault` reads the record back
into the same view shape while it still belongs to this pause and its
deadline has not passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from aiohttp import web

from .. import store
from . import actors, paths, session_fault
from .session import STATUS_IDLE

log = logging.getLogger(__name__)

#: Seconds between passes. The fault scanner reads a screen once a second;
#: a usage limit lasts hours, so half a minute of slack is noise.
INTERVAL = 30.0

#: The fault kinds that pause a session. Only an exhausted plan: it lasts
#: until a reset hours or days away, while a rate limit, a network outage or
#: an API status error clears on its own within its window.
KINDS = ("usage_limit",)

#: Seconds after a resume during which a session is not paused (nor
#: key-switched) for a fault (claunch-x9agn). ``daemon.fault_resume_grace``
#: overrides it; 0 turns it off.
GRACE = 30.0

#: The config key for :data:`GRACE`.
GRACE_KEY = "fault_resume_grace"

#: The reason recorded on the pause (``Session.ended_by``).
REASON = "usage-limit"

#: The file in the session directory holding the fault a pause was made for.
RECORD = "fault-pause.json"

#: The fault fields kept in :data:`RECORD`. They are the fields the view
#: answers with, so a record read back after a restart says the same thing
#: about the stop as the live reading did. ``action``, ``resolved_by`` and
#: ``code`` were added for that (claunch-9djny.1); a record written before
#: them simply carries none, and :func:`pause_fault_view` fills what the
#: catalogue can still say.
RECORD_FIELDS = (
    "kind", "label", "harness", "message", "reset", "until", "until_stated",
    "action", "resolved_by", "code",
)

#: The record field naming the fault the pause was made for: the ``until``
#: the live reading carried, empty when the harness stated none. It is the
#: key :func:`sweep` keeps per session, so a daemon that restarts can tell it
#: already paused this session for this fault (claunch-ijot0.1). Not part of
#: :data:`RECORD_FIELDS`: it is the clock's own bookkeeping and the pause
#: view never shows it.
KEY_FIELD = "pause_key"

#: What a reading taken from :data:`RECORD` says in ``source``, so a reader
#: can tell it apart from one taken off a screen (``session_fault``).
SOURCE = "pause-record"


def pause_key(record: dict) -> str:
    """The identity of the fault a pause is made for: its stated ``until``.

    The same key the clock keeps in memory, written to the session directory
    too. A fault with no stated deadline keys on ``""`` -- the in-memory map
    has always held that for it, so a restart matches it the same way rather
    than inventing an identity the live reading would not repeat.
    """
    return str(record.get("until") or "")


def enabled(cfg: Optional[dict] = None) -> bool:
    """Whether the automatic pause is on (``fault_auto_pause``)."""
    cfg = store.daemon_config() if cfg is None else cfg
    return bool(cfg.get("fault_auto_pause", True))


def resume_grace(cfg: Optional[dict] = None) -> float:
    """The grace after a resume in seconds (``fault_resume_grace``), >= 0."""
    cfg = store.daemon_config() if cfg is None else cfg
    try:
        value = float(cfg.get(GRACE_KEY, GRACE))
    except (TypeError, ValueError):
        return GRACE
    return value if value > 0 and value == value else 0.0


def grace_until(session, grace: float) -> Optional[float]:
    """When ``session``'s resume grace ends (epoch seconds), or None.

    None when the session was not brought back by a resume this daemon saw
    (:attr:`Session.resumed_at` is unset: a fresh session, a restore after a
    daemon restart), when it has exited, or when the grace is off.
    """
    if grace <= 0 or getattr(session, "exited", False):
        return None
    resumed = getattr(session, "resumed_at", None)
    if not isinstance(resumed, (int, float)) or isinstance(resumed, bool):
        return None
    return float(resumed) + grace


def in_grace(session, grace: float, now: Optional[float] = None) -> bool:
    """Whether ``session`` is inside its resume grace now."""
    until = grace_until(session, grace)
    if until is None:
        return False
    return (time.time() if now is None else now) < until


def fault_to_pause(
    session, *, grace: Optional[float] = None, now: Optional[float] = None
) -> Optional[dict]:
    """The active fault that pauses ``session`` now, or None.

    ``grace`` is the resume grace in seconds (:func:`resume_grace` when
    omitted); a session inside it is not paused yet, whatever its reading.
    """
    if session.exited or getattr(session, "kill_requested", False):
        return None
    if in_grace(session, resume_grace() if grace is None else grace, now):
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


def sweep(
    manager, paused: Dict[str, str], *, grace: Optional[float] = None
) -> List[str]:
    """Pause every session :func:`fault_to_pause` names; the names paused.

    ``paused`` maps a session to the key of the fault it was paused for
    (:func:`pause_key`), kept across passes so a resumed session is not
    paused again for the same fault. It dies with the daemon, so the record
    on disk answers the same question for a pass that runs after a restart
    (:func:`paused_before`); a match there is remembered in ``paused`` too,
    so the file is read once per session, not once per pass.
    """
    grace = resume_grace() if grace is None else grace
    out: List[str] = []
    for session in list(manager.list()):
        name = session.sdef.name
        try:
            record = fault_to_pause(session, grace=grace)
            if record is None:
                continue
            key = pause_key(record)
            if paused.get(name) == key:
                continue
            if paused_before(name, key):
                paused[name] = key
                continue
            manager.pause(name, by=actors.make(actors.DAEMON, reason=REASON))
        except Exception:  # noqa: BLE001 -- one session must not stop the pass
            log.exception("fault pause of %r failed", name)
            continue
        # The record carries the pause's OWN ``paused_at``, not a fresh
        # reading of the clock: :func:`paused_fault` answers only a record
        # whose stamp is the session's, and two separate ``now()`` calls can
        # land on either side of a second boundary.
        write_record(name, record, paused_at=getattr(session, "paused_at", None))
        paused[name] = key
        out.append(name)
    if out:
        log.info("fault pause (usage limit): %s", ", ".join(out))
    return out


def paused_before(name: str, key: str) -> bool:
    """Whether this clock already paused ``name`` for fault ``key`` before.

    Answered from :data:`RECORD`, so it survives a daemon restart. The
    session is a live candidate here -- exited and ending ones are filtered
    by :func:`fault_to_pause` -- so a record for this fault means a person
    resumed the session while the limit was still on its screen, and the
    clock leaves that decision alone.

    A record written before :data:`KEY_FIELD` existed has no key; its
    ``until`` stands in only when the harness stated it, because a guessed
    one is the pause's own invention and never equals a live ``until``.

    A name reused by a later session can still find an older record; it has
    to name the same deadline to match, and the cost of a false match is one
    pause not made, not a wrong stop.
    """
    record = read_record(name)
    if not record:
        return False
    stored = record.get(KEY_FIELD)
    if stored is None:
        return bool(record.get("until_stated")) and str(record.get("until") or "") == key
    return str(stored) == key


def kind_window(kind: str) -> float:
    """The catalogue's window for ``kind`` in seconds (0 when it is unknown)."""
    try:
        spec = session_fault.default().kinds.get(kind)
    except Exception:  # noqa: BLE001 -- a broken catalogue caps nothing
        return 0.0
    return float(spec.window) if spec is not None else 0.0


def write_record(name: str, record: dict, *, paused_at: Optional[str] = None) -> None:
    """Keep the fault a pause was made for (:data:`RECORD`).

    ``paused_at`` is the pause's own stamp on the session record; without it
    the record takes the current time, which is what a caller outside
    :func:`sweep` has. A record whose ``until`` cannot be read is capped with
    its kind's window measured from the pause (usage_limit: 86400s) and is
    marked as a guess, so a session paused for a stop with no readable
    deadline still comes off the rail instead of staying on it forever.
    """
    body = {k: record.get(k) for k in RECORD_FIELDS if k in record}
    stamp = paused_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    body["paused_at"] = stamp
    # The key the pause was made for, from the live reading -- before the
    # capping below invents an ``until`` this fault never carried.
    body[KEY_FIELD] = pause_key(record)
    if _parse(body.get("until")) is None:
        window = kind_window(str(body.get("kind") or ""))
        paused = _parse(stamp)
        if window and paused is not None:
            body["until"] = (paused + timedelta(seconds=window)).isoformat(timespec="seconds")
            body["until_stated"] = False
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


def _iso(at: Optional[datetime]) -> str:
    return at.isoformat(timespec="seconds") if at is not None else ""


def pause_fault_view(record: dict, now: Optional[datetime] = None) -> dict:
    """The view shape of :meth:`session_fault.Detector.view`, from a record.

    The record holds what the pause was made for, not a screen: ``since`` and
    ``last_seen`` are the pause (the last moment anything was read), and
    ``strikes`` is 1 because a pause is one sighting. ``active`` uses the
    same expression the live reading does, so the rail, the fleet route and
    the reset list all agree on when the stop is over.
    """
    now = datetime.now(timezone.utc) if now is None else now
    paused = _parse(record.get("paused_at"))
    until = _parse(record.get("until"))
    kind = str(record.get("kind") or "")
    window = kind_window(kind)
    if until is None and paused is not None and window:
        # A record with no readable deadline still cannot last longer than
        # its kind's window, measured from the pause (write_record does the
        # same when it can; this covers a file written by an older daemon).
        until = paused + timedelta(seconds=window)
    spec = None
    try:
        spec = session_fault.default().kinds.get(kind)
    except Exception:  # noqa: BLE001 -- a broken catalogue costs a label, not the row
        spec = None
    stamp = _iso(paused)
    return {
        "harness": str(record.get("harness") or ""),
        "kind": kind,
        "label": str(record.get("label") or (spec.label if spec else kind)),
        "action": str(record.get("action") or (spec.action if spec else "")),
        "resolved_by": str(
            record.get("resolved_by") or (spec.resolved_by if spec else "")
        ),
        "message": str(record.get("message") or ""),
        "code": str(record.get("code") or ""),
        "reset": str(record.get("reset") or ""),
        "since": stamp,
        "last_seen": stamp,
        "until": _iso(until),
        "until_stated": bool(record.get("until_stated")),
        "cleared": "",
        "strikes": 1,
        "active": until is not None and now < until,
        "source": SOURCE,
    }


def paused_fault(session) -> Optional[dict]:
    """The fault a pause by this clock was made for, read from the directory.

    A paused session has no screen left to scan, so the live reading dies
    with the process. This answers in its place, and only while all of these
    hold -- anything else is not this pause's fault:

    * the record carries this clock's ending (``ended_by`` reason
      :data:`REASON`) and a ``paused_at``;
    * it is not archived -- a filed-away record is inert, and its pill would
      name a stop nobody is being asked about any more;
    * the record file is *this* pause's: its ``paused_at`` equals the
      session's. A name reused by a later session has its own stamp, and an
      older file on disk must not answer for it. Unpausing a record clears
      ``paused_at``, which fails the first test and takes the fault with it,
      so a person who resumes a session stops seeing the pause they undid.
    """
    paused_at = getattr(session, "paused_at", None)
    if not paused_at:
        return None
    if getattr(session, "archived_at", None):
        return None
    ender = getattr(session, "ended_by", None) or {}
    if ender.get("kind") != actors.DAEMON or ender.get("reason") != REASON:
        return None
    record = read_record(session.sdef.name)
    if not record:
        return None
    if _parse(record.get("paused_at")) != _parse(paused_at):
        return None
    return pause_fault_view(record)


def resets(
    manager, now: Optional[datetime] = None, *, include_pending: bool = False
) -> List[dict]:
    """The sessions this clock paused for a fault with a stated reset.

    A row needs the session still paused (``paused_at``) by this clock
    (``ended_by`` reason :data:`REASON`) and a :data:`RECORD` whose
    ``until`` was stated by the harness. A resumed, killed or person-paused
    session, or a fault without a stated reset, contributes nothing.

    Each row says whether the reset has passed (``due``). By default only
    the passed ones are returned; ``include_pending`` adds the ones still
    waiting, which is what the dashboard draws a card for before the reset
    (claunch-9djny.1: a person could not tell a session the daemon had
    stopped from one that had died until the reset was over).
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
        if until is None:
            continue
        due = until <= now
        if not due and not include_pending:
            continue
        out.append({
            "session": name,
            "paused_at": session.paused_at,
            "due": due,
            "fault": {k: record.get(k) for k in RECORD_FIELDS if k in record},
        })
    out.sort(key=lambda r: r["session"])
    return out


#: The ``session_timers`` source and kind of the resume grace chip.
TIMER_SOURCE = "fault"
TIMER_KIND = "grace"


def grace_timer(session, cfg: Optional[dict] = None,
                now: Optional[float] = None) -> Optional[dict]:
    """The timer-line chip for ``session``'s resume grace, or None.

    Shown while the grace runs and the automatic pause is on (with the pause
    off there is nothing for the grace to hold back).
    """
    from . import session_timers

    cfg = store.daemon_config() if cfg is None else cfg
    if not enabled(cfg):
        return None
    grace = resume_grace(cfg)
    if not in_grace(session, grace, now):
        return None
    until = grace_until(session, grace)
    return session_timers.timer(
        TIMER_SOURCE, TIMER_KIND, "resume-grace", "usage-limit grace", until,
        since=getattr(session, "resumed_at", None),
        detail=(
            f"resumed: no usage-limit pause or key switch for {grace:g}s "
            f"(daemon.{GRACE_KEY}); the pause pass after that may pause it "
            "again if the limit is still on its screen"
        ),
    )


def install(app) -> None:
    async def h_fault_resets(request: web.Request) -> web.Response:
        rows = resets(request.app["manager"], include_pending=True)
        due = [r for r in rows if r["due"]]
        pending = [r for r in rows if not r["due"]]
        return web.json_response(
            {
                "resets": due,
                "pending": pending,
                "count": len(due),
                "pending_count": len(pending),
            },
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
        cfg = self.config()
        if not enabled(cfg):
            return []
        return sweep(self.manager, self.paused, grace=resume_grace(cfg))

    async def async_pass_once(self) -> List[str]:
        """One pass: switch keys first, then pause what is left.

        The switch (:mod:`key_switch`) runs inside this pass rather than in a
        clock of its own so the two cannot race: a session that was just
        relaunched onto another key reads no fault any more, so :func:`sweep`
        below skips it without needing to know the switch happened. A separate
        clock would leave a window where the pass paused the very session the
        switch was about to restart.
        """
        cfg = self.config()
        if not enabled(cfg):
            return []
        from . import key_switch

        await key_switch.sweep(self.manager, cfg)
        return sweep(self.manager, self.paused, grace=resume_grace(cfg))

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.async_pass_once()
            except Exception:  # noqa: BLE001 -- the clock outlives a bad pass
                log.exception("fault pause pass failed")
