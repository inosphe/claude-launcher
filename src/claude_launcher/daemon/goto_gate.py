"""The approval gate a leader's run-move request waits behind.

A session may command its own subtree and nothing else (manager.require_commands
— the same authority kill, connect and reparent stand on). Until now that
authority stopped at sessions: a leader that had verified where a child's cflow
run actually stood still had to ask a human to type ``claunch cflow goto -t
<child>`` for it — the judgement was the leader's, the keystrokes were the
person's, six times over in one afternoon (claunch-ny72).

This module is the missing door. The leader files a *request* naming the
child's run, the step and the grounds; the request lands here and waits:

* **Approved** — the person clicks Approve in the web UI; the move goes
  through the engine's ordinary settlement (:func:`cflow.engine.resolve_goto`),
  so the journal reads ``goto_requested`` (by the leader, via 'leader') then
  ``goto_approved``/``state_forced`` — not a bare override.
* **Denied** — nothing moves; the refusal waits in the child run's state for
  its driver to read, exactly as when the driver itself had asked.
* **Unanswered** — after :data:`GATE_TIMEOUT` the request *counts as
  approved*. The user asked for this fallback outright ("5분 지나서 fallback
  되면 자동 승인"): a run already verified as stuck must not stay stuck
  because nobody is watching the page, and a move the leader justified is
  reversible by the same person with the ordinary goto.

Filing moves nothing and stops the *child's* run: the request is recorded on
that run (``goto_request`` in its state), so its driver's next poll answers
``waiting_goto`` and holds until the person settles it. One pending request
per run — a second filer is refused (``GateBusy`` -> 409) rather than
queued, because two pending answers to "where should this run be" is one
question too many.

State is in memory, like the restart gate's, and for the same reason plus
one: the page that decides is the page this daemon serves, and a request
that outlived its daemon would be a card nobody can click. On shutdown the
gate *withdraws* what is still pending (best-effort, journaled
``goto_withdrawn`` on the child run) — a held run must not stay held behind
a gate that no longer exists; the leader re-files after the restart.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional

from ..cflow import engine as cflow_engine, state as cflow_state

#: How long an unanswered request stays open before it counts as approval.
#: The user's spec: "5분 지나서 fallback 되면 자동 승인". Pinned in a test
#: against store.DAEMON_DEFAULTS["goto_approval_timeout"], which is what the
#: daemon actually wires in.
GATE_TIMEOUT = 300.0

#: Settled records kept for readers (the leader's poll, the web card's last
#: state). Pending ones are never pruned.
_KEEP_SETTLED = 20


class GateBusy(Exception):
    """This run already has a pending goto request; one question at a time."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GotoGate:
    """The pending leader requests, owned by one daemon process.

    Like :class:`restart_gate.RestartGate`, everything here runs on the
    daemon's one event loop: submits, settlements and the timer callbacks
    cannot interleave mid-record, so no lock. The engine calls inside are
    synchronous file operations under the run slot's own cross-process lock
    (``engine._locked_op``) — the same calls the web UI's goto endpoints
    already make from this process.
    """

    def __init__(
        self,
        app,
        *,
        manager,
        timeout: float = GATE_TIMEOUT,
        nudge: Optional[Callable[[str, str, str], Awaitable[list]]] = None,
    ) -> None:
        self.app = app
        self.manager = manager
        self.timeout = timeout
        #: Async ``(cwd, scope, message) -> [sessions]``: types the resume
        #: nudge into the child run's driver. Injected by api.build_app (the
        #: helper lives there); tests pass a fake. None disables nudging.
        self._nudge = nudge
        #: Live and recently settled requests, by id.
        self.records: Dict[str, dict] = {}
        self._timers: Dict[str, object] = {}
        app.on_shutdown.append(self.cancel)

    # ------------------------------------------------------------------ #
    # the ask
    # ------------------------------------------------------------------ #
    def submit(self, *, session: str, target_session: str, step: str, reason: str) -> dict:
        """Open a gate for one leader's request to move a child's run.

        The checks, in the order a wrong request meets them:

        * authority: ``session`` must command ``target_session`` (ancestor
          only — a peer cannot move a peer's run);
        * the run: exactly one registered run owned by the target session;
        * the move itself: the engine's own ``request_goto`` refuses a
          finished run, an unknown step, or the step the run is already on;
        * one pending request per run (GateBusy).
        """
        session = (session or "").strip()
        target_session = (target_session or "").strip()
        step = (step or "").strip()
        reason = (reason or "").strip()
        if not session:
            raise cflow_engine.CflowError("session (the requesting leader) is required")
        if not target_session:
            raise cflow_engine.CflowError("target_session is required")
        if not step:
            raise cflow_engine.CflowError("step is required")
        if not reason:
            raise cflow_engine.CflowError(
                "reason is required: the person answering has not watched the "
                "child's run, and a step id on its own is not something anyone "
                "can approve or refuse"
            )
        if session == target_session:
            raise cflow_engine.CflowError(
                "a session's own run is moved by its own request_goto, not "
                "through this gate"
            )
        # Authority runs down the tree only (ManagerError -> 400 with the
        # explanation require_commands words).
        self.manager.require_commands(session, target_session)
        hits = sorted(
            {c for c, s in cflow_state.known_runs() if s == target_session}
        )
        if not hits:
            raise cflow_engine.CflowError(
                f"no cflow run for session {target_session!r} in the run "
                f"registry — nothing to move"
            )
        if len(hits) > 1:
            raise cflow_engine.CflowError(
                f"session {target_session!r} has cflow runs in several "
                f"directories ({', '.join(hits)}); refusing to guess"
            )
        cwd, scope = hits[0], target_session
        for record in self.records.values():
            if (
                record["status"] == "pending"
                and record["cwd"] == cwd
                and record["scope"] == scope
            ):
                raise GateBusy(
                    f"a goto request from {record.get('session') or '?'} for "
                    f"this run is already pending"
                )
        # Filing on the run is what holds it: the driver's next poll answers
        # waiting_goto. Engine errors (finished run, unknown/current step)
        # escape before any gate record exists.
        filed = cflow_engine.request_goto(
            step, reason, by=session, via="leader", cwd=cwd, scope=scope
        )
        engine_request = filed["goto_request"]
        now = datetime.now(timezone.utc)
        record = {
            "id": secrets.token_hex(6),
            "session": session,
            "target_session": target_session,
            "cwd": cwd,
            "scope": scope,
            "run": filed.get("run"),
            "request": engine_request.get("id"),
            "step": step,
            "from": engine_request.get("from"),
            "reason": reason,
            "requested_at": now.isoformat(timespec="seconds"),
            "deadline": (now + timedelta(seconds=self.timeout)).isoformat(
                timespec="seconds"
            ),
            "status": "pending",
        }
        self.records[record["id"]] = record
        self._timers[record["id"]] = asyncio.get_running_loop().call_later(
            self.timeout, self._on_timeout, record["id"]
        )
        self._prune()
        return dict(record)

    def get(self, request_id: str) -> Optional[dict]:
        record = self.records.get(request_id)
        return dict(record) if record is not None else None

    def list(self) -> List[dict]:
        """Pending first, then settled, newest last within each."""
        records = sorted(
            self.records.values(), key=lambda r: r.get("requested_at") or ""
        )
        return [dict(r) for r in records if r["status"] == "pending"] + [
            dict(r) for r in records if r["status"] != "pending"
        ]

    # ------------------------------------------------------------------ #
    # the settlement
    # ------------------------------------------------------------------ #
    def approve(self, request_id: str, *, decided_by: str = "web") -> Optional[dict]:
        """Grant the move that was asked for and apply it through the engine's
        ordinary settlement, so the journal keeps the request attached to the
        move (``granted``/``asked_by`` on the ``state_forced`` event)."""
        record = self._settle(request_id, "approved", decided_by)
        if record is None:
            return None
        self._apply(record, decision="approve", decided_by=decided_by)
        return dict(record)

    def deny(
        self, request_id: str, *, decided_by: str = "web", reason: Optional[str] = None
    ) -> Optional[dict]:
        """Refuse: nothing moves, and the refusal waits in the child run's
        state for its driver — the same shape as a denied own-run request."""
        record = self._settle(request_id, "denied", decided_by)
        if record is None:
            return None
        record["decided_reason"] = (reason or "").strip()
        self._apply(record, decision="deny", decided_by=decided_by, reason=reason)
        return dict(record)

    def withdraw(self, request_id: str, *, actor: str) -> Optional[dict]:
        """The filing leader takes its question back — the reason stopped
        being true, or the run was unblocked another way. Nobody else may
        withdraw through this door: the person's answer is approve/deny."""
        record = self.records.get(request_id)
        if record is None or record["status"] != "pending":
            return None
        if actor != record["session"]:
            raise cflow_engine.CflowError(
                f"only {record['session']!r} may withdraw this request — it "
                f"is the asker"
            )
        settled = self._settle(request_id, "withdrawn", actor)
        try:
            cflow_engine.cancel_goto_request(
                by=actor, cwd=record["cwd"], scope=record["scope"]
            )
        except Exception as exc:  # noqa: BLE001 — run moved on its own first
            settled["status"] = "moot"
            settled["error"] = str(exc)
        self._nudge_child(settled, cflow_engine.NUDGE_CONTINUE)
        return dict(settled)

    def _settle(self, request_id: str, status: str, decided_by: str) -> Optional[dict]:
        record = self.records.get(request_id)
        if record is None or record["status"] != "pending":
            return None
        timer = self._timers.pop(request_id, None)
        if timer is not None:
            timer.cancel()
        record["status"] = status
        record["decided_at"] = _utcnow()
        record["decided_by"] = decided_by
        return record

    def _apply(
        self,
        record: dict,
        *,
        decision: str,
        decided_by: str,
        reason: Optional[str] = None,
    ) -> None:
        """Carry the settlement to the child run, then tell both ends.

        The engine request can be gone by now — the child withdrew it, a
        human forced a third position (which supersedes), the run finished.
        That is not a gate failure: the record is marked ``moot`` with the
        engine's own words, and the leader is told rather than left
        believing its request moved the run.
        """
        try:
            payload = cflow_engine.resolve_goto(
                decision,
                by=decided_by,
                reason=reason,
                cwd=record["cwd"],
                scope=record["scope"],
            )
        except Exception as exc:  # noqa: BLE001 — CflowError and friends
            record["status"] = "moot"
            record["error"] = str(exc)
            self._tell_leader(
                record,
                f"goto request {record['id']} ({record['target_session']} -> "
                f"{record['step']!r}) settled as {decision} but nothing was "
                f"applied: {exc}",
            )
            self._nudge_child(record, cflow_engine.NUDGE_CONTINUE)
            return
        asked = (payload.get("goto_request") or {}).get("step") or record["step"]
        if decision == "approve":
            self._tell_leader(
                record,
                f"goto request {record['id']} approved ({decided_by}): run of "
                f"{record['target_session']} moved {record.get('from')!r} -> "
                f"{asked!r}",
            )
            self._nudge_child(record, cflow_engine.nudge_for_state(str(asked)))
        else:
            self._tell_leader(
                record,
                f"goto request {record['id']} denied ({decided_by}): run of "
                f"{record['target_session']} stays at {record.get('from')!r}",
            )
            self._nudge_child(record, cflow_engine.NUDGE_GOTO_DENIED)

    def _on_timeout(self, request_id: str) -> None:
        """The deadline fired: count as approval. The settlement path clears
        the timer, so a request already answered between deadline and
        callback no longer has this handle to fire."""
        record = self.records.get(request_id)
        if record is not None and record["status"] == "pending":
            self.approve(request_id, decided_by="timeout")

    # ------------------------------------------------------------------ #
    # notifications
    # ------------------------------------------------------------------ #
    def _nudge_child(self, record: dict, message: str) -> None:
        if self._nudge is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def run():
            try:
                await self._nudge(record["cwd"], record["scope"], message)
            except Exception:  # noqa: BLE001 — a missed nudge costs a delay
                pass

        loop.create_task(run())

    def _tell_leader(self, record: dict, message: str) -> None:
        """Type the outcome into the asking leader's terminal. Best-effort:
        a dead leader reads the record on its return instead of the message
        queueing forever."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def run():
            try:
                session = self.manager.get(record["session"])
            except Exception:  # noqa: BLE001 — raced with a removal
                return
            if session.exited:
                return
            try:
                await session.deliver(
                    "goto-gate: " + message
                )
            except Exception:  # noqa: BLE001
                pass

        loop.create_task(run())

    # ------------------------------------------------------------------ #
    # teardown
    # ------------------------------------------------------------------ #
    async def cancel(self, _app=None) -> None:
        """The daemon is going down. Free the timers, and withdraw what is
        still pending on the child runs: a held run must not stay held
        behind a gate that no longer exists (see the module docstring).
        ``_app`` is what aiohttp's on_shutdown handlers are always called
        with, and the method is a coroutine because aiohttp awaits them."""
        timers, self._timers = self._timers, {}
        for timer in timers.values():
            timer.cancel()
        for record in self.records.values():
            if record["status"] != "pending":
                continue
            record["status"] = "withdrawn"
            record["decided_at"] = _utcnow()
            record["decided_by"] = "daemon-shutdown"
            try:
                cflow_engine.cancel_goto_request(
                    by="daemon-shutdown", cwd=record["cwd"], scope=record["scope"]
                )
            except Exception:  # noqa: BLE001 — the run moved on; nothing owed
                pass

    def _prune(self) -> None:
        settled = [
            r
            for r in sorted(
                self.records.values(), key=lambda r: r.get("requested_at") or ""
            )
            if r["status"] != "pending"
        ]
        for record in settled[: max(0, len(settled) - _KEEP_SETTLED)]:
            self.records.pop(record["id"], None)
