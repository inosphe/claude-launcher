"""The approval gate an agent's daemon-restart request waits behind.

A restart kills every terminal attached to the daemon, which is why a session
asking for one writes itself down first (:mod:`restart_notice`) — but the
notice is the *aftermath*. The asking turn itself is dead the moment the
request is accepted: whatever the agent was mid-way through is gone, and that
is what the leader's restart costs every time it happens on a live fleet.

This module gives the human a say between the ask and the death. The two
paths that may not wait are the human's own — a shell that typed ``claunch
daemon restart`` and the web UI's Restart button are restarts *by* the person
who would approve them, so they keep going out immediately. The gated path
is the one where the requester and the decider are different people: a
managed session (``CLAUNCH_SESSION`` set) that asked through the CLI. The CLI
decides who is asking — the daemon cannot see the caller's environment, and
this is the same split ``cli_sessions`` already enforces for ``spawn`` — and
hands the request over here; this module holds it until the web UI settles
it.

The settlement is three-way, and the third answer is not a choice:

* **Approved** — the restart goes out exactly as the web button's does: the
  request is recorded (``restart_notice.record_request``) so the successor
  daemon owes the asking session an account of the boot, then the ordinary
  shutdown-with-intent path runs and ``__main__`` spawns the successor.
* **Rejected** — nothing restarts. The request is marked and stays readable
  until the next submit, so the asker's CLI poll can report the outcome; the
  asking turn is alive (nothing has died) and carries on.
* **Unanswered** — after :data:`GATE_TIMEOUT` the request *counts as
  approved* and the restart goes out. A timeout is not a no-op: the agent's
  stop may be load-bearing (a config change, a new build), and holding it
  forever means the turn waits on a person who may be away all afternoon. The
  deadlock breaker is the revert of the default: approval does nothing that
  cannot be undone by the living sessions.

State is in memory on purpose. A gate that had to survive the daemon's death
would be a gate nobody could read — the web page that decides is the page the
daemon itself serves — and a pending request that dies with an unplanned
crash leaves nothing orphaned: no restart happened, and the agent's CLI poll
tells it the daemon went away with the request unanswered.

One request at a time. Two agents wanting two restarts at once both want the
turn death of everything attached; the second one queues nothing and is
refused (``GateBusy`` -> 409), so a fleet that has gone restart-happy loses
one request, not its whole stack of turns.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import restart_notice

#: How long an unanswered gate stays open before it counts as approval. The
#: spec's "max timeout 5 minutes": long enough that a person can come to the
#: page, short enough that nobody waits forever on one who will not.
GATE_TIMEOUT = 300.0


class GateBusy(Exception):
    """A restart request is already pending; the gate takes one at a time."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RestartGate:
    """The single pending approval, owned by one daemon process.

    Everything here is synchronous and lock-free on purpose: the timer
    callback and the HTTP handlers all run on the daemon's one event loop, so
    there is no other thread to race, and ``record_request`` is the one
    blocking file write — the same one the restart it precedes already
    accepts.
    """

    def __init__(self, app, timeout: float = GATE_TIMEOUT) -> None:
        self.app = app
        self.timeout = timeout
        #: The pending (or most recently settled) request — JSON-safe. None
        #: means none was ever submitted since this daemon booted.
        self.record: Optional[dict] = None
        self._timer = None
        app.on_shutdown.append(self.cancel)

    # ------------------------------------------------------------------ #
    # the ask
    # ------------------------------------------------------------------ #
    def submit(self, *, session: str, via: str = "cli") -> dict:
        """Open a gate for one session's restart request.

        Refuses only while one is *pending*: a settled request (rejected — or
        approved, in the instant before the daemon goes down) no longer holds
        the gate, and the new ask replaces it.
        """
        if self.record is not None and self.record["status"] == "pending":
            raise GateBusy(
                f"a restart request from {self.record.get('session') or '?'} is "
                f"already pending"
            )
        now = datetime.now(timezone.utc)
        self.record = {
            "id": secrets.token_hex(6),
            "session": session,
            "via": via,
            "requested_at": now.isoformat(timespec="seconds"),
            "deadline": (now + timedelta(seconds=self.timeout)).isoformat(
                timespec="seconds"
            ),
            "status": "pending",
        }
        self._timer = asyncio.get_running_loop().call_later(
            self.timeout, self._on_timeout, self.record["id"]
        )
        return dict(self.record)

    def get(self) -> Optional[dict]:
        """The pending request (or the last settled one), or None."""
        return dict(self.record) if self.record is not None else None

    # ------------------------------------------------------------------ #
    # the settlement
    # ------------------------------------------------------------------ #
    def approve(self, *, decided_by: str = "web") -> Optional[dict]:
        """Approve and restart. No-op (None) when nothing is pending.

        Otherwise this is exactly the web button's restart — record first so
        the successor owes the asking session an account of the boot, mark
        the intent, trip the ordinary shutdown path — with the one addition
        that the request is attributed to the session that asked, which is
        what makes the successor's notice possible.
        """
        record = self._settle("approved", decided_by)
        if record is None:
            return None
        restart_notice.record_request(
            kind=restart_notice.KIND_RESTART,
            via="agent-approval",
            session=record.get("session"),
            cwd="",
        )
        self.app["restart_requested"] = True
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, self.app["shutdown_event"].set)
        return dict(record)

    def reject(self, *, decided_by: str = "web") -> Optional[dict]:
        """Reject and restart nothing. The asking turn is alive (nothing has
        died), and it learns the outcome through its CLI poll."""
        record = self._settle("rejected", decided_by)
        return dict(record) if record is not None else None

    def _settle(self, status: str, decided_by: str) -> Optional[dict]:
        self._clear_timer()
        record = self.record
        if record is None or record["status"] != "pending":
            return None
        record["status"] = status
        record["decided_at"] = _utcnow()
        record["decided_by"] = decided_by
        return record

    def _on_timeout(self, request_id: str) -> None:
        """The timer fired: count as approval, unless the request was already
        settled between the deadline and the callback (the settle path clears
        the handle, so in practice this is the only caller that can arrive
        here)."""
        if self.record is not None and self.record["id"] == request_id:
            self.approve(decided_by="timeout")

    # ------------------------------------------------------------------ #
    # teardown
    # ------------------------------------------------------------------ #
    async def cancel(self, _app=None) -> None:
        """The daemon is going down — a restart, a stop, a crash. Free the
        timer; the request itself dies with the process, which is the design
        (see the module docstring). ``_app`` is the argument aiohttp's
        ``on_shutdown`` handlers are always called with, and the method is a
        coroutine because aiohttp awaits every handler on that signal."""
        self._clear_timer()

    def _clear_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
