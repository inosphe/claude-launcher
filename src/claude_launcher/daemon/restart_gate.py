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
* **Extended** — not a settlement: the deadline moves out by
  :data:`EXTENSION` and the request stays pending, as many times as the
  person presses it. It is for the person who is here and is not ready,
  whom the deadline would otherwise answer for. There is no ceiling: a cflow
  step's ``restart.timeout`` does not cut a held gate short, because the
  RestartClock asks :func:`holding` before it kills the waiting shell
  (``cflow_clock.RestartClock._execute``).
* **Unanswered** — after :data:`GATE_TIMEOUT` (plus whatever was added) the
  request *counts as approved* and the restart goes out. A timeout is not a no-op: the agent's
  stop may be load-bearing (a config change, a new build), and holding it
  forever means the turn waits on a person who may be away all afternoon. The
  deadlock breaker is the revert of the default: approval does nothing that
  cannot be undone by the living sessions.

A request carries the asker's **reason**: why the restart is needed (the
merges the live daemon does not serve yet, a config change, a new build).
The card prints it in full, because the person deciding is being asked to
cut off every attached turn and the reason is the whole basis for the click
(the goto gate does the same, ``goto_gate``). A request without one is still
filed: an older CLI does not send the field.

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


#: How much one Extend adds to a pending request's deadline. It may be
#: pressed any number of times (claunch-mm8zv). It used to stop at two
#: presses (870 seconds in all) so the gate would end inside cflow's
#: ``restart.timeout`` ceiling of 900 -- past that the RestartClock killed
#: the waiting shell and the run journaled a timeout for a restart that then
#: happened anyway. The clock now keeps waiting while :func:`holding` says
#: this gate still holds that session's request, so the ceiling no longer
#: bounds how long a person may take.
EXTENSION = 300.0

#: The longest reason a request keeps. A reason is read by a person on a
#: notification card; past this it is a log, and the rest is cut with a
#: marker rather than refused -- a restart must not fail over its prose.
REASON_MAX = 4000

#: The gate this daemon process serves, for readers that are not handed the
#: app (the RestartClock runs in a worker thread). Set by the constructor;
#: one daemon builds one gate.
_LIVE: Optional["RestartGate"] = None


def holding(session: str) -> bool:
    """Whether this process's gate holds a pending request from ``session``.

    Read from the RestartClock's thread while the restart shell it ran is
    waiting on the gate: a True keeps the clock from killing that shell at
    the step's ``restart.timeout``. A plain read of one dict; the event loop
    is the only writer, and a stale answer costs one poll interval.
    """
    gate = _LIVE
    record = gate.record if gate is not None else None
    return bool(
        record is not None
        and record.get("status") == "pending"
        and session
        and record.get("session") == session
    )


def clean_reason(text) -> str:
    """A submitted reason as the record keeps it: stripped, capped."""
    reason = str(text or "").strip()
    if len(reason) > REASON_MAX:
        reason = reason[:REASON_MAX].rstrip() + " ...(truncated)"
    return reason


class GateBusy(Exception):
    """A restart request is already pending; the gate takes one at a time."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp) -> Optional[datetime]:
    """One of this record's own timestamps, or None if it cannot be read."""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: The restart modes a request can carry (``pty_host``); see :meth:`RestartGate.submit`.
PTY_HOST_MODES = ("keep", "full")


class StaleHost(Exception):
    """A keep approval would leave the PTY host on code other than what the
    next daemon loads (claunch-xnnct.3); ``check`` is
    :func:`host_code.keep_check`'s answer."""

    def __init__(self, message: str, check: dict) -> None:
        super().__init__(message)
        self.check = check


def _stale_check(record: dict) -> Optional[dict]:
    """The keep check for a request about to be applied, or None when it
    does not apply (mode full, or the asker said keep it anyway)."""
    if (record.get("pty_host") or "keep") != "keep" or record.get("keep_stale"):
        return None
    from . import host_code

    check = host_code.keep_check()
    return check if check["stale"] else None


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
        global _LIVE
        _LIVE = self

    # ------------------------------------------------------------------ #
    # the ask
    # ------------------------------------------------------------------ #
    def submit(self, *, session: str, via: str = "cli", reason: str = "",
               pty_host: str = "keep", keep_stale: bool = False) -> dict:
        """Open a gate for one session's restart request.

        ``pty_host`` is the restart's mode (claunch-xnnct.1): ``"keep"``
        leaves the PTY host and its sessions running, ``"full"`` (the CLI's
        ``--with-pty-host``) takes them down too. It is the asker's choice,
        shown on the card and applied on approval -- by a person or by the
        clock; nobody changes it in between (reject and ask again). The one
        exception is a stale PTY host found at approval (claunch-xnnct.3):
        the approver then picks full or keep-anyway, see :meth:`approve`.
        ``keep_stale`` is the asker's ``--keep-stale-host``.

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
            "reason": clean_reason(reason),
            "requested_at": now.isoformat(timespec="seconds"),
            "deadline": (now + timedelta(seconds=self.timeout)).isoformat(
                timespec="seconds"
            ),
            "status": "pending",
            "extensions": 0,
            "pty_host": pty_host if pty_host in PTY_HOST_MODES else "keep",
            "keep_stale": bool(keep_stale),
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
    def extend(self, *, decided_by: str = "web") -> Optional[dict]:
        """Push the deadline out and leave the request pending.

        The gate's third answer is the clock's, and this is how a person who
        is present takes it back without deciding yet: reading the diff that
        the restart would interrupt, waiting for a worker to reach a resting
        point, finishing a turn of their own. Nothing settles, so the asking
        session stays exactly where it was -- it is still waiting, just for
        longer, and its CLI poll reads the new deadline off the same record.

        Returns None when nothing is pending. There is no limit on how many
        times it may be pressed (claunch-mm8zv).
        """
        record = self.record
        if record is None or record["status"] != "pending":
            return None
        used = int(record.get("extensions") or 0)
        # Added to the deadline, not to now: pressing early must not shorten
        # the wait it was meant to lengthen.
        now = datetime.now(timezone.utc)
        current = _parse(record.get("deadline")) or now
        deadline = current + timedelta(seconds=EXTENSION)
        self._clear_timer()
        record["extensions"] = used + 1
        record["deadline"] = deadline.isoformat(timespec="seconds")
        record["extended_at"] = _utcnow()
        record["extended_by"] = decided_by
        self._timer = asyncio.get_running_loop().call_later(
            (deadline - now).total_seconds(), self._on_timeout, record["id"]
        )
        return dict(record)

    def approve(self, *, decided_by: str = "web", pty_host: Optional[str] = None,
                keep_stale: bool = False) -> Optional[dict]:
        """Approve and restart. No-op (None) when nothing is pending.

        Otherwise this is exactly the web button's restart — record first so
        the successor owes the asking session an account of the boot, mark
        the intent, trip the ordinary shutdown path — with the one addition
        that the request is attributed to the session that asked, which is
        what makes the successor's notice possible.

        A keep restart onto a stale PTY host (claunch-xnnct.3) raises
        :class:`StaleHost` and leaves the request pending; the approver
        answers again with ``pty_host="full"`` or ``keep_stale=True``.
        """
        record = self.record
        if record is not None and record["status"] == "pending":
            if pty_host in PTY_HOST_MODES:
                record["pty_host"] = pty_host
            if keep_stale:
                record["keep_stale"] = True
            check = _stale_check(record)
            if check is not None:
                from . import host_code

                record["stale"] = check
                raise StaleHost(host_code.refusal(check), check)
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
        self.app["restart_mode"] = record.get("pty_host") or "keep"
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
            try:
                self.approve(decided_by="timeout")
            except StaleHost as exc:
                # The clock cannot pick between ending the sessions and
                # keeping the old host, so it restarts nothing; the asking
                # CLI reads the refusal off the record (claunch-xnnct.3).
                record = self._settle("rejected", "timeout")
                if record is not None:
                    record["refused"] = str(exc)

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
