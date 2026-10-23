"""Report timers an agent sets for itself, kept by the daemon (claunch-lvrcp).

An agent has no clock. Its turn ends and nothing starts the next one; a
"report every ten minutes" it tries to keep itself is a promise only the
daemon can hold, because the process that made it is not the process that
would have to wake up. This module is that clock.

A timer is *(every seconds, times, message)*: every ``every`` seconds, up to
``times`` times, the daemon types ``message`` into the session that made it.
Three properties follow from the timer living here rather than in the MCP
process that created it:

* **It outlives the session's turns.** The MCP server, the harness and the
  PTY may all be gone and back; the ledger is a file under the session's own
  directory and the clock is the daemon's.
* **A daemon that is down does not count.** Nothing can fire while the daemon
  is stopped or restarting, so that interval is *paused*, not missed: a
  heartbeat beside the daemon's state records the last pass, and the next
  boot moves every ``next_due`` forward by the downtime
  (:func:`resume_after_pause`). Without it a timer that had two minutes left
  when the daemon stopped would fire the moment it came back.
* **The session's own ending ends it.** A kill (or a crash, or an archive)
  deletes its ledger (:func:`session_ended`). A *hold* or a *pause* is not an
  ending -- the session is meant to come back -- so the timer keeps running
  and its fire wakes the session (:meth:`TimerClock._deliver`).

The session's timer line (``daemon/session_timers.py``) shows a running timer
through the ``agent`` provider, and the ``timer`` MCP tool
(:mod:`claude_launcher.timer_mcp`) is the only door that creates one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import paths, session_timers
from .session import delivery_wake

log = logging.getLogger("claunch.daemon.agent-timers")

#: Seconds between the clock's passes. Short enough that a report is never
#: late by more than a few seconds, long enough that the ledger scan and the
#: heartbeat write are noise.
POLL = 15.0

#: The shortest interval a timer may ask for. A report timer exists to make
#: an agent speak on a schedule; anything under this is a busy loop against a
#: terminal, and the harness's own ``wait`` tool already bounds at 30s.
MIN_EVERY = 10.0
#: The longest interval, a month. Longer is a reminder nobody will read.
MAX_EVERY = 30 * 24 * 3600.0
#: A timer fires at most this many times; ``times`` is what the agent asked
#: for, and a bounded timer needs no cancel tool to be safe.
MAX_TIMES = 1000
#: Longest message a timer may carry. The delivery is typed into a terminal,
#: and a paste longer than this is a document, not a report prompt.
MAX_MESSAGE = 4000
#: How many live timers one session may hold. Ten agents that all mean to
#: report every minute should not be able to fill their own terminal with
#: interleaved prompts by accident.
MAX_ACTIVE = 20

FILENAME = "timers.json"

#: Serialises the ledger read-modify-write between the request handler (on
#: the event loop) and the clock's scan (in a worker thread).
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #
def ledger_file(session: str) -> Path:
    return paths.session_dir(session) / FILENAME


def _read(session: str) -> dict:
    path = ledger_file(session)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("agent timers: cannot read %s: %s", path, exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def _write(session: str, doc: dict) -> None:
    path = ledger_file(session)
    if not doc.get("timers"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load(session: str) -> List[dict]:
    """The session's timers, oldest first. Read-only callers may keep them."""
    rows = _read(session).get("timers")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save(session: str, rows: Iterable[dict]) -> None:
    doc = _read(session)
    doc["timers"] = list(rows)
    _write(session, doc)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{name}' must be a number")
    out = float(value)
    if out != out:  # NaN
        raise ValueError(f"'{name}' must be a number")
    return out


def add(session: str, *, every: Any, times: Any, message: Any,
        now: Optional[float] = None, by: str = "") -> dict:
    """Create a timer and return it. Raises ``ValueError`` on a bad ask.

    The first fire is one interval away, not immediate: the caller has just
    given the message and typing it back at once would be a round trip, not
    a schedule.
    """
    every_s = _number(every, "every")
    if not MIN_EVERY <= every_s <= MAX_EVERY:
        raise ValueError(
            f"'every' must be between {MIN_EVERY:g} and {MAX_EVERY:g} seconds"
        )
    times_i = _number(times, "times")
    if times_i != int(times_i) or not 1 <= int(times_i) <= MAX_TIMES:
        raise ValueError(f"'times' must be a whole number from 1 to {MAX_TIMES}")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("'message' must be a non-empty string")
    message = message.strip()
    if len(message) > MAX_MESSAGE:
        raise ValueError(f"'message' must be at most {MAX_MESSAGE} characters")

    now = time.time() if now is None else float(now)
    with _LOCK:
        rows = load(session)
        if len(rows) >= MAX_ACTIVE:
            raise ValueError(
                f"this session already has {len(rows)} timers (the limit is "
                f"{MAX_ACTIVE}); let one finish before adding another"
            )
        record = {
            "id": f"t-{uuid.uuid4().hex[:8]}",
            "every": every_s,
            "times": int(times_i),
            "fires": 0,
            "message": message,
            "created_at": now,
            "next_due": now + every_s,
            "created_by": by or session,
        }
        rows.append(record)
        _save(session, rows)
    log.info("agent timer %s for %r: every %gs x%d", record["id"], session,
             every_s, int(times_i))
    return dict(record)


def remove_session(session: str) -> bool:
    """Forget every timer of ``session`` (its real ending)."""
    with _LOCK:
        path = ledger_file(session)
        if not path.exists():
            return False
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return True


def session_names() -> List[str]:
    """Sessions that have a timer ledger right now."""
    root = paths.sessions_root()
    try:
        return sorted(p.parent.name for p in root.glob(f"*/{FILENAME}"))
    except OSError as exc:  # an unreadable directory hides only itself
        log.warning("agent timers: cannot scan %s: %s", root, exc)
        return []


# --------------------------------------------------------------------------- #
# pause across a daemon that was not running
# --------------------------------------------------------------------------- #
def heartbeat_file() -> Path:
    return paths.daemon_dir() / "agent-timers.json"


def _beat(now: float) -> None:
    """Record that the clock was alive at ``now``."""
    path = heartbeat_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"checked_at": now}), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # the ledger still works; only resume accounting is lost
        log.warning("agent timers: cannot write heartbeat %s: %s", path, exc)


def _last_beat() -> Optional[float]:
    try:
        doc = json.loads(heartbeat_file().read_text(encoding="utf-8"))
        value = doc.get("checked_at")
        return float(value) if value is not None else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        log.warning("agent timers: cannot read heartbeat: %s", exc)
        return None


def resume_after_pause(now: Optional[float] = None) -> float:
    """Move every timer's ``next_due`` past the daemon's downtime.

    Returns the paused seconds (0 when there was no heartbeat: no timer has
    been created yet, or the last daemon never ran a pass). The interval the
    daemon was down does not count toward any timer, which is what "paused
    while the daemon is not running" means in practice.
    """
    now = time.time() if now is None else float(now)
    last = _last_beat()
    if last is None:
        return 0.0
    paused = now - last
    if paused <= 0:
        return 0.0
    shifted = 0
    with _LOCK:
        for name in session_names():
            rows = load(name)
            if not rows:
                continue
            for row in rows:
                row["next_due"] = float(row.get("next_due") or now) + paused
            _save(name, rows)
            shifted += len(rows)
        # The boot's own moment is alive: recording it here makes a second
        # call in the same boot a no-op instead of a second shift.
        _beat(now)
    log.info("agent timers: daemon was down %.0fs; %d timer(s) resumed", paused,
             shifted)
    return paused


# --------------------------------------------------------------------------- #
# the session's timer line
# --------------------------------------------------------------------------- #
def _preview(message: str, limit: int = 40) -> str:
    text = " ".join(message.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def strip_timers(session: str) -> List[dict]:
    """The ``agent`` provider's answer for the terminal's timer line."""
    out: List[dict] = []
    for row in load(session):
        fires = int(row.get("fires") or 0)
        times = int(row.get("times") or 0)
        every = float(row.get("every") or 0)
        out.append(session_timers.timer(
            "agent", "timer", row.get("id", ""),
            _preview(str(row.get("message") or "")),
            row.get("next_due"),
            since=row.get("created_at"),
            state=session_timers.STATE_WAITING,
            detail=(f"fire {fires + 1}/{times} · every {every:g}s · report timer"),
        ))
    return [t for t in out if t]


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
def fire_block(record: dict) -> str:
    """What the agent reads when a timer fires.

    Framed the way every daemon-typed message is: it lands in a terminal that
    may have been idle for an hour, and an unframed line reads as the user
    speaking. It names the timer and says what to do with the message.
    """
    fires = int(record.get("fires") or 0)
    times = int(record.get("times") or 0)
    every = float(record.get("every") or 0)
    last = fires >= times
    count = f"{fires}/{times}" + (" (last)" if last else "")
    tail = (
        "protocol: the report timer you set with the 'timer' tool has finished "
        "its last fire. Do what the message asks, then carry on with your work "
        "-- nothing else will fire from this timer."
        if last else
        "protocol: the report timer you set with the 'timer' tool fired. Do "
        "what the message asks now; the next fire is one interval away."
    )
    return "\n".join([
        "---",
        "# claunch timer: scheduled message -- machine-generated, not typed by "
        "the user",
        f"timer: {record.get('id')} (fire {count}, every {every:g}s)",
        f"message: {record.get('message')}",
        tail,
        "---",
    ])


class TimerClock:
    """Fires due report timers and types their message into the session.

    The pass runs in a worker thread (it reads and writes ledgers); the
    delivery runs on the event loop, because it types into PTYs.
    """

    def __init__(self, manager, *, poll: float = POLL) -> None:
        self.manager = manager
        self.poll = poll
        self._task: Optional[asyncio.Task] = None
        #: The pause the last boot resumed, for readers (the tests, a log line).
        self.resumed_pause = 0.0

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        if self._task is not None:
            return
        self.resumed_pause = resume_after_pause()
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
                groups = await asyncio.to_thread(self.scan)
                for name, records in groups:
                    await self._deliver(name, records)
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad pass must not stop the clock
                log.exception("agent timer clock pass failed")

    # -- the pass ----------------------------------------------------------- #
    def scan(self, now: Optional[float] = None) -> List[Tuple[str, List[dict]]]:
        """Fire every due timer on the machine; blocking, so call it in a
        thread. Returns ``(session, fired records)`` per session that is due.

        A fired timer's next fire is one interval from *now*, not from its
        old due time: a clock that was blocked for longer than an interval
        would otherwise fire a burst to catch up, and a report timer's job is
        to speak on a cadence, not to deliver a backlog.
        """
        now = time.time() if now is None else float(now)
        due: List[Tuple[str, List[dict]]] = []
        with _LOCK:
            names = session_names()
            for name in names:
                rows = load(name)
                if not rows:
                    continue
                fired: List[dict] = []
                kept: List[dict] = []
                for row in rows:
                    try:
                        due_at = float(row.get("next_due"))
                    except (TypeError, ValueError):
                        continue  # a malformed row cannot be scheduled
                    if due_at > now:
                        kept.append(row)
                        continue
                    row["fires"] = int(row.get("fires") or 0) + 1
                    row["last_fired"] = now
                    every = float(row.get("every") or MIN_EVERY)
                    row["next_due"] = now + every
                    fired.append(dict(row))
                    if int(row["fires"]) < int(row.get("times") or 0):
                        kept.append(row)
                    else:
                        log.info("agent timer %s for %r finished (%d/%d)",
                                 row.get("id"), name, row["fires"],
                                 row.get("times"))
                if fired:
                    _save(name, kept)
                    due.append((name, fired))
            if names:
                _beat(now)
        return due

    # -- delivery ----------------------------------------------------------- #
    async def _deliver(self, name: str, records: List[dict]) -> None:
        session = await self._wake(name)
        if session is None:
            log.info(
                "agent timer fired for %r, but the session has no process to "
                "type into; leaving the timer for the next pass", name)
            return
        for record in records:
            try:
                with delivery_wake("timer"):
                    accepted = session.queue_delivery(fire_block(record))
            except Exception:  # noqa: BLE001 - one message must not stop the rest
                log.exception("agent timer delivery to %r failed", name)
                continue
            log.info("agent timer %s delivered to %r (accepted=%s)",
                     record.get("id"), name, accepted)

    async def _wake(self, name: str):
        """The session to type into, waking a held or paused one.

        ``None`` means there is nobody to type into: a killed record, or a
        name the registry does not know. A hold or a pause is not an ending
        (the timer deliberately outlives both), so those are woken, and the
        wake is the daemon's -- ``resumed_by_human`` stays False, exactly as
        for the other clocks.
        """
        try:
            session = self.manager.get(name)
        except Exception:  # ManagerError: the registry does not know this name
            return None
        if not session.exited:
            return session
        if getattr(session, "held_at", None):
            # queue_delivery's own wake path takes it from here.
            return session
        if not getattr(session, "paused_at", None):
            return None  # killed, failed or archived: the timer should be gone
        try:
            relaunched = await self.manager.respawn_async(name)
        except Exception:  # noqa: BLE001 - a failed relaunch is not fatal here
            log.exception("agent timer could not wake paused session %r", name)
            return None
        relaunched.resumed_by_human = False
        log.info("agent timer woke paused session %r", name)
        return relaunched


def session_ended(session) -> None:
    """Exit hook: a session's real ending drops its timers.

    A *hold* never reaches the exit hooks at all (``manager._session_exited``
    returns before them), and a *pause* reaches them with ``paused_at`` set.
    Both are states the timer is meant to survive; everything else -- a kill,
    a crash, an archive -- is the end of the session the timer belonged to.
    """
    if getattr(session, "held_at", None) or getattr(session, "paused_at", None):
        return
    with contextlib.suppress(Exception):
        if remove_session(session.sdef.name):
            log.info("agent timers of %r removed with the session",
                     session.sdef.name)
