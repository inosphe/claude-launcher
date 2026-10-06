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


def pending(cwd: str, scope: Optional[str]) -> Dict[str, Any]:
    """The open asks of the run in (cwd, scope), as the status payload lists them."""
    payload = runtime.status(cwd=cwd, scope=scope or None, tick=False)
    return {"asks": payload.get("asks") or [], "status": payload.get("status")}
