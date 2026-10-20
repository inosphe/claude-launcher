"""The approval gate an agent's request for a session waits behind
(claunch-iw5i8.15, widened by claunch-iw5i8.16).

Creating a session is the person's action (``claunch new-session`` is refused
inside a managed session), so an agent's ask for one goes through this gate:
the web UI shows it as a notification card with a countdown, and the person
approves, rejects, extends the deadline or takes it over by hand.

Who files one: an agent's child spawn (MCP ``spawn`` or ``claunch spawn`` from
a managed session, except a ``reviewer`` child), an agent's ask for a
top-level session (``spawn`` with ``root``), and the operator v2 agent route
(:mod:`operators_v2`), whose request must name the user message it acts on
and is a top-level ask.

Two kinds of request:

``spawn`` -- create a new session. ``scope`` says which:

* ``root`` -- a top-level session from ``spec`` (a ``POST /api/sessions``
  body); ``child_spec`` is the same session as a child of the asker (a
  ``POST /api/sessions/{asker}/children`` body), the fallback.
* ``child`` -- a child of the asker from ``spec`` (a children body).

* **Approved** -- the session is created as asked (``create``). A create
  that fails settles the request as ``failed`` with the error.
* **Manual** -- the person opens the New session form (root) or the spawn
  child form (child) seeded with ``spec`` and creates it themselves (or does
  not). The request is settled; the form's create reports the session it
  made back here (:meth:`link`) so the asker learns its name.
* **Rejected** -- nothing is created.
* **Unanswered** -- after :data:`GATE_TIMEOUT` (plus every extension) a
  child request counts as approved (th-d9e343), and a root request creates
  no top-level session: its ``child_spec`` is created as the asker's child
  instead (claunch-iw5i8.16).

``setup`` on a settled spawn says who set the session up: ``agent`` (nobody
answered; created from the agent's spec as it asked), ``approved`` (the
person approved the agent's spec), ``custom`` (the person made it in the
form). ``created_as`` says whether it was made ``root`` or ``child``.

``resume`` -- bring an exited session back on its own conversation.

* **Approved** -- the target is respawned (``manager.respawn_async``).
* **Spawn instead** -- the target stays exited and a ``spawn`` request is
  filed from the request's ``fallback`` spec.
* **Rejected** -- nothing is relaunched and nothing is created.
* **Unanswered** -- ``expired``: the target is not resumed, and a ``spawn``
  request is filed from ``fallback`` (which then waits behind its own
  deadline). The user's decision: a resume nobody approved does not happen.
* **Moot** -- the target is running again by some other way.

Both kinds take **Extend** (the deadline moves out by :data:`EXTENSION`, as
many times as pressed) and **Withdrawn** (only the asker).

Every settlement is typed into the asking session's terminal (``notify``)
and handed to ``on_settle`` (the operator's feed). Pending requests are kept
in ``spawn-requests.json`` so a daemon restart does not drop the question;
their timers are re-armed when the daemon's loop starts (:meth:`start`), and
a deadline that passed while the daemon was down gets :data:`BOOT_GRACE`
seconds so the person can see the card before the clock answers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from .. import atomic

log = logging.getLogger(__name__)

FILE = "spawn-requests.json"

#: How long an unanswered request waits for a person (the user's 5 minutes).
GATE_TIMEOUT = 300.0
#: How much one Extend adds to a pending request's deadline.
EXTENSION = 300.0
#: The least time a request re-armed at daemon start is given.
BOOT_GRACE = 60.0

#: Settled records kept for the asker and the notification history to read.
KEEP_SETTLED = 40
#: Pending requests one asker may hold at once.
MAX_PENDING_PER_ASKER = 10
REASON_MAX = 4000

SPAWN = "spawn"
RESUME = "resume"
KINDS = (SPAWN, RESUME)

#: Where a spawn request's session goes: top-level, or under the asker.
ROOT = "root"
CHILD = "child"
SCOPES = (ROOT, CHILD)

#: Who set a created session up (``setup`` on a settled spawn).
SETUP_AGENT = "agent"
SETUP_APPROVED = "approved"
SETUP_CUSTOM = "custom"

PENDING = "pending"
CREATING = "creating"
RESUMING = "resuming"
APPROVED = "approved"
MANUAL = "manual"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
FAILED = "failed"
EXPIRED = "expired"
SPAWNED = "spawned"
MOOT = "moot"

#: States in which the request still holds the gate.
OPEN = (PENDING, CREATING, RESUMING)

#: ``(spec, asker, record) -> (status, payload)``: the session-create path.
#: ``record["created_as"]`` says which: ``root`` (``spec`` is a
#: ``POST /api/sessions`` body) or ``child`` (a children body, under the
#: asker); ``record["setup"]`` says who set it up.
Create = Callable[[dict, str, dict], Awaitable[Tuple[int, dict]]]


class GateRefused(Exception):
    """The request cannot be filed or settled as asked (HTTP 400/403/409)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: Optional[datetime] = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


def _parse(stamp) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def clean_reason(text) -> str:
    reason = str(text or "").strip()
    if len(reason) > REASON_MAX:
        reason = reason[:REASON_MAX].rstrip() + " ...(truncated)"
    return reason


def outcome_message(record: dict) -> str:
    """The block typed into the asking session when its request settles."""
    rid = record.get("id") or "?"
    status = record.get("status")
    by = record.get("decided_by") or "?"
    head = f"[claunch spawn-request {rid}]"
    if record.get("kind") == RESUME:
        target = record.get("target") or "?"
        follow = record.get("followup")
        if status == APPROVED:
            body = (f"approved: session {target} was resumed (by {by}). It knows "
                    "nothing of your request -- message it with the work.")
        elif status == FAILED:
            body = f"approved, but the resume of {target} failed: {record.get('error') or 'unknown error'}"
        elif status in (EXPIRED, SPAWNED):
            why = "nobody answered in time" if status == EXPIRED else f"{by} chose a new session"
            body = (f"{target} stays exited ({why}); "
                    + (f"spawn request {follow} was filed from your fallback." if follow
                       else "no fallback was given, so nothing was created."))
        elif status == REJECTED:
            body = f"rejected: {target} stays exited and nothing is created (by {by})."
        elif status == MOOT:
            body = f"closed: {record.get('note') or f'session {target} is running again'}"
        else:
            body = f"{status}: session {target}"
        return f"{head} {body}"
    created = record.get("created") or "?"
    if status == APPROVED and record.get("setup") == SETUP_AGENT:
        if record.get("scope") == ROOT:
            body = (f"nobody answered: no top-level session was made; session {created} "
                    "was created as your child from your spec instead. It starts on the "
                    "task you gave.")
        else:
            body = (f"nobody answered: session {created} was created as your child from "
                    "your spec. It starts on the task you gave.")
    elif status == APPROVED:
        where = "as your child" if record.get("created_as") == CHILD else "top-level"
        body = (f"approved (by {by}): session {created} was created {where}. "
                "It starts on the task you gave; follow it from your poll.")
    elif status == MANUAL:
        form = "spawn child form" if record.get("scope") == CHILD else "New session form"
        body = (f"the person created session {record['created']} by hand from your request."
                if record.get("created") else
                f"taken over by hand ({by}): the person is creating the session in the "
                f"{form}. You are told its name if they create it.")
    elif status == REJECTED:
        body = f"rejected (by {by}): nothing was created. Do not ask again for the same work."
    elif status == FAILED:
        body = f"approved, but the create failed: {record.get('error') or 'unknown error'}"
    else:
        body = f"{status}"
    return f"{head} {body}"


class SpawnGate:
    """The spawn and resume requests of one daemon, pending and settled.

    Everything runs on the daemon's event loop; the awaits (create, respawn)
    are guarded by the transient ``creating``/``resuming`` states, so a
    second press, or the timer firing during one, finds nothing pending.
    """

    def __init__(
        self,
        *,
        manager,
        path: Optional[Path] = None,
        create: Optional[Create] = None,
        notify: Optional[Callable[[str, str], bool]] = None,
        on_settle: Optional[Callable[[dict], None]] = None,
        timeout: float = GATE_TIMEOUT,
    ) -> None:
        self.manager = manager
        self.path = Path(path) if path is not None else None
        self._create = create
        self._notify = notify
        #: Called with every settled record (the operator's feed line).
        self.on_settle = on_settle
        self.timeout = timeout
        self.records: Dict[str, dict] = {}
        self._timers: Dict[str, asyncio.TimerHandle] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("unreadable %s; starting with no spawn requests", self.path)
            return
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            # A create or respawn the previous daemon was in the middle of
            # did not finish with it: the question goes back to the person.
            if row.get("status") in (CREATING, RESUMING):
                row["status"] = PENDING
            self.records[str(row["id"])] = row

    def _save(self) -> None:
        if self.path is None:
            return
        self._trim()
        body = json.dumps(list(self.records.values()), ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self.path) as tmp:
                tmp.write_text(body, encoding="utf-8", newline="\n")
                atomic.replace(tmp, self.path)
        except OSError:
            log.exception("could not write %s", self.path)

    def _trim(self) -> None:
        settled = [r for r in self.records.values() if r.get("status") not in OPEN]
        settled.sort(key=lambda r: r.get("decided_at") or r.get("requested_at") or "")
        for row in settled[:-KEEP_SETTLED] if len(settled) > KEEP_SETTLED else []:
            self.records.pop(row["id"], None)

    # ------------------------------------------------------------------ #
    # the clock
    # ------------------------------------------------------------------ #
    async def start(self, _app=None) -> None:
        """Arm the timers of the pending requests read from disk (aiohttp
        ``on_startup``: the loop is running from here on)."""
        floor = _now() + timedelta(seconds=BOOT_GRACE)
        changed = False
        for row in self.records.values():
            if row.get("status") != PENDING:
                continue
            deadline = _parse(row.get("deadline"))
            if deadline is None or deadline < floor:
                row["deadline"] = _stamp(floor)
                changed = True
            self._arm(row)
        if changed:
            self._save()

    async def cancel(self, _app=None) -> None:
        """The daemon is going down: free the timers. The records stay on
        disk and are re-armed by the next daemon's :meth:`start`."""
        for rid in list(self._timers):
            self._clear(rid)

    def _arm(self, row: dict) -> None:
        self._clear(row["id"])
        deadline = _parse(row.get("deadline")) or _now()
        delay = max(0.0, (deadline - _now()).total_seconds())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet: start() arms it
        self._timers[row["id"]] = loop.call_later(delay, self._fire, row["id"])

    def _clear(self, rid: str) -> None:
        timer = self._timers.pop(rid, None)
        if timer is not None:
            timer.cancel()

    def _fire(self, rid: str) -> None:
        self._timers.pop(rid, None)
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return
        asyncio.ensure_future(self.expire(rid))

    async def expire(self, rid: str) -> Optional[dict]:
        """The deadline passed with no answer: a child spawn is created as
        asked, a root spawn is created as the asker's child instead, and a
        resume request expires into its fallback spawn."""
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return None
        if row.get("kind") == RESUME:
            return self._resume_to_spawn(row, EXPIRED, "timeout")
        self._clear(rid)
        return await self._create_from(row, "timeout", as_child=True, setup=SETUP_AGENT)

    # ------------------------------------------------------------------ #
    # the ask
    # ------------------------------------------------------------------ #
    def _check_asker(self, session: str) -> None:
        if not session:
            raise GateRefused(400, "session (the asking session) is required")
        held = sum(1 for r in self.records.values()
                   if r.get("session") == session and r.get("status") in OPEN)
        if held >= MAX_PENDING_PER_ASKER:
            raise GateRefused(
                409, f"{session} already has {held} pending spawn requests; "
                "wait for the person to answer them")

    def _record(self, kind: str, session: str, reason: str, basis: Optional[dict]) -> dict:
        now = _now()
        return {
            "id": secrets.token_hex(6),
            "kind": kind,
            "session": session,
            "reason": reason,
            "basis": dict(basis or {}),
            "requested_at": _stamp(now),
            "deadline": _stamp(now + timedelta(seconds=self.timeout)),
            "extensions": 0,
            "status": PENDING,
            "decided_by": None,
            "decided_at": None,
        }

    def submit_spawn(self, *, session: str, spec: dict, reason: str,
                     basis: Optional[dict] = None, origin: Optional[str] = None,
                     scope: str = ROOT, child_spec: Optional[dict] = None) -> dict:
        """File a request to create a session from ``spec``: top-level
        (``scope`` root; ``child_spec``, default ``spec``, is what an
        unanswered request creates under the asker instead) or as the
        asker's child (``scope`` child)."""
        session = str(session or "").strip()
        reason = clean_reason(reason)
        self._check_asker(session)
        if scope not in SCOPES:
            raise GateRefused(400, f"scope must be one of {', '.join(SCOPES)}")
        if not isinstance(spec, dict) or not spec:
            raise GateRefused(400, "spec (the session to create) is required")
        if child_spec is not None and not isinstance(child_spec, dict):
            raise GateRefused(400, "child_spec must be a session spec")
        if not reason:
            raise GateRefused(400, "reason is required: the person decides on it alone")
        record = self._record(SPAWN, session, reason, basis)
        record["scope"] = scope
        record["spec"] = dict(spec)
        if scope == ROOT:
            record["child_spec"] = dict(child_spec or spec)
        if origin:
            record["origin"] = origin
        self.records[record["id"]] = record
        self._save()
        self._arm(record)
        return dict(record)

    def _target(self, name: str):
        try:
            return self.manager.get(name)
        except Exception:  # noqa: BLE001 -- ManagerError: no such record
            return None

    def submit_resume(self, *, session: str, target: str, reason: str,
                      fallback: Optional[dict] = None,
                      fallback_child: Optional[dict] = None,
                      basis: Optional[dict] = None) -> dict:
        """File a request to resume the exited ``target``; ``fallback`` is
        the (root) spawn spec filed when the person declines or does not
        answer, ``fallback_child`` that spawn's ``child_spec``."""
        session = str(session or "").strip()
        target = str(target or "").strip()
        reason = clean_reason(reason)
        self._check_asker(session)
        if not target:
            raise GateRefused(400, "target (the exited session to resume) is required")
        if not reason:
            raise GateRefused(400, "reason is required: the person decides on it alone")
        if fallback is not None and not isinstance(fallback, dict):
            raise GateRefused(400, "fallback must be a session spec")
        found = self._target(target)
        if found is None:
            raise GateRefused(400, f"no session named {target!r}")
        if not found.exited:
            raise GateRefused(409, f"session {target!r} is running -- message it instead")
        if getattr(found, "held_at", None):
            raise GateRefused(409, f"session {target!r} is held: it wakes when it is "
                              "sent a message, so message it instead")
        self.reconcile()
        for row in self.records.values():
            if (row.get("kind") == RESUME and row.get("target") == target
                    and row.get("status") in OPEN):
                raise GateRefused(409, f"a resume request for {target} is already "
                                  f"pending ({row['id']})")
        record = self._record(RESUME, session, reason, basis)
        record["target"] = target
        record["target_cwd"] = getattr(getattr(found, "sdef", None), "cwd", "") or ""
        record["fallback"] = dict(fallback) if fallback else None
        if fallback and fallback_child:
            record["fallback_child"] = dict(fallback_child)
        self.records[record["id"]] = record
        self._save()
        self._arm(record)
        return dict(record)

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def reconcile(self) -> None:
        """Close the pending resume requests whose target came back."""
        changed = []
        for row in self.records.values():
            if row.get("kind") != RESUME or row.get("status") != PENDING:
                continue
            found = self._target(row.get("target") or "")
            if found is not None and not found.exited:
                self._clear(row["id"])
                self._mark(row, MOOT, "daemon")
                row["note"] = f"session {row.get('target')} is running again"
                changed.append(row)
        for row in changed:
            self._tell(row)
        if changed:
            self._save()

    def list(self) -> List[dict]:
        self.reconcile()
        rows = sorted(self.records.values(),
                      key=lambda r: (r.get("status") not in OPEN, r.get("requested_at") or ""))
        return [dict(r) for r in rows]

    def get(self, rid: str) -> Optional[dict]:
        self.reconcile()
        row = self.records.get(rid)
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # the settlement
    # ------------------------------------------------------------------ #
    def _pending(self, rid: str, kind: Optional[str] = None) -> dict:
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            raise GateRefused(409, "no pending spawn request with that id")
        if kind is not None and row.get("kind") != kind:
            raise GateRefused(400, f"request {rid} is a {row.get('kind')} request")
        return row

    @staticmethod
    def _mark(row: dict, status: str, decided_by: str) -> None:
        row["status"] = status
        row["decided_by"] = decided_by
        row["decided_at"] = _stamp()

    def _finish(self, row: dict) -> dict:
        self._tell(row)
        self._save()
        return dict(row)

    async def approve(self, rid: str, *, decided_by: str = "web") -> dict:
        """Create the session (spawn) or respawn the target (resume)."""
        self.reconcile()
        row = self._pending(rid)
        self._clear(rid)
        if row.get("kind") == RESUME:
            row["status"] = RESUMING
            try:
                await self.manager.respawn_async(row["target"])
            except Exception as exc:  # noqa: BLE001 -- reported to the asker
                self._mark(row, FAILED, decided_by)
                row["error"] = str(exc) or type(exc).__name__
            else:
                self._mark(row, APPROVED, decided_by)
            return self._finish(row)
        return await self._create_from(row, decided_by, as_child=row.get("scope") == CHILD,
                                       setup=SETUP_APPROVED)

    async def _create_from(self, row: dict, decided_by: str, *, as_child: bool,
                           setup: str) -> dict:
        """Create the session of the spawn request ``row``: top-level from
        ``spec``, or under the asker (``spec`` of a child request, the
        ``child_spec`` of a root one)."""
        row["status"] = CREATING
        row["setup"] = setup
        row["created_as"] = CHILD if as_child else ROOT
        if self._create is None:
            self._mark(row, FAILED, decided_by)
            row["error"] = "this daemon cannot create sessions from the gate"
            return self._finish(row)
        spec = row["spec"] if row.get("scope") == CHILD or not as_child else (
            row.get("child_spec") or row["spec"])
        try:
            status, payload = await self._create(dict(spec), row["session"], row)
        except Exception as exc:  # noqa: BLE001 -- reported to the asker
            status, payload = 500, {"error": str(exc) or type(exc).__name__}
        if 200 <= status < 300 and payload.get("name"):
            self._mark(row, APPROVED, decided_by)
            row["created"] = payload["name"]
        else:
            self._mark(row, FAILED, decided_by)
            row["error"] = str(payload.get("error") or f"HTTP {status}")
        return self._finish(row)

    def reject(self, rid: str, *, decided_by: str = "web") -> dict:
        self.reconcile()
        row = self._pending(rid)
        self._clear(rid)
        self._mark(row, REJECTED, decided_by)
        return self._finish(row)

    def manual(self, rid: str, *, decided_by: str = "web") -> dict:
        """The person takes the spawn over in the New session form (root)
        or the spawn child form (child). The reply carries ``spec`` for the
        form to be seeded with."""
        row = self._pending(rid, SPAWN)
        self._clear(rid)
        self._mark(row, MANUAL, decided_by)
        row["setup"] = SETUP_CUSTOM
        return self._finish(row)

    def spawn_instead(self, rid: str, *, decided_by: str = "web") -> dict:
        """A resume request answered with a new session instead."""
        self.reconcile()
        row = self._pending(rid, RESUME)
        return self._resume_to_spawn(row, SPAWNED, decided_by)

    def _resume_to_spawn(self, row: dict, status: str, decided_by: str) -> dict:
        self._clear(row["id"])
        self._mark(row, status, decided_by)
        if row.get("fallback"):
            follow = self.submit_spawn(
                session=row["session"], spec=row["fallback"],
                child_spec=row.get("fallback_child"),
                reason=row.get("reason") or "", basis=row.get("basis"),
                origin=row["id"])
            row["followup"] = follow["id"]
        return self._finish(row)

    def link(self, rid: str, created: str) -> dict:
        """The manual form created ``created`` for the request ``rid``."""
        row = self.records.get(rid)
        if row is None or row.get("status") != MANUAL:
            raise GateRefused(409, "no manual spawn request with that id")
        if row.get("created"):
            raise GateRefused(409, f"request {rid} is already linked to {row['created']}")
        created = str(created or "").strip()
        if not created:
            raise GateRefused(400, "session (the created session) is required")
        row["created"] = created
        row["created_at"] = _stamp()
        return self._finish(row)

    def extend(self, rid: str, *, decided_by: str = "web") -> dict:
        """Push the deadline out by :data:`EXTENSION`; nothing settles."""
        row = self._pending(rid)
        now = _now()
        current = _parse(row.get("deadline")) or now
        row["deadline"] = _stamp(max(current, now) + timedelta(seconds=EXTENSION))
        row["extensions"] = int(row.get("extensions") or 0) + 1
        row["extended_at"] = _stamp(now)
        row["extended_by"] = decided_by
        self._save()
        self._arm(row)
        return dict(row)

    def withdraw(self, rid: str, *, session: str) -> dict:
        row = self._pending(rid)
        if str(session or "") != row.get("session"):
            raise GateRefused(403, f"only {row.get('session') or 'the asker'} can withdraw {rid}")
        self._clear(rid)
        self._mark(row, WITHDRAWN, str(session))
        self._save()
        return dict(row)

    def _tell(self, row: dict) -> None:
        if self.on_settle is not None:
            try:
                self.on_settle(dict(row))
            except Exception:  # noqa: BLE001 -- the answer stands without the line
                log.exception("spawn request %s: on_settle failed", row.get("id"))
        if self._notify is None or not row.get("session"):
            return
        try:
            row["told"] = bool(self._notify(row["session"], outcome_message(row)))
        except Exception:  # noqa: BLE001 -- the answer stands without the note
            log.exception("could not tell %s about spawn request %s",
                          row.get("session"), row.get("id"))
            row["told"] = False


def path(base: Optional[Path] = None) -> Path:
    from . import paths

    return (Path(base) if base is not None else paths.daemon_dir()) / FILE
