"""The automatic key switch: move a usage-limited session onto another key.

A session under an exhausted usage plan (:mod:`session_fault`, kind
``usage_limit``) normally just stops: :mod:`fault_pause` pauses it until the
plan resets, which is honest and, when the profile holds one key, the only
thing that can be done. A *multi-key* profile has a second answer -- another
key of the same pool may still have plan left -- and this clock takes it:

* only a session whose own definition asks for it
  (:attr:`store`-independent ``SessionDef.auto_key_switch``) is switched. The
  answer is the session's, given at creation or in the key modal, because a
  restart is a visible thing to do to somebody's session;
* the key is chosen by ``profile_keys.switch_candidate`` -- the operator's
  fixed rule (claunch-d2t79): the usable key with the lowest peak usage
  across the 5-hour, weekly and monthly windows, never one that is already
  spent and never the one the session is on;
* after the relaunch the session is told ``continue``, because a resumed
  conversation that is left alone waits for a person and the whole point is
  that nobody is there (:data:`CONTINUE`);
* a session gets ``daemon.fault_key_switch_retries`` attempts per fault, one
  by default. The count lives in the session directory (:data:`RECORD`),
  keyed by the fault's own reset deadline, so it survives a daemon restart --
  the same lesson claunch-ijot0.1 records for the pause clock, which kept its
  set in memory and paused a resumed session again after a restart.

When there is nothing to switch to -- no second key, every other key spent,
or the attempts used up -- this clock does nothing and returns the session to
the pause it would have had before the feature existed. That fallback is the
whole contract: the switch may only *improve* on the pause, never replace it
with an endless restart loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from .. import store
from . import paths

log = logging.getLogger(__name__)

#: What is typed into the session once it is back. Not a fenced notice: the
#: point is to look exactly like a person who was watching type "continue".
CONTINUE = "continue"

#: The file in the session directory holding this fault's attempt count.
RECORD = "key-switch.json"

#: How long an "nothing usable to switch to" answer stands before the pool is
#: read again. The pass runs every 30 seconds and a usage limit lasts hours or
#: days, so without this a session nobody can rescue would ask the provider
#: about every key of its pool twice a minute for a week. Ten minutes is short
#: enough that a window which resets is noticed while the session still
#: matters, and long enough to keep the ask off the provider's own rate limit.
RECONSULT = 600.0


def retries(cfg: Optional[dict] = None) -> int:
    """How many switches one session may spend on one fault (0 = off)."""
    cfg = store.daemon_config() if cfg is None else cfg
    try:
        value = int(cfg.get("fault_key_switch_retries", 1))
    except (TypeError, ValueError):
        return 1
    return max(0, value)


def read_state(name: str) -> dict:
    """The switch record for ``name``, or an empty one.

    A record that cannot be parsed reads as empty rather than failing the
    pass: the worst case is one extra attempt after a corrupt write, and a
    clock that raises on one session's file stops switching every other one.
    """
    try:
        data = json.loads((paths.session_dir(name) / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(name: str, state: dict) -> None:
    try:
        target = paths.session_dir(name) / RECORD
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("key switch: could not record the state of %r", name, exc_info=True)


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _fault_key(record: dict) -> str:
    """What identifies *this* fault for the attempt count.

    The reset deadline when the harness stated one, else the moment the
    fault was first read. Two different faults of the same session must not
    share a count, or the second one starts with no attempts left.
    """
    return str(record.get("until") or record.get("since") or "")


def _candidate(session):
    """``profile_keys.switch_candidate`` for this session (blocking I/O)."""
    from .. import profile_keys
    from .. import profile as profile_mod

    sdef = session.sdef
    if not sdef.profile:
        return profile_keys.SwitchCandidate(
            None, None, "the session has no profile, so it reads no key pool"
        )
    selector = sdef.borrow or sdef.profile
    prof = profile_mod.require_selector(selector)
    return profile_keys.switch_candidate(
        prof, current_key_id=getattr(session, "launched_key", None) or sdef.key_id
    )


async def sweep(manager, cfg: Optional[dict] = None) -> List[str]:
    """Switch what can be switched now; the sessions switched.

    Runs before :func:`fault_pause.sweep` in the same pass, so a session this
    function handled is already relaunched (and reads no fault) by the time
    the pause looks at it.
    """
    limit = retries(cfg)
    if limit <= 0:
        return []
    from .fault_pause import fault_to_pause
    from . import session_input

    out: List[str] = []
    for session in list(manager.list()):
        name = session.sdef.name
        try:
            if not getattr(session.sdef, "auto_key_switch", False):
                continue
            if getattr(session.sdef, "null_token", False):
                # No token is injected at all, so there is no key to move to;
                # ``set_key`` refuses these and the pause is the only answer.
                continue
            record = fault_to_pause(session)
            if record is None:
                continue
            fault_key = _fault_key(record)
            state = read_state(name)
            if state.get("until") != fault_key:
                state = {"until": fault_key, "attempts": 0, "keys": []}
            if int(state.get("attempts") or 0) >= limit:
                continue
            checked = state.get("checked_at")
            if (
                state.get("reason")
                and isinstance(checked, (int, float))
                and _now() - checked < RECONSULT
            ):
                continue
            candidate = await asyncio.to_thread(_candidate, session)
            if candidate.key_id is None:
                state["reason"] = candidate.reason
                state["readings"] = list(candidate.readings)
                state["checked_at"] = _now()
                write_state(name, state)
                continue
            # Written BEFORE the restart: a crash in between must cost one
            # attempt, not loop the session through restarts forever.
            state["attempts"] = int(state.get("attempts") or 0) + 1
            state["keys"] = list(state.get("keys") or []) + [candidate.key_id]
            state["reason"] = ""
            state["readings"] = list(candidate.readings)
            state["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_state(name, state)
            await manager.set_key(name, candidate.key_id)
        except Exception:  # noqa: BLE001 -- one session must not stop the pass
            log.exception("key switch of %r failed", name)
            continue
        # The relaunch succeeded; the rest is courtesy that must not undo it.
        try:
            resumed = manager.get(name)
            await resumed.await_input_ready()
            await session_input.type_line(resumed, CONTINUE)
        except Exception:  # noqa: BLE001 -- a session that cannot be told is
            log.warning(  # still switched, and the person sees it on the rail
                "key switch: %r was switched but 'continue' did not go in", name,
                exc_info=True,
            )
        out.append(name)
    if out:
        log.info("key switch (usage limit): %s", ", ".join(out))
    return out
