"""Nudges: a driver asking its person to act on a cflow run (claunch-g08xz).

A run often stops on something only a person releases -- a ``chooser: user``
choice (the worker's end decision, where a new task has to be taken in), an
ask that fell to a human, the driver's move request, a loop or visit limit, a
halt, a v1 run's ``waiting_selection``/``waiting_approval``. The driver says
so in its terminal and stops its turn, and a person who is not looking at that
terminal does not find out: the run waits for as long as nobody happens to
look.

``flow_nudge`` (cflow2/mcp.py) is the driver's way to raise it on the
dashboard's notification channel instead -- the corner card, a row in the
notification history and a place in the bell's waiting count, like the
daemon's own approval gates. This module is the daemon's half: it keeps the
requests and decides, from the run's own files, whether each one is still
waiting.

Two things keep it from becoming noise:

- A nudge is refused unless the run really waits on a person *now*
  (:func:`owed`). The list it raises is read from disk, not taken from the
  agent, so the card names what the person is actually asked to release.
- One live nudge per run slot. Asking again while the same things are owed
  within :data:`REPEAT_AFTER` changes nothing (the card is already up); after
  it, or when what is owed changed, the old request is superseded and a new
  card goes up -- also in a browser that hid the old one.

A request settles by itself the moment none of what it named is owed any more
(the person pressed something, the run moved, the run is gone), or when its
session is no longer registered. Kept in memory: a daemon restart forgets
them, and the driver's next nudge raises the card again.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

#: How long the same nudge stays one card. Asking again inside this window
#: answers with the request already up.
REPEAT_AFTER = 300.0
#: The longest message a nudge carries.
MESSAGE_MAX = 1200
#: Settled requests kept for the notification history's status words.
KEEP_SETTLED = 50

PENDING = "pending"
SETTLED = "settled"
SUPERSEDED = "superseded"
ENDED = "session ended"


class NudgeRefused(Exception):
    """A nudge that raises nothing: the run waits on no person, or the
    request is malformed. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def v1_gate_kind(entry: dict) -> str:
    """Which press settles a v1 run position, or '' when no person's does.

    The same reading as the dashboard's rail (app.js ``sessCflowGated``): an
    approval, a user-chosen branch, a goto request, or an agent question that
    reached nobody. A timer, a checklist, a window and a question still with
    another agent wait on no one here.
    """
    status = str(entry.get("status") or "")
    if status == "waiting_approval":
        return "approval"
    if status == "waiting_selection":
        return "selection"
    if status == "waiting_goto":
        return "goto"
    if status == "waiting_answer" and not ((entry.get("ask") or {}).get("asked") or []):
        return "selection" if v1_gate_options(entry) else "approval"
    return ""


def v1_gate_options(entry: dict) -> list:
    ask = entry.get("ask") or {}
    raw = ask.get("options") if ask.get("kind") == "branch" else entry.get("options")
    return [{"name": str(o.get("name")), "description": o.get("description") or ""}
            for o in raw or [] if isinstance(o, dict) and o.get("name")]


def _owed_v2(cwd: str, scope: str) -> Optional[dict]:
    from ..cflow2 import store as store2
    from . import cflow2_web

    if not store2.RunStore(cwd, scope).exists():
        return None
    entry = cflow2_web.rail_entry(cwd, scope)
    if entry is None:
        return None
    if entry.get("status") == "error":
        raise NudgeRefused(f"the v2 run in {cwd} ({scope}) cannot be read: "
                           f"{entry.get('error')}", status=409)
    return {"version": 2, "run": entry.get("run"), "workflow": entry.get("workflow"),
            "items": list(entry.get("owed") or [])}


def _owed_v1(cwd: str, scope: str) -> Optional[dict]:
    from ..cflow import engine, model, state as state_mod

    try:
        entry = engine.status(cwd, scope=scope)
    except (engine.CflowError, model.WorkflowError, state_mod.StateError, OSError):
        return None
    if entry.get("status") == "idle":
        return None
    kind = v1_gate_kind(entry)
    items = []
    if kind:
        step = entry.get("title") or entry.get("step_id") or "?"
        items.append(f"{kind}: {step}")
    return {"version": 1, "run": entry.get("run"), "workflow": entry.get("workflow"),
            "items": items}


def owed(cwd: str, scope: str) -> Optional[dict]:
    """What the run in ``(cwd, scope)`` waits on a person for:
    ``{version, run, workflow, items}``, ``items`` empty when nothing; None
    when the slot holds no run at all. A v2 run is looked for first -- a
    session drives one format at a time, and the v1 tools refuse a slot that
    holds a v2 run."""
    found = _owed_v2(cwd, scope)
    if found is not None:
        return found
    return _owed_v1(cwd, scope)


#: The option a run's end decision ends the session with (improv-worker-v2's
#: end-decision, and any workflow that names its ending the same way).
END_OPTION = "end"


def end_decision(cwd: str, scope: str) -> Optional[dict]:
    """The open decision in ``(cwd, scope)``'s run that a person answers with
    ``end`` -- an ask (improv-worker-v2's end-decision is one) or a ``chooser:
    user`` choice whose options include :data:`END_OPTION`, or a v1 run
    waiting on a selection that offers it: ``{version, instance, label}``, or
    None (claunch-g08xz.1).

    A session standing there has finished its round; ending it through that
    answer runs the workflow's own ending (the end notice, the record of who
    approved it) where a kill cuts it off."""
    from ..cflow2 import ir, runtime, store as store2

    if store2.RunStore(cwd, scope).exists():
        payload = runtime.status(cwd=cwd, scope=scope, tick=False)
        if payload.get("status") not in runtime.LIVE_STATUSES:
            return None
        for a in payload.get("asks") or []:
            if END_OPTION in {o.get("name") for o in a.get("options") or []}:
                return {"version": 2, "instance": a.get("instance"),
                        "label": a.get("label") or a.get("node")}
        for c in payload.get("choices") or []:
            if (c.get("chooser") or ir.CHOOSER_AGENT) != ir.CHOOSER_USER:
                continue
            if END_OPTION in {o.get("name") for o in c.get("options") or []}:
                return {"version": 2, "instance": c.get("instance"),
                        "label": c.get("label") or c.get("node")}
        return None
    from ..cflow import engine, model, state as state_mod

    try:
        entry = engine.status(cwd, scope=scope)
    except (engine.CflowError, model.WorkflowError, state_mod.StateError, OSError):
        return None
    if v1_gate_kind(entry) != "selection":
        return None
    if END_OPTION not in {o["name"] for o in v1_gate_options(entry)}:
        return None
    return {"version": 1, "instance": entry.get("step_id"),
            "label": entry.get("title") or entry.get("step_id")}


def end_request_reply(target: str, decision: dict, result: dict) -> Dict[str, Any]:
    """What an agent's kill of a session standing on its end decision answers
    instead of filing a kill request (claunch-g08xz.1)."""
    req = result.get("request") or {}
    return {
        "pending": True, "kill_request": None, "end_requested": True,
        "target": target, "decision": decision.get("label"), "nudge": req.get("id"),
        "note": (
            f"{target} stands on its end decision ({decision.get('label')}), so no kill "
            f"request was filed: the person is asked on the dashboard to answer "
            f"'{END_OPTION}' there (nudge {req.get('id')}), which ends the session through "
            f"its workflow's own ending. Nothing has been ended yet and nothing is typed "
            f"back to you: the session's exit is the answer (its mesh row reads exited). "
            f"Do not ask again. To file a plain kill request anyway, call kill again with "
            f"skip_end_decision: true"
        ),
    }


class NudgeBoard:
    """The daemon's nudge requests. ``read_owed`` is :func:`owed`, injectable
    for tests; ``alive`` answers whether a session is still registered."""

    def __init__(self, *, read_owed: Callable[[str, str], Optional[dict]] = owed,
                 alive: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._read_owed = read_owed
        self._alive = alive
        self._clock = clock
        self._lock = threading.Lock()
        self._records: Dict[str, dict] = {}
        self._ids = itertools.count(1)

    def _new_id(self) -> str:
        return f"nudge-{int(self._clock())}-{next(self._ids)}"

    def _live_for(self, cwd: str, scope: str) -> Optional[dict]:
        for rec in self._records.values():
            if rec["status"] == PENDING and rec["cwd"] == cwd and rec["scope"] == scope:
                return rec
        return None

    def _close(self, rec: dict, status: str, now: float) -> None:
        rec["status"] = status
        rec["closed"] = now

    def _prune(self) -> None:
        settled = sorted((r for r in self._records.values() if r["status"] != PENDING),
                         key=lambda r: r.get("closed") or 0)
        for rec in settled[:max(0, len(settled) - KEEP_SETTLED)]:
            self._records.pop(rec["id"], None)

    def submit(self, *, session: str, cwd: str, scope: str, message: str,
               by: Optional[str] = None) -> dict:
        """File a nudge for ``session``'s run in ``(cwd, scope)``. Returns
        ``{raised, request, note}``; raises :class:`NudgeRefused` when the run
        waits on nobody."""
        message = " ".join(str(message or "").split())
        if not message:
            raise NudgeRefused("'message' is required: one or two lines telling the person "
                               "what to decide and where (the card shows it)", status=400)
        if len(message) > MESSAGE_MAX:
            message = message[:MESSAGE_MAX - 1] + "…"
        found = self._read_owed(cwd, scope)
        if found is None:
            raise NudgeRefused(f"no cflow run in {cwd} for {scope!r}: there is nothing "
                               f"for a person to act on")
        items = list(found.get("items") or [])
        if not items:
            raise NudgeRefused(
                "the run waits on no person right now (no user choice, no ask that fell "
                "to a human, no move request, loop/visit limit or halt; for v1 no "
                "waiting_selection/approval/goto) -- a nudge would ask them for nothing. "
                "Carry on with the run; if you need the run moved somewhere its route "
                "does not lead, file flow_request_move first, then nudge")
        now = self._clock()
        with self._lock:
            live = self._live_for(cwd, scope)
            if live is not None and live["items"] == items \
                    and now - live["opened"] < REPEAT_AFTER:
                live["message"] = message
                live["asked"] = live.get("asked", 1) + 1
                return {"raised": False, "request": dict(live),
                        "note": (f"the same nudge is already on the dashboard (since "
                                 f"{int(now - live['opened'])}s ago); its message is "
                                 f"updated. Stop your turn and wait -- asking again does "
                                 f"not reach the person sooner")}
            if live is not None:
                self._close(live, SUPERSEDED, now)
            rec = {
                "id": self._new_id(), "session": session, "cwd": cwd, "scope": scope,
                "version": found.get("version"), "run": found.get("run"),
                "workflow": found.get("workflow"), "items": items, "message": message,
                "status": PENDING, "opened": now, "closed": None, "asked": 1,
                "by": by or session,
            }
            self._records[rec["id"]] = rec
            self._prune()
        log.info("nudge %s: %s asks a person to act on %s", rec["id"], session, items)
        return {"raised": True, "request": dict(rec),
                "note": ("raised on the dashboard: a card, a row in the notification history "
                         "and the bell's waiting count. It settles by itself when what it "
                         "names is no longer owed. Stop your turn and wait; the person's "
                         "answer moves the run")}

    def list(self) -> List[dict]:
        """Every request, newest first, the pending ones re-checked against
        their run (call off the event loop: it reads run files)."""
        with self._lock:
            pending = [dict(r) for r in self._records.values() if r["status"] == PENDING]
        verdicts: Dict[str, str] = {}
        for rec in pending:
            if self._alive is not None and not self._alive(rec["session"]):
                verdicts[rec["id"]] = ENDED
                continue
            try:
                found = self._read_owed(rec["cwd"], rec["scope"])
            except Exception:  # noqa: BLE001 - an unreadable run keeps its card
                log.exception("nudge %s: re-check failed", rec["id"])
                continue
            still = set((found or {}).get("items") or [])
            if found is None or found.get("run") != rec.get("run") \
                    or not still.intersection(rec["items"]):
                verdicts[rec["id"]] = SETTLED
        now = self._clock()
        with self._lock:
            for rid, status in verdicts.items():
                rec = self._records.get(rid)
                if rec is not None and rec["status"] == PENDING:
                    self._close(rec, status, now)
            self._prune()
            out = [dict(r) for r in self._records.values()]
        out.sort(key=lambda r: r.get("opened") or 0, reverse=True)
        return out


def reply(result: dict) -> Dict[str, Any]:
    """The MCP-facing shape of :meth:`NudgeBoard.submit`'s answer."""
    req = result.get("request") or {}
    return {"raised": bool(result.get("raised")), "request": req.get("id"),
            "owed": req.get("items") or [], "note": result.get("note")}
