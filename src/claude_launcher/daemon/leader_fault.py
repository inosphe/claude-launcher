"""Leader faults: a session the harness stopped while it led a mesh.

:mod:`session_fault` reads the fault off a session's screen and
:mod:`session_fault_api` lists the whole fleet. Neither asks *who* the
faulted session is. A stopped worker costs one branch; a stopped leader
holds every landing request, integration and review that the mesh routes
through it, and the project stands still. The rail shows a small pill on that
one row, which is easy to miss when a person is looking at another session --
and a usage limit that lasts a day is not something to find out about by
scrolling.

So this module answers one question for the dashboard's fatal notification:
**which leader-role sessions are stopped right now, and still meant to be
running?** Three readings make a row:

- **the role** -- the session is a member holding ``leader`` on a mesh this
  daemon knows (``Member.roles``: the primary role or a subrole). A handle
  whose role belongs to a member on another machine resolves to no local
  session, so it contributes nothing: a fault is read off this machine's
  screen.
- **the state** -- the session's category is running or hold. Hold is a
  stopped process whose turn is kept, and its work is still owed, so it is
  alerted like a running one. Paused, killed and archived sessions were
  stopped on purpose -- the project is not waiting on them -- and they raise
  nothing.
- **the fault** -- the session's fault record is active: a usage limit that
  has not reset, an API error inside its window, a prompt waiting for a key.

The list is read on every call from the sessions and the mesh roster, so
nothing is cached and a cleared fault disappears on the next poll. That
mirrors :func:`session_fault_api.rows`: the reading is the answer.
"""

from __future__ import annotations

import json
from typing import List

from aiohttp import web

from .session import CATEGORY_HOLD, CATEGORY_RUNNING, session_category

#: The role that makes a fault an operator-level one.
LEADER = "leader"

#: The categories whose leader fault still stops the project. ``hold`` is a
#: session whose process is not running but whose conversation, run and work
#: are kept: the work is still owed, so the project is still waiting on it.
ALERT_CATEGORIES = (CATEGORY_RUNNING, CATEGORY_HOLD)

#: The fault fields a card reads. Kept explicit so a later field on the
#: detector's view cannot reach the page by accident.
FAULT_FIELDS = (
    "kind", "label", "action", "message", "harness", "since", "last_seen",
    "until", "reset", "resolved_by", "code", "strikes", "active",
)


def leaders(mesh_manager) -> List[dict]:
    """The local sessions holding ``leader`` on some mesh, one row each.

    ``meshes`` lists every mesh that gives the session the role, because two
    meshes can both route through it and a person answering the card needs to
    know which. A handle with no session name is not a session to look up.
    """
    rows = {}
    for mesh in mesh_manager.list():
        for handle in sorted(mesh.members):
            member = mesh.members[handle]
            if LEADER not in getattr(member, "roles", ()):
                continue
            session = str(getattr(member, "session", "") or "")
            if not session:
                continue
            row = rows.get(session)
            if row is None:
                row = rows[session] = {
                    "session": session,
                    "handle": str(getattr(member, "handle", "") or ""),
                    "meshes": [],
                }
            if mesh.name not in row["meshes"]:
                row["meshes"].append(mesh.name)
    return list(rows.values())


def alerts(manager, mesh_manager=None) -> List[dict]:
    """The leader-role sessions stopped right now, one row per session.

    A member whose session lives on another machine, whose fault record has
    expired, or whose category is paused/killed/archived contributes nothing.
    An unreadable mesh store answers an empty list rather than an error: the
    route is polled by the dashboard, and a broken roster should not turn the
    page's poll into a failure.
    """
    if mesh_manager is None:
        return []
    try:
        rows = leaders(mesh_manager)
    except Exception:  # noqa: BLE001 - a roster we cannot read is not an alert
        return []
    out: List[dict] = []
    for row in rows:
        try:
            session = manager.get(row["session"])
        except Exception:  # noqa: BLE001 - the member is on another machine
            continue
        try:
            category = session_category(session)
        except Exception:  # noqa: BLE001 - an unreadable record cannot be alerted
            continue
        if category not in ALERT_CATEGORIES:
            continue
        fault = getattr(session, "fault", None)
        try:
            view = fault() if callable(fault) else None
        except Exception:  # noqa: BLE001
            view = None
        if not isinstance(view, dict) or not view.get("active"):
            continue
        out.append({
            **row,
            "category": category,
            "fault": {k: view.get(k) for k in FAULT_FIELDS if k in view},
        })
    out.sort(key=lambda r: r["session"])
    return out


def install(app) -> None:
    async def h_leader_faults(request: web.Request) -> web.Response:
        rows_out = alerts(request.app["manager"], request.app.get("mesh"))
        return web.json_response(
            {"alerts": rows_out, "count": len(rows_out)},
            dumps=lambda d: json.dumps(d, ensure_ascii=False),
        )

    app.router.add_get("/api/leader-faults", h_leader_faults)
