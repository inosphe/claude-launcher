"""Whether a v2 run is holding its round in the plan gate's ``pause``.

The round's last step (``improv-worker-v2``의 ``settle/closure-gate``) asks the
person ``pause`` / ``skip`` / ``reject`` when the plan panel still has an
open item after its 60-second wait (claunch-umf88.1). ``pause`` re-enters an
open-ended wait, and the person asked that the session stay unaware of it:
"에이전트에게 넛지되지 않음. 에이전트도 스스로 모르게 대기. 근데 reminder 도
암시적 held 상태로 만들어줘(세션 pause 상태라 held 된것처럼). 사람이 pause
한것처럼 처리하면 복구가 안되니까 다른 경로를 타야해."

So the pause is read off the run itself — the wait node is the marker — and
not off the session's reminder pause (``session.reminders_paused``), which is
what a person's own ``claunch hold``/pause writes and what the wake and
recovery paths consult. Nothing here writes session state: while the run sits
in that wait, the repeating sources are held and the plan pane's blocks are
deferred, and the moment the wait ends both go back to normal by themselves.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..cflow2 import store as cstore

log = logging.getLogger(__name__)

#: The wait node ``improv-worker-v2`` enters when the person picks ``pause``.
#: The run's active instances name it, so this is the whole test: no flag has
#: to be written or cleared, and a run that leaves the wait is not paused the
#: next time this is asked.
PAUSE_NODE = "closure-pause-wait"


def scope_paused(cwd: Optional[str], scope: Optional[str]) -> bool:
    """Whether the run in ``(cwd, scope)`` is waiting in the plan gate's pause.

    Any failure to read the slot answers False: a pause that cannot be
    measured is not a pause, and holding a session's reminders because its
    run file was unreadable would silence it for the wrong reason.
    """
    if not cwd or not scope:
        return False
    try:
        st = cstore.RunStore(cwd, scope)
        if not st.exists():
            return False
        state = st.load_state()
    except Exception:  # noqa: BLE001 - unreadable is not paused
        log.debug("cflow2 pause: cannot read %s in %s", scope, cwd, exc_info=True)
        return False
    inst = state.get("inst") or {}
    if not isinstance(inst, dict):
        return False
    for iid in state.get("active") or []:
        rec = inst.get(iid) or {}
        if not isinstance(rec, dict):
            continue
        node = str(rec.get("node") or "")
        if node.rsplit("/", 1)[-1] == PAUSE_NODE:
            return True
    return False


def session_paused(manager, name: str) -> bool:
    """``scope_paused`` for a managed session, found by its own directory.

    The scope is the session's main slot (``<session>@<daemon>``); the store
    qualifies a bare session name to this daemon's, which is exactly the slot
    a session running here drives.
    """
    try:
        session = manager.get(name)
    except Exception:  # noqa: BLE001 - an unknown session is not paused
        return False
    if getattr(session, "exited", False):
        return False
    cwd = getattr(getattr(session, "sdef", None), "cwd", "") or ""
    return scope_paused(cwd, name)
