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

The dashboard now raises a card for every such gate by itself, with its
answers, from the cflow polls (claunch-whm4e.1, app.js ``runGateSync``). A
nudge for a run that has that card puts up no second one: its message is
shown on the run's card and it leaves the waiting count. A nudge with no
such card (a run whose session the dashboard does not list) is drawn as
before.

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

#: An emergency nudge (claunch-8myyt) has no :func:`owed` list to name -- the
#: run may not wait on anyone, or not exist -- so its one item is this word.
EMERGENCY = "emergency"
#: The shortest gap between two emergency calls of one session. A loop that
#: calls the tool every turn would otherwise hammer the dashboard; the card is
#: one per run slot either way, so the extra calls carried nothing.
EMERGENCY_MIN_INTERVAL = 60.0

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


#: A v2 limit's fixed presses (``/api/cflow2/extend``), as the run page and
#: the terminal strip offer them (app.js ``cf2LimitPresses``). A loop limit's
#: two raises are not here: they scale with the loop's ``max`` (see
#: :func:`loop_raise_steps`), which only a loop answer carries.
_V2_LIMIT_PRESSES = {
    "visit_limit": (("Allow one more visit", {"by": 1, "visits": True}),
                    ("Stop here", {"stop": True, "visits": True})),
}

#: A loop limit's raises, as a share of the loop's ``max``: a fifth and three
#: fifths, floored, at least one each, never the same twice. A 25-max loop
#: keeps the 5 / 15 presses it has had (claunch-n6ms0); the await-landing loop
#: at max 5 gets 1 / 3, because a 15 there would open three times the loop's
#: own ceiling (claunch-gpgw1, user decision 2026-10-30).
LOOP_RAISE_FALLBACK = (5, 15)


def loop_raise_steps(max_n) -> tuple:
    """The two ``by`` amounts a loop limit's presses offer for a loop whose
    ``max`` is ``max_n``. A missing or unusable ``max`` falls back to the old
    fixed pair, so a payload written before the field existed still offers
    usable presses."""
    try:
        n = int(max_n)
    except (TypeError, ValueError):
        return LOOP_RAISE_FALLBACK
    if n < 1:
        return LOOP_RAISE_FALLBACK
    small = max(1, n // 5)
    return small, max(small + 1, (3 * n) // 5)


def _loop_presses(max_n) -> tuple:
    """A loop limit's presses: two raises scaled to its ``max``, then stop."""
    return tuple((f"Allow {by} more", {"by": by})
                 for by in loop_raise_steps(max_n)) + (("Stop the loop", {"stop": True}),)


def v2_presses(entry: dict, answer: dict) -> list:
    """The presses that settle one of a v2 rail row's ``answers``
    (cflow2_web ``_rail_answers``): ``{label, path, body}`` each, the same
    endpoint and body the terminal strip posts (app.js ``termCflow2Answers``).
    ``reason_required`` marks the press that needs text from the person:
    ``reframe``, as on the strip. ``require_reason`` is the agent's
    constraint -- a person may answer without it (docs 2.11), so it marks
    nothing. ``reason_optional`` marks one whose reason the driver reads. A
    halt and an acceptance hold have none -- the dashboard has no endpoint
    for them."""
    slot = {"cwd": entry.get("cwd"), "scope": entry.get("scope")}
    kind = answer.get("kind")
    if kind == "move":
        return [
            {"label": (f"Report '{answer.get('outcome')}'" if answer.get("report")
                       else f"Move to '{answer.get('label')}'"),
             "path": "/api/cflow2/move/resolve",
             "body": {**slot, "decision": "approve", "reason": ""}},
            {"label": "Refuse", "path": "/api/cflow2/move/resolve",
             "body": {**slot, "decision": "deny", "reason": ""}, "reason_optional": True},
        ]
    body = {**slot, "node": answer.get("instance")}
    if kind in ("ask", "choose"):
        out = []
        for o in answer.get("options") or []:
            name = o.get("name") if isinstance(o, dict) else str(o)
            if not name:
                continue
            press = ({"label": name, "path": "/api/cflow2/approve",
                      "body": {**body, "decision": name, "reason": ""}} if kind == "ask" else
                     {"label": name, "path": "/api/cflow2/select",
                      "body": {**body, "option": name, "reason": ""}})
            if kind == "ask" and name == "reframe":
                press["reason_required"] = True
            if kind == "ask" and answer.get("recommended") == name:
                press["recommended"] = True
            out.append(press)
        return out
    presses = (_loop_presses(answer.get("max")) if kind == "loop_limit"
               else _V2_LIMIT_PRESSES.get(kind))
    if presses:
        return [{"label": label, "path": "/api/cflow2/extend", "body": {**body, **extra}}
                for label, extra in presses]
    return []


def v2_gates(entry: dict) -> list:
    """The person gates of one v2 rail row (cflow2_web ``rail_entry``), one
    per ``answers`` item: ``kind`` (ask, choose, move, loop_limit,
    visit_limit, halt, acceptance), the question, the options and the
    presses (:func:`v2_presses`); ``ask_id`` is an ask's id and ``request``
    a move request's. Empty when the run waits on no person."""
    if not entry or not entry.get("gated"):
        return []
    out = []
    for a in entry.get("answers") or []:
        kind = a.get("kind") if isinstance(a, dict) else None
        if not kind:
            continue
        if kind == "loop_limit":
            prompt = f"loop reached its max ({a.get('count')}/{a.get('max')})"
        elif kind == "visit_limit":
            prompt = f"reached the run's visit limit ({a.get('count')}/{a.get('max')})"
        else:
            prompt = a.get("prompt") or ""
        out.append({
            "kind": kind, "instance": a.get("instance"), "ask_id": a.get("ask"),
            "request": a.get("request"), "label": a.get("label") or a.get("instance") or kind,
            "prompt": prompt, "urgent": bool(a.get("urgent")),
            # A choice the run is not stopped on (claunch-hbmhk): carried so
            # a reader of these gates can tell a door from a stop.
            "optional": bool(a.get("optional")),
            "criteria": [str(c) for c in (a.get("criteria") or [])],
            "options": [{"name": o.get("name"), "description": o.get("description") or ""}
                        for o in a.get("options") or [] if isinstance(o, dict)],
            "presses": v2_presses(entry, a),
        })
    return out


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
            # A nudge names what the person can still do about this run, and
            # an optional door counts: it is open now and closes by itself,
            # which is exactly what asking them to look at it is about. What
            # is NOT optional is a stop, and ``owed`` holds those alone
            # (claunch-hbmhk).
            "items": (list(entry.get("owed") or [])
                      + list(entry.get("optional") or []))}


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
            f"skip_end_decision: true (CLI: --skip-end-decision); a forced kill files it "
            f"as well"
        ),
    }


def position(cwd: str, scope: str) -> Optional[tuple]:
    """Where the run in ``(cwd, scope)`` stands, as a comparable key, or None
    when the slot holds no run. An emergency nudge settles when this changes:
    the run moved, so whatever stopped it is no longer what the card says.
    v2: the run, its status and the open tasks, choices and asks; v1: the run,
    its status and its step."""
    from ..cflow2 import runtime, store as store2

    if store2.RunStore(cwd, scope).exists():
        payload = runtime.status(cwd=cwd, scope=scope, tick=False)
        inst = lambda rows: tuple(sorted(str(r.get("instance")) for r in rows or []))  # noqa: E731
        return ("v2", payload.get("run"), payload.get("status"), inst(payload.get("tasks")),
                inst(payload.get("choices")), inst(payload.get("asks")))
    from ..cflow import engine, model, state as state_mod

    try:
        entry = engine.status(cwd, scope=scope)
    except (engine.CflowError, model.WorkflowError, state_mod.StateError, OSError):
        return None
    if entry.get("status") == "idle":
        return None
    return ("v1", entry.get("run"), entry.get("status"), entry.get("step_id"))


class NudgeBoard:
    """The daemon's nudge requests. ``read_owed`` is :func:`owed`, injectable
    for tests; ``alive`` answers whether a session is still registered."""

    def __init__(self, *, read_owed: Callable[[str, str], Optional[dict]] = owed,
                 alive: Optional[Callable[[str], bool]] = None,
                 clock: Callable[[], float] = time.time,
                 read_position: Callable[[str, str], Optional[tuple]] = position) -> None:
        self._read_owed = read_owed
        self._read_position = read_position
        self._emergency_at: Dict[str, float] = {}
        self._alive = alive
        self._clock = clock
        self._lock = threading.Lock()
        self._records: Dict[str, dict] = {}
        self._ids = itertools.count(1)

    def _new_id(self) -> str:
        return f"nudge-{int(self._clock())}-{next(self._ids)}"

    def _live_for(self, cwd: str, scope: str, kind: str = "flow") -> Optional[dict]:
        for rec in self._records.values():
            if rec["status"] == PENDING and rec["cwd"] == cwd and rec["scope"] == scope \
                    and rec.get("kind", "flow") == kind:
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

    def emergency(self, *, session: str, cwd: str, scope: str, message: str = "",
                  cancel: bool = False, by: Optional[str] = None) -> dict:
        """File, update or withdraw ``session``'s emergency nudge
        (claunch-8myyt). Unlike :meth:`submit` it asks nothing of the run:
        the run may wait on no person (an auto-mode classifier denial stops
        the agent inside a step) or not exist, and the person is still needed.

        One card per run slot, kept apart from the ``flow_nudge`` card. A
        second call changes the card's message and ``updated`` time and
        nothing else, so the notification history gets no new row. It settles
        when the run's position changes, the session ends, a person resolves
        it (:meth:`resolve`) or the agent withdraws it (``cancel``). A
        session's calls are spaced at least :data:`EMERGENCY_MIN_INTERVAL`
        apart (429 inside it); a withdrawal is not limited."""
        now = self._clock()
        if cancel:
            with self._lock:
                live = self._live_for(cwd, scope, EMERGENCY)
                if live is None:
                    return {"raised": False, "request": None,
                            "note": "no emergency nudge of yours is up"}
                self._close(live, SETTLED, now)
                live["withdrawn"] = True
            return {"raised": False, "request": dict(live),
                    "note": "the emergency nudge is withdrawn"}
        message = " ".join(str(message or "").split())
        if not message:
            raise NudgeRefused("'message' is required: one or two lines telling the person "
                               "what to decide and your recommendation", status=400)
        if len(message) > MESSAGE_MAX:
            message = message[:MESSAGE_MAX - 1] + "…"
        with self._lock:
            last = self._emergency_at.get(session)
            if last is not None and now - last < EMERGENCY_MIN_INTERVAL:
                wait = int(EMERGENCY_MIN_INTERVAL - (now - last)) + 1
                raise NudgeRefused(
                    f"an emergency nudge from {session} was sent {int(now - last)}s ago; "
                    f"wait {wait}s. The card is already up -- repeating it sooner does not "
                    f"reach the person sooner", status=429)
        try:
            where = self._read_position(cwd, scope)
        except Exception:  # noqa: BLE001 - an unreadable run still gets its card
            log.exception("emergency nudge: position unreadable for %s", scope)
            where = None
        with self._lock:
            self._emergency_at[session] = now
            live = self._live_for(cwd, scope, EMERGENCY)
            if live is not None:
                live["message"] = message
                live["updated"] = now
                live["asked"] = live.get("asked", 1) + 1
                return {"raised": False, "request": dict(live),
                        "note": (f"the emergency card is already up (since "
                                 f"{int(now - live['opened'])}s ago); its message is updated "
                                 f"and no new notification row is added. Call again about every "
                                 f"20 minutes while the wait lasts")}
            rec = {
                "id": self._new_id(), "session": session, "cwd": cwd, "scope": scope,
                "kind": EMERGENCY, "version": (where[0] == "v2" and 2 or 1) if where else None,
                "run": where[1] if where else None, "workflow": None,
                "items": [EMERGENCY], "position": where, "message": message,
                "status": PENDING, "opened": now, "updated": now, "closed": None,
                "asked": 1, "by": by or session,
            }
            self._records[rec["id"]] = rec
            self._prune()
        log.info("emergency nudge %s: %s asks a person for help", rec["id"], session)
        return {"raised": True, "request": dict(rec),
                "note": ("raised on the dashboard: a card, a row in the notification history "
                         "and the bell's waiting count. It settles when the run moves, the "
                         "person resolves it or you withdraw it (cancel). Stop your turn and "
                         "wait; call again about every 20 minutes with the current message")}

    def resolve(self, nudge_id: str) -> dict:
        """A person's answer to a pending nudge: close it as settled."""
        with self._lock:
            rec = self._records.get(str(nudge_id))
            if rec is None:
                raise NudgeRefused(f"no nudge {nudge_id!r}", status=404)
            if rec["status"] != PENDING:
                raise NudgeRefused(f"nudge {nudge_id} is already {rec['status']}", status=409)
            self._close(rec, SETTLED, self._clock())
            rec["resolved"] = True
            return dict(rec)

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
                if rec.get("kind") == EMERGENCY:
                    moved = rec.get("position") is not None \
                        and self._read_position(rec["cwd"], rec["scope"]) != rec["position"]
                    if moved:
                        verdicts[rec["id"]] = SETTLED
                    continue
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
