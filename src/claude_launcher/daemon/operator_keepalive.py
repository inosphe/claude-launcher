"""Operator sessions always carry keep-alive (claunch-higqx).

An operator session is the user's standing window onto the fleet: its run
parks on purpose between passes, and its value is the conversation it has
built up. Both automatic endings would drop that context -- the idle hold
(:mod:`hold`) ends its program after an hour of quiet, and kill-on-end
(:mod:`cflow_clock`) terminates it when its run finishes. ``keep_alive`` is
the one flag both of them read, so this module keeps it set on every operator
session instead of teaching either clock a second exemption.

Which sessions are operators is :func:`resume.operator_sessions`'s answer:
bound to an Operator v2 record, or holding the ``operator`` role on a mesh.
The flag is applied where the daemon can see a new operator appear -- right
after an Operator v2 start binds its session, once at daemon start, and at
the top of every idle-hold pass (which also catches a mesh role given after
the session was created). A user who clears the flag on an operator session
finds it set again on the next pass: "always" is the request.
"""

from __future__ import annotations

import logging
from typing import List

from . import resume

log = logging.getLogger(__name__)


def _candidate(session) -> bool:
    """Live, or held (a held record comes back on its first use)."""
    if getattr(session, "kill_requested", False):
        return False
    return not session.exited or bool(getattr(session, "held_at", None))


def ensure(manager, *, operators=None, mesh_mgr=None) -> List[str]:
    """Set ``keep_alive`` on every operator session that lacks it; the names
    changed. Never raises: an unreadable reader means no change this pass."""
    try:
        missing = [
            s.sdef.name for s in manager.list()
            if _candidate(s) and not getattr(s.sdef, "keep_alive", False)
        ]
        names = resume.operator_sessions(
            missing, operators=operators, mesh_mgr=mesh_mgr
        )
    except Exception:  # noqa: BLE001 -- the callers are clocks and a route
        log.exception("operator keep-alive: sessions unreadable")
        return []
    changed: List[str] = []
    for name in names:
        try:
            manager.set_keep_alive(name, True)
        except Exception:  # noqa: BLE001 -- one session must not stop the rest
            log.exception("operator keep-alive of %r failed", name)
            continue
        changed.append(name)
    if changed:
        log.info("operator keep-alive set: %s", ", ".join(changed))
    return changed
