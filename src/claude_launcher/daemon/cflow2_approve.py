"""HTTP handler body for answering a cflow v2 ask from the web UI.

Route to wire (``daemon/api.py``)::

    POST /api/cflow2/approve
    body: {"cwd": str, "scope": str,
           "node": str?,                 # task instance id or name; omitted = the only open ask
           "decision": "approve" | "decline" | <option name>?,   # default approve
           "reason": str?}
    200: the run's status payload (runtime.describe) plus "answered"
    400: {"error": ...} for a FlowError / StoreError (no such ask, bad decision)

The handler is a person's channel -- the same authority as ``claunch cflow2
approve`` (v1's dashboard Approve). It takes the run lock and, when the
question was out with responder sessions, posts a mesh "fyi" telling them it
is closed and nudges the run's session through this daemon's own API, so it
must run off the event loop: ``await asyncio.to_thread(approve, cwd, scope,
body)``.

``node: "acceptance"`` accepts a finish whose acceptance checks failed.

``extend`` answers a run's limit the same way (the person's side of
``claunch cflow2 extend``)::

    POST /api/cflow2/extend
    body: {"cwd": str, "scope": str, "node": str?,
           "kind": "loop" | "visits",   # default loop
           "by": int?,                  # default 1
           "stop": bool?}
    200: the run's status payload; 400: {"error": ...}

``resume`` releases a halting monitor (``claunch cflow2 resume``)::

    POST /api/cflow2/resume   body: {"cwd", "scope", "monitor": str?}

``pending`` lists the open asks of a run for a UI that wants them without the
whole status payload: ``GET /api/cflow2/asks?cwd&scope`` -> ``{"asks": [...]}``
(each entry is the status payload's ``asks`` item).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..cflow2 import asks, runtime
from ..cflow2 import store as cstore

ERRORS = (runtime.FlowError, cstore.StoreError)


def approve(cwd: str, scope: Optional[str], args: Dict[str, Any], *,
            by: str = "user",
            withdraw_fn: Optional[Callable[..., Any]] = None,
            nudge_fn: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Apply a person's answer to an open ask of the run in (cwd, scope)."""
    args = dict(args or {})
    if not cwd:
        raise runtime.FlowError("cwd is required")
    decision = str(args.get("decision") or "").strip() or None
    decline = bool(args.get("decline"))
    if decision == runtime.ASK_DECLINE:
        decision, decline = None, True
    elif decision == runtime.ASK_APPROVE:
        decision = None
    node = str(args.get("node") or "").strip() or None
    reason = args.get("reason")
    if reason is not None:
        reason = str(reason)
    return asks.human_approve(node, decision=decision, decline=decline, reason=reason,
                              by=by, cwd=cwd, scope=scope or None,
                              withdraw_fn=withdraw_fn, nudge_fn=nudge_fn)


def extend(cwd: str, scope: Optional[str], args: Dict[str, Any]) -> Dict[str, Any]:
    """Answer a loop limit or a visit limit as a person."""
    args = dict(args or {})
    if not cwd:
        raise runtime.FlowError("cwd is required")
    node = str(args.get("node") or "").strip() or None
    try:
        by = int(args.get("by") if args.get("by") is not None else 1)
    except (TypeError, ValueError):
        raise runtime.FlowError("'by' must be an integer")
    stop = bool(args.get("stop"))
    kind = str(args.get("kind") or "loop").strip()
    if kind not in ("loop", "visits"):
        raise runtime.FlowError("'kind' is loop or visits")
    fn = runtime.extend_visits if kind == "visits" else runtime.extend_loop
    return fn(node, by, stop=stop, cwd=cwd, scope=scope or None)


def resume(cwd: str, scope: Optional[str], args: Dict[str, Any]) -> Dict[str, Any]:
    """Release a halting monitor as a person."""
    if not cwd:
        raise runtime.FlowError("cwd is required")
    monitor = str((args or {}).get("monitor") or "").strip() or None
    return runtime.release_halt(monitor, cwd=cwd, scope=scope or None)


def pending(cwd: str, scope: Optional[str]) -> Dict[str, Any]:
    """The open asks of the run in (cwd, scope), as the status payload lists them."""
    payload = runtime.status(cwd=cwd, scope=scope or None, tick=False)
    return {"asks": payload.get("asks") or [], "status": payload.get("status")}
