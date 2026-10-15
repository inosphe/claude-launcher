"""The approval gate an agent's request to resume an exited session waits behind
(claunch-yjhgx).

Relaunching an exited session is an operator action (``POST
/api/sessions/{name}/respawn``, the rail's Respawn button): the session comes
back on ``--resume`` of its pinned conversation and starts spending again, and
whoever ended it may have ended it on purpose. An agent that needs a finished
session back -- the reviewer it reported to, the child whose context it needs
-- used to have no door at all. This module is that door, with the same shape
as the daemon-restart gate (:mod:`restart_gate`): the agent files a request
with its reason, the web UI shows it as a notification card, and only the
person's answer acts on it.

Settlement:

* **Approved** -- the target is respawned exactly as the operator's button
  does it (:meth:`SessionManager.respawn_async`). A respawn that fails settles
  the request as ``failed`` with the error, so the asker learns why.
* **Rejected** -- nothing is relaunched.
* **Withdrawn** -- the asker took it back (only the asker can).
* **Moot** -- the target is running again by some other way (an operator's
  respawn, a wake) or its record is gone; there is nothing left to answer.
* **Unanswered** -- stays pending. There is no deadline: the user asked for
  the request to wait as long as it takes. Unlike the restart gate, holding
  it costs nothing that is running -- the target is already stopped, and the
  asker is not blocked on a call; it is told the answer in its terminal.

Every settlement is typed into the asking session's terminal (``notify``), so
an agent never polls for it.

Because a pending request has no deadline it has to outlive the daemon that
took it, or a restart would silently drop the question: the records are kept
in ``resume-requests.json`` beside the daemon's other state. One pending
request per target -- a second asker is refused (``GateBusy`` -> 409) with
the first one's id, rather than stacking two cards for one decision.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import atomic

log = logging.getLogger(__name__)

FILE = "resume-requests.json"

#: Settled records kept for the asker and the notification history to read.
#: Pending ones are never pruned.
KEEP_SETTLED = 20

#: The longest reason a request keeps; past it the rest is cut with a marker
#: (a request must not fail over its prose).
REASON_MAX = 4000

PENDING = "pending"
RESUMING = "resuming"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
FAILED = "failed"
MOOT = "moot"

#: States in which the request still holds the gate for its target.
OPEN = (PENDING, RESUMING)


class GateBusy(Exception):
    """The target already has a pending resume request."""


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


def outcome_message(record: dict) -> str:
    """The block typed into the asking session when its request settles."""
    rid = record.get("id") or "?"
    target = record.get("target") or "?"
    status = record.get("status")
    head = f"[claunch resume-request {rid}]"
    if status == APPROVED:
        body = (
            f"approved: session {target} was resumed (by "
            f"{record.get('decided_by') or '?'}). It comes back on its own "
            "conversation and knows nothing of your request -- message it if "
            "it should act on something."
        )
    elif status == REJECTED:
        body = (
            f"rejected: session {target} stays exited (by "
            f"{record.get('decided_by') or '?'}). Do not ask again for the "
            "same reason; carry on without it or ask the user what they want."
        )
    elif status == FAILED:
        body = (
            f"approved, but the resume of {target} failed: "
            f"{record.get('error') or 'unknown error'}"
        )
    elif status == MOOT:
        body = f"closed: {record.get('note') or f'session {target} is no longer exited'}"
    else:
        body = f"{status}: session {target}"
    return f"{head} {body}"


class ResumeGate:
    """The resume requests of one daemon, pending and recently settled.

    Everything runs on the daemon's event loop: submits, settlements and the
    reconcile pass cannot interleave mid-record, so there is no lock. The one
    await (the respawn) is guarded by the ``resuming`` state, so a second
    Approve press on the same card finds nothing pending.
    """

    def __init__(
        self,
        *,
        manager,
        path: Optional[Path] = None,
        notify: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        self.manager = manager
        self.path = Path(path) if path is not None else None
        #: ``(session, message) -> delivered``: types the outcome into the
        #: asking session. Injected by api.build_app; None disables it.
        self._notify = notify
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
            log.warning("unreadable %s; starting with no resume requests", self.path)
            return
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            # A respawn the previous daemon was in the middle of did not
            # finish with it: put the question back in front of the person.
            if row.get("status") == RESUMING:
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

    def submit(self, *, session: str, target: str, reason: str) -> dict:
        session = str(session or "").strip()
        target = str(target or "").strip()
        reason = clean_reason(reason)
        if not session:
            raise GateRefused(400, "session (the asking session) is required")
        if not target:
            raise GateRefused(400, "target (the exited session to resume) is required")
        if not reason:
            raise GateRefused(
                400,
                "reason is required: the person answering decides on it alone "
                "-- say what you need the session for",
            )
        if target == session:
            raise GateRefused(400, "a session cannot ask to resume itself")
        found = self._target(target)
        if found is None:
            raise GateRefused(400, f"no session named {target!r}")
        if not found.exited:
            raise GateRefused(
                409, f"session {target!r} is running -- message it instead"
            )
        if getattr(found, "held_at", None):
            raise GateRefused(
                409,
                f"session {target!r} is held, not exited: it wakes on its own "
                "when it is sent a message, so message it instead",
            )
        self.reconcile()
        for row in self.records.values():
            if row.get("target") == target and row.get("status") in OPEN:
                raise GateBusy(
                    f"a resume request for {target} is already pending "
                    f"({row['id']}, asked by {row.get('session') or '?'})"
                )
        record = {
            "id": secrets.token_hex(6),
            "session": session,
            "target": target,
            "reason": reason,
            "requested_at": _utcnow(),
            "status": PENDING,
            "target_exited_at": getattr(found, "exited_at", None),
            "target_archived": bool(getattr(found, "archived_at", None)),
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
        """Close the pending requests whose target came back some other way.

        A target the manager does not know is left alone: right after a
        daemon start the restore may not have put it back yet, and closing
        the question on that would lose it. An approval then fails with the
        manager's error instead, which says what happened."""
        changed = []
        for row in self.records.values():
            if row.get("status") != PENDING:
                continue
            found = self._target(row.get("target") or "")
            if found is not None and not found.exited:
                row["status"] = MOOT
                row["decided_by"] = "daemon"
                row["decided_at"] = _utcnow()
                row["note"] = f"session {row.get('target')} is running again"
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
        """Respawn the target. None when ``rid`` is not pending."""
        self.reconcile()
        row = self.records.get(rid)
        if row is None or row.get("status") != PENDING:
            return None
        row["status"] = RESUMING
        try:
            await self.manager.respawn_async(row["target"])
        except Exception as exc:  # noqa: BLE001 -- reported to the asker
            row["status"] = FAILED
            row["error"] = str(exc) or type(exc).__name__
        else:
            row["status"] = APPROVED
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
            log.exception("could not tell %s about resume request %s",
                          row.get("session"), row.get("id"))
            row["told"] = False


def path(base: Optional[Path] = None) -> Path:
    from . import paths

    return (Path(base) if base is not None else paths.daemon_dir()) / FILE
