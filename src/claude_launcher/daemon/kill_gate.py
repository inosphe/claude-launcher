"""The approval gate an agent's kill of another session waits behind
(claunch-5aysr).

Ending a session loses whatever it had not reported, and the one asking is
not always the one who knows what that is. It happened: a worker settling its
run ended every live child with no unanswered question -- the reviewer it had
spawned, and a quick-fork the person had opened from it and was about to
merge (s712-qf1). Nothing stood between the call and the SIGTERM, because the
kill routes asked only whether the caller had spawned the target.

So an agent's kill is a request, the same shape as the resume gate
(:mod:`resume_gate`): the agent files it with its reason, the web UI shows it
as a notification card, and only the person's answer acts on it.

Who is gated is read off the request's actor (:mod:`actors`): ``agent`` --
a managed session acting through the MCP ``kill`` tool or the CLI from its
own shell. The person (the dashboard, a shell that is not a session's), a
workflow run's machinery (``cflow``: kill-on-end, the reviewer reaps, the
cflow2 orphan reap) and the daemon itself end sessions as before: the gate is
for the case where the asker and the decider are different. A session ending
itself is not gated either -- that is its own exit, said out loud.

A quick-fork (``SessionDef.quick_fork_of``) is the person's: it ends when the
person merges it from the web UI. An agent's kill of one is refused outright,
not filed -- there is no answer the person would give to "may I end the copy
you are about to merge" that the merge button does not already give.

Settlement:

* **Approved** -- the target is ended as the agent's own kill would have
  ended it: a wind-down first when it holds active board issues, the record
  saying it was ended by that agent.
* **Rejected** -- nothing is ended.
* **Withdrawn** -- the asker took it back (only the asker can).
* **Moot** -- the target exited some other way before anyone answered.
* **Unanswered** -- stays pending. There is no deadline (the user's choice):
  holding the request costs a slot, and that is cheaper than a kill nobody
  approved. The asker is not blocked on the call; it is told the answer in
  its terminal.

Every settlement is typed into the asking session's terminal, so an agent
never polls for it. Pending requests are kept in ``kill-requests.json`` so a
daemon restart does not drop the question. One pending request per target:
a second ask returns the first one (the same decision, asked again, is the
same card).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from .. import atomic

log = logging.getLogger(__name__)

FILE = "kill-requests.json"

#: Settled records kept for the asker and the notification history to read.
#: Pending ones are never pruned.
KEEP_SETTLED = 20

#: The longest reason a request keeps; past it the rest is cut with a marker.
REASON_MAX = 4000

PENDING = "pending"
KILLING = "killing"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
FAILED = "failed"
MOOT = "moot"
#: Not a request: a reviewer the workflow's machinery ended on its own
#: (``cflow_clock.RunEventClock._end_reviewers``), filed so the person is
#: told on the same notification channel without being asked.
REAPED = "reaped"

#: States in which the request still holds the gate for its target.
OPEN = (PENDING, KILLING)


class GateRefused(Exception):
    """The request cannot be filed or settled as asked (HTTP 400/403/409)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_reason(text) -> str:
    reason = str(text or "").strip()
    if len(reason) > REASON_MAX:
        reason = reason[:REASON_MAX].rstrip() + " ...(truncated)"
    return reason


def is_gated(actor: Optional[dict], target: str) -> bool:
    """Whether a kill by ``actor`` of ``target`` waits behind the gate: an
    agent session ending some other session."""
    if not isinstance(actor, dict) or actor.get("kind") != "agent":
        return False
    return str(actor.get("name") or "") != str(target or "")


def quick_fork_refusal(session) -> Optional[str]:
    """The refusal for an agent's kill of a quick-fork, or None when
    ``session`` is not one."""
    origin = getattr(getattr(session, "sdef", None), "quick_fork_of", None)
    if not origin:
        return None
    name = getattr(getattr(session, "sdef", None), "name", "") or "?"
    return (
        f"{name!r} is a quick-fork of {origin!r}: it belongs to the person, "
        "and ends when they merge it from the web UI. An agent does not end "
        "it -- leave it running, or tell the user it looks finished."
    )


def outcome_message(record: dict) -> str:
    """The block typed into the asking session when its request settles."""
    rid = record.get("id") or "?"
    target = record.get("target") or "?"
    status = record.get("status")
    head = f"[claunch kill-request {rid}]"
    by = record.get("decided_by") or "?"
    if status == APPROVED:
        how = "is winding down (settling its board issues first)" if record.get(
            "winding_down") else "was ended"
        body = f"approved: session {target} {how} (by {by})."
    elif status == REJECTED:
        body = (
            f"rejected: session {target} keeps running (by {by}). Do not ask "
            "again for the same reason; carry on without ending it, or ask "
            "the user what they want."
        )
    elif status == FAILED:
        body = (
            f"approved, but ending {target} failed: "
            f"{record.get('error') or 'unknown error'}"
        )
    elif status == MOOT:
        body = f"closed: {record.get('note') or f'session {target} has already exited'}"
    else:
        body = f"{status}: session {target}"
    return f"{head} {body}"


#: ``(target, actor, force) -> info``: ends the target the way the agent's own
#: kill would have (wind-down included). Injected by api.build_app.
EndFn = Callable[[str, dict, bool], Awaitable[dict]]


class KillGate:
    """The kill requests of one daemon, pending and recently settled.

    Everything runs on the daemon's event loop, so there is no lock; the one
    await (the ending) is guarded by the ``killing`` state, so a second
    Approve press on the same card finds nothing pending.
    """

    def __init__(
        self,
        *,
        manager,
        path: Optional[Path] = None,
        notify: Optional[Callable[[str, str], bool]] = None,
        end: Optional[EndFn] = None,
    ) -> None:
        self.manager = manager
        self.path = Path(path) if path is not None else None
        self._notify = notify
        self._end = end
        self.records: Dict[str, dict] = {}
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
            log.warning("unreadable %s; starting with no kill requests", self.path)
            return
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            # An ending the previous daemon was in the middle of did not
            # finish with it: put the question back in front of the person.
            if row.get("status") == KILLING:
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
    # the ask
    # ------------------------------------------------------------------ #
    def _target(self, name: str):
        try:
            return self.manager.get(name)
        except Exception:  # noqa: BLE001 -- ManagerError: no such record
            return None

    def submit(self, *, session: str, target: str, reason: str = "",
               force: bool = False, route: str = "") -> dict:
        """File an agent's kill of ``target``. Returns the record; a pending
        request for the same target is returned as it is (``existing``)."""
        session = str(session or "").strip()
        target = str(target or "").strip()
        if not session:
            raise GateRefused(400, "session (the asking session) is required")
        if not target:
            raise GateRefused(400, "target (the session to end) is required")
        if target == session:
            raise GateRefused(400, "a session ending itself is not a request")
        found = self._target(target)
        if found is None:
            raise GateRefused(404, f"no session named {target!r}")
        refusal = quick_fork_refusal(found)
        if refusal:
            raise GateRefused(403, refusal)
        self.reconcile()
        for row in self.records.values():
            if row.get("target") == target and row.get("status") in OPEN:
                return {**row, "existing": True}
        record = {
            "id": secrets.token_hex(6),
            "session": session,
            "target": target,
            "reason": clean_reason(reason),
            "force": bool(force),
            "route": str(route or ""),
            "requested_at": _utcnow(),
            "status": PENDING,
            "target_role": _role_of(found),
            "target_parent": getattr(getattr(found, "sdef", None), "parent", None),
            "target_cwd": getattr(getattr(found, "sdef", None), "cwd", "") or "",
            "decided_by": None,
            "decided_at": None,
        }
        self.records[record["id"]] = record
        self._save()
        return dict(record)

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def reconcile(self) -> None:
        """Close the pending requests whose target exited some other way.

        A target the manager does not know is left alone: right after a
        daemon start the restore may not have put it back yet."""
        changed = []
        for row in self.records.values():
            if row.get("status") != PENDING:
                continue
            found = self._target(row.get("target") or "")
            if found is not None and found.exited and not getattr(found, "held_at", None):
                row["status"] = MOOT
                row["decided_by"] = "daemon"
                row["decided_at"] = _utcnow()
                row["note"] = f"session {row.get('target')} has already exited"
                changed.append(row)
        if changed:
            self._save()
            for row in changed:
                self._tell(row)

    def list(self) -> List[dict]:
        self.reconcile()
        rows = sorted(
            self.records.values(),
            key=lambda r: (r.get("status") not in OPEN, r.get("requested_at") or ""),
        )
        return [dict(r) for r in rows]

    def get(self, rid: str) -> Optional[dict]:
        self.reconcile()
        row = self.records.get(rid)
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # the settlement
    # ------------------------------------------------------------------ #
    async def approve(self, rid: str, *, decided_by: str = "web") -> Optional[dict]:
        """End the target. None when ``rid`` is not pending."""
        self.reconcile()
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return None
        if self._end is None:
            raise GateRefused(503, "this daemon cannot end sessions from the gate")
        row["status"] = KILLING
        actor = {"kind": "agent", "name": row.get("session") or "?",
                 "reason": "kill-approved"}
        try:
            info = await self._end(row["target"], actor, bool(row.get("force")))
        except Exception as exc:  # noqa: BLE001 -- reported to the asker
            row["status"] = FAILED
            row["error"] = str(exc) or type(exc).__name__
        else:
            row["status"] = APPROVED
            row["winding_down"] = bool((info or {}).get("winding_down"))
        row["decided_by"] = decided_by
        row["decided_at"] = _utcnow()
        self._save()
        self._tell(row)
        return dict(row)

    def reject(self, rid: str, *, decided_by: str = "web") -> Optional[dict]:
        row = self._settle(rid, REJECTED, decided_by)
        if row is not None:
            self._tell(row)
        return row

    def withdraw(self, rid: str, *, session: str) -> Optional[dict]:
        """The asker takes its request back; nobody else may."""
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return None
        if str(session or "") != row.get("session"):
            raise GateRefused(
                403, f"only {row.get('session') or 'the asker'} can withdraw {rid}"
            )
        return self._settle(rid, WITHDRAWN, str(session))

    def note_auto(self, driver: str, target: str, reason: str) -> dict:
        """File a reviewer the workflow ended without asking (claunch-5aysr):
        the user chose to be told, not asked, for this one ending. Settled on
        arrival -- the dashboard shows it as a notice, never as a card that
        waits."""
        now = _utcnow()
        record = {
            "id": secrets.token_hex(6),
            "session": str(driver or ""),
            "target": str(target or ""),
            "reason": str(reason or ""),
            "route": "cflow",
            "requested_at": now,
            "status": REAPED,
            "decided_by": "cflow",
            "decided_at": now,
        }
        self.records[record["id"]] = record
        self._save()
        return dict(record)

    def _settle(self, rid: str, status: str, decided_by: str) -> Optional[dict]:
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return None
        row["status"] = status
        row["decided_by"] = decided_by
        row["decided_at"] = _utcnow()
        self._save()
        return dict(row)

    def _tell(self, row: dict) -> None:
        if self._notify is None or not row.get("session"):
            return
        try:
            row["told"] = bool(self._notify(row["session"], outcome_message(row)))
        except Exception:  # noqa: BLE001 -- the answer stands without the note
            log.exception("could not tell %s about kill request %s",
                          row.get("session"), row.get("id"))
            row["told"] = False


def _role_of(session) -> str:
    return str(getattr(getattr(session, "sdef", None), "role", "") or "")


def pending_reply(record: dict) -> dict:
    """What a gated kill route answers: nothing ended yet, and how the asker
    hears the answer."""
    return {
        "pending": True,
        "kill_request": record.get("id"),
        "target": record.get("target"),
        "existing": bool(record.get("existing")),
        "note": (
            "an agent's kill of another session waits for a person: the request "
            f"{record.get('id')} is on the web UI's notification card. Nothing "
            "has been ended. The answer is typed into this terminal as a "
            "[claunch kill-request] block -- do not poll, and do not ask again."
        ),
    }


def path(base: Optional[Path] = None) -> Path:
    from . import paths

    return (Path(base) if base is not None else paths.daemon_dir()) / FILE
