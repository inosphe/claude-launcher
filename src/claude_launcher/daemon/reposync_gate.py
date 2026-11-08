"""The approval gate a session's repository sync waits behind (claunch-dh73y).

A sync pushes to a shared remote and, when both machines moved, rewrites the
unpushed commits of master -- which strands every branch built on them and
turns the board's ``merged <hash>`` proofs into hashes that no longer exist.
The user asked for that to be their call (decision Q5): by default a person
approves each sync that pushes, and ``daemon.reposync_push: auto`` lifts it.

The asker and the approver must be different people for the approval to mean
anything, so an agent session cannot approve by typing: its CLI files the
plan here and polls, and only the web UI's Approve settles it. A person at
their own terminal answers the CLI's prompt instead and never reaches this
module.

Settlement:

* **Approved** -- the asking CLI carries the plan out.
* **Rejected** -- nothing happens; the CLI reports the refusal.
* **Unanswered** -- after :data:`GATE_TIMEOUT` the request *expires* and
  counts as rejected. The restart and goto gates default the other way
  because what they hold is reversible; a push to a shared remote is not.

Every settlement (approved, rejected, expired) is also typed into the asking
session's terminal (``notify``, as in :mod:`.kill_gate`): the MCP tool does not
block, so a session that ended its turn after ``waiting_approval`` would not
learn of a later approval and would wait for the user to tell it.

State is in memory, like the other gates: the page that decides is served by
this daemon, and a request that outlived it would be a card nobody can press.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

#: How long an unanswered request stays open before it expires (= rejected).
GATE_TIMEOUT = 1800.0

#: Settled requests kept for the asker's poll to read back.
KEEP_SETTLED = 20

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"

#: Plan fields a card shows. Anything else in a submitted plan is dropped, so
#: the daemon never holds more of a repository than the card prints.
PLAN_FIELDS = (
    "repo", "workspace", "remote", "remote_key", "branch", "action",
    "ahead", "behind", "local", "upstream", "merges", "affected", "summary",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def outcome_message(record: dict) -> str:
    """The block typed into the asking session when its request settles."""
    rid = record.get("id") or "?"
    plan = record.get("plan") or {}
    what = f"{plan.get('action') or 'sync'} {plan.get('remote') or '?'}/{plan.get('branch') or '?'}"
    status = record.get("status")
    by = record.get("decided_by") or "?"
    head = f"[claunch reposync-request {rid}]"
    if status == APPROVED:
        body = (
            f"approved ({what}, by {by}). Call repo_sync again with "
            f"request={rid} (same action, workspace, remote and branch) to carry "
            "it out; the approval covers only the tips the card showed, so a "
            "'stale_approval' answer means replan and ask again."
        )
    elif status == REJECTED:
        body = (
            f"rejected ({what}, by {by}); nothing was pushed. Do not ask again "
            "for the same plan; carry on without it, or ask the user what they want."
        )
    else:
        body = (
            f"{status} ({what}); nothing was pushed. Ask again with a new "
            "repo_sync call if the user still wants it."
        )
    return f"{head} {body}"


class ReposyncGate:
    def __init__(self, timeout: float = GATE_TIMEOUT,
                 notify: Optional[Callable[[str, str], bool]] = None) -> None:
        self.timeout = float(timeout)
        self._notify = notify
        self._requests: Dict[str, dict] = {}
        self._order: List[str] = []

    # ------------------------------------------------------------------ #
    def submit(self, *, session: str, plan: dict) -> dict:
        if not isinstance(plan, dict) or not plan.get("action"):
            raise ValueError("plan with an action is required")
        rid = secrets.token_hex(6)
        now = _now()
        record = {
            "id": rid,
            "status": PENDING,
            "session": str(session or ""),
            "plan": {k: plan[k] for k in PLAN_FIELDS if k in plan},
            "submitted_at": _iso(now),
            "deadline": _iso(now + timedelta(seconds=self.timeout)),
            "decided_by": None,
            "decided_at": None,
        }
        self._requests[rid] = record
        self._order.append(rid)
        self._trim()
        return dict(record)

    def get(self, rid: str) -> Optional[dict]:
        self._expire()
        record = self._requests.get(rid)
        return dict(record) if record else None

    def list(self) -> List[dict]:
        self._expire()
        rows = [self._requests[rid] for rid in self._order if rid in self._requests]
        rows.sort(key=lambda r: (r["status"] != PENDING, r["submitted_at"]))
        return [dict(r) for r in rows]

    def approve(self, rid: str, *, decided_by: str = "web") -> Optional[dict]:
        return self._settle(rid, APPROVED, decided_by)

    def reject(self, rid: str, *, decided_by: str = "web") -> Optional[dict]:
        return self._settle(rid, REJECTED, decided_by)

    # ------------------------------------------------------------------ #
    def _settle(self, rid: str, status: str, decided_by: str) -> Optional[dict]:
        self._expire()
        record = self._requests.get(rid)
        if record is None or record["status"] != PENDING:
            return None
        record["status"] = status
        record["decided_by"] = decided_by
        record["decided_at"] = _iso(_now())
        self._tell(record)
        return dict(record)

    def _expire(self) -> None:
        now = _iso(_now())
        for record in self._requests.values():
            if record["status"] == PENDING and record["deadline"] <= now:
                record["status"] = EXPIRED
                record["decided_by"] = "timeout"
                record["decided_at"] = now
                self._tell(record)

    def _tell(self, record: dict) -> None:
        if self._notify is None or not record.get("session"):
            return
        try:
            record["told"] = bool(self._notify(record["session"], outcome_message(record)))
        except Exception:  # noqa: BLE001 -- the answer stands without the note
            log.exception("could not tell %s about reposync request %s",
                          record.get("session"), record.get("id"))
            record["told"] = False

    def _trim(self) -> None:
        settled = [rid for rid in self._order if self._requests[rid]["status"] != PENDING]
        for rid in settled[:-KEEP_SETTLED] if len(settled) > KEEP_SETTLED else []:
            self._requests.pop(rid, None)
            self._order.remove(rid)
