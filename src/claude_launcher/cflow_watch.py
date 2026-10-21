"""Watching another session's cflow run: its state, and what a watcher has seen.

Two run formats share one shape here:

- cflow v2 slot ``<cwd>/.cflow2/runs/<scope>`` (:class:`..cflow2.store.RunStore`)
- cflow v1 slot ``<cwd>/.cflow/runs/<scope>`` (:mod:`..cflow.state`, ``engine.status``)

``slot`` answers "where is this run now" and returns a cursor; ``events_since``
answers "what moved since that cursor". The cursor is ``"<run id>:<count>"``:
the run id fences it so a cursor from a finished run is never applied to the
next run that reuses the slot, and the count is the journal position both
formats already keep (:func:`..cflow2.store.RunStore.journal`,
:func:`..journal.read`).

This module is pure reading and composes no delivery; the daemon owns the
subscription record and the terminal write (:mod:`..daemon.cflow_watch`).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from . import journal as journal_mod
from .cflow import state as v1_state


#: Events a watcher hears by default: a report was filed, a person or a peer
#: is owed a decision, a run ended, or a delegated decision moved. Node
#: transitions (``entered``/``left``) are deliberately outside it — a long run
#: would fill the watcher's terminal, and the pull side (``slot``) shows them
#: on demand.
#:
#: The names are the ones the two runtimes actually emit, not the status names
#: they lead to (``cflow2/runtime.py`` ``emit`` calls, ``cflow/engine.py``
#: ``state_mod.journal`` calls). v2 has one ``waiting`` event whose ``kind``
#: says why (``choose``/``ask`` are gates; ``pause``/``loop_limit``/``visit``
#: are not — :data:`GATE_WAITING`), and no ``failed`` event: a failed end comes
#: as ``done`` with ``ok: false`` plus ``verify_failed``/``delegate_failed``.
DEFAULT_KINDS: Tuple[str, ...] = (
    "reported",
    "done",
    "aborted",
    "select_held",
    "waiting",
    "ask_routed",
    "ask_timeout",
    "ask_answered",
    "ask_withdrawn",
    "round_requested",
    "delegate_failed",
    "verify_failed",
    "started",
    "step_report",
    "step_completed",
    "gate_wait",
    "select_presented",
    "checklist_presented",
    "timer_presented",
    "ask_escalated",
    "ask_declined",
    "ask_deferred",
    "sub_ended",
    "published",
)

#: The ``kind`` values of the v2 ``waiting`` event that mean "someone must
#: decide". The rest are pacing, and a watcher that asked for gates only should
#: not hear them.
GATE_WAITING = ("choose", "ask")

#: A hard ceiling on events carried by one delivery. The watcher reads the run
#: itself for the rest (``events_since`` with the returned cursor).
BATCH_LIMIT = 40

#: Run states no later event will follow. The board announces the transition
#: once, because a run that ends without filing a report is exactly the case a
#: supervisor misses.
TERMINAL_STATUSES = ("done", "aborted", "failed")


def token(cwd: str, scope: str) -> str:
    """A cheap change token for a slot: what the daemon compares per tick.

    Reading a run means decoding a whole checkpoint (``RunStore.journal``), so
    the board asks this first: nothing moved means no read at all. It covers
    every file the two runtimes write for one run — the v2 checkpoint and its
    legacy pair, and the v1 state and journal.
    """
    from .cflow2 import store as v2_store

    st = v2_store.RunStore(cwd, scope)
    paths = [st.checkpoint_path if st.checkpoint_path.exists() else st.state_path,
             st.journal_path]
    try:  # a v2 scope ("<session>@<daemon>") is not a valid v1 scope name
        paths += [v1_state._state_path(cwd, scope), v1_state.journal_path(cwd, scope)]
    except Exception:  # noqa: BLE001 - no v1 slot by that name
        pass
    parts: List[str] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            parts.append("-")
            continue
        parts.append(f"{info.st_mtime_ns}:{info.st_size}")
    return "|".join(parts)


def _v2_slot(cwd: str, scope: str) -> Optional[dict]:
    from .cflow2 import runtime as v2_runtime, store as v2_store

    st = v2_store.RunStore(cwd, scope)
    if not st.exists():
        return None
    state = st.load_state()
    events = st.journal()
    run = state.get("run_id")
    current: List[str] = []
    owed: List[str] = []
    try:
        chart = st.load_chart()
    except Exception:  # noqa: BLE001 - a readable run still answers
        chart = None
    for iid in state.get("active") or []:
        rec = (state.get("inst") or {}).get(iid) or {}
        title = str(rec.get("node") or "")
        if chart is not None:
            try:
                node = chart.node(rec.get("node"))
                title = str(node.get("title") or node.label or node.id)
            except Exception:  # noqa: BLE001
                pass
        if title:
            current.append(title)
        if chart is not None and rec.get("await") == v2_runtime.AW_CHOOSE:
            try:
                node = chart.node(rec.get("node"))
            except Exception:  # noqa: BLE001
                continue
            if (node.get("chooser") or "") == "user":
                owed.append(f"choose: {title}")
    for approval in state.get("approvals") or []:
        kind = str(approval.get("kind") or "")
        if kind == v2_runtime.RECOVERY_MONITOR:
            continue
        owed.append(f"{kind}: {approval.get('label') or approval.get('node') or ''}".strip())
    return {
        "version": 2,
        "session": v2_store.session_of(scope) or scope,
        "scope": scope,
        "cwd": cwd,
        "run": run,
        "workflow": state.get("workflow"),
        "status": state.get("status"),
        "current": current,
        "owed": owed,
        "updated": state.get("updated"),
        "entered_at": _entered_at(state),
        "cursor": f"{run}:{len(events)}",
        "events": events,
    }


def _entered_at(state: dict) -> Optional[float]:
    best: Optional[float] = None
    for iid in state.get("active") or []:
        rec = (state.get("inst") or {}).get(iid) or {}
        at = rec.get("entered_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            best = at if best is None else min(best, at)
    return best


def _v1_slot(cwd: str, scope: str) -> Optional[dict]:
    from .cflow import engine as v1_engine

    resolved = v1_state.resolve_cwd(cwd)
    try:
        known = v1_state.scopes_in(resolved)
    except Exception:  # noqa: BLE001
        return None
    if scope not in known:
        return None
    payload = v1_engine.status(resolved, scope=scope)
    run = payload.get("run")
    if payload.get("status") == "idle" and not payload.get("pending_start"):
        return None
    events = journal_mod.read(v1_state.journal_path(resolved, scope))
    owed: List[str] = []
    status = str(payload.get("status") or "")
    if status in ("waiting_approval", "waiting_selection"):
        owed.append(f"{status}: {payload.get('title') or payload.get('step_id') or ''}".strip())
    return {
        "version": 1,
        "session": payload.get("scope") or scope,
        "scope": scope,
        "cwd": resolved,
        "run": run,
        "workflow": payload.get("workflow"),
        "status": status,
        "current": [str(payload.get("title") or payload.get("step_id") or "")],
        "owed": owed,
        "updated": payload.get("started_at"),
        "entered_at": None,
        "cursor": f"{run}:{len(events)}",
        "events": events,
    }


def slot(cwd: str, scope: str) -> Optional[dict]:
    """One run's compact state, or None when the slot holds no live run.

    v2 is looked for first: a session drives one format at a time, and a v1
    slot next to a v2 one is a leftover (the v1 tools refuse it too).
    """
    found = _v2_slot(cwd, scope)
    if found is not None:
        return _compact(found)
    found = _v1_slot(cwd, scope)
    return _compact(found) if found is not None else None


def _compact(found: dict) -> dict:
    out = {k: v for k, v in found.items() if k != "events"}
    return out


def _parse_cursor(cursor: str) -> Tuple[str, int]:
    run, _, count = str(cursor or "").rpartition(":")
    if not run or not count.isdigit():
        return "", 0
    return run, int(count)


def _detail(event: dict) -> str:
    bits = []
    for key in ("node", "step", "instance", "kind", "ask", "label", "outcome",
                "status", "option", "reason"):
        value = event.get(key)
        if value not in (None, ""):
            bits.append(f"{key}={value}")
    return " ".join(bits)


def reading(cwd: str, scope: str, cursor: str,
            kinds: Optional[Iterable[str]] = None) -> dict:
    """Everything one tick needs about a slot: what moved, and where it stands.

    ``events``/``cursor`` answer "what moved since that cursor"; ``run``,
    ``status`` and ``owed`` answer "where is it now" so the caller can announce
    a run that ended or a gate that opened even when the journal recorded no
    event for it (v1 does not journal a gate; v2 records approvals in state).
    ``token`` is the change token the cursor was read at, for the next tick to
    skip a slot nothing touched. ``present`` is False when the slot holds no
    live run.

    The returned cursor names the journal position just past the last event
    *sent*, so a truncated batch (``BATCH_LIMIT``) leaves the rest for the
    next call instead of skipping it. Events filtered out by ``kinds`` are
    still stepped over — and the cursor is returned even when nothing matched,
    so a quiet run costs one read, not one read per tick.
    """
    found = _v2_slot(cwd, scope) or _v1_slot(cwd, scope)
    if found is None:
        return {"events": [], "cursor": cursor, "run": None, "status": None,
                "owed": [], "current": [], "token": token(cwd, scope),
                "present": False}
    wanted = frozenset(kinds) if kinds is not None else frozenset(DEFAULT_KINDS)
    gates_only = kinds is None
    events: List[dict] = found["events"]
    run, count = _parse_cursor(cursor)
    if not run or run != str(found.get("run")) or count > len(events):
        count = 0
    selected: List[dict] = []
    pos = count
    for index in range(count, len(events)):
        record = events[index]
        pos = index + 1
        name = str(record.get("event") or "")
        if name not in wanted:
            continue
        if name == "waiting" and gates_only and record.get("kind") not in GATE_WAITING:
            continue
        selected.append({
            "event": name,
            "at": record.get("at") or record.get("ts"),
            "detail": _detail(record),
            "node": record.get("node") or record.get("step"),
        })
        if len(selected) >= BATCH_LIMIT:
            break
    return {
        "events": selected,
        "cursor": f"{found.get('run')}:{pos}",
        "run": found.get("run"),
        "status": found.get("status"),
        "owed": list(found.get("owed") or []),
        "current": list(found.get("current") or []),
        "token": token(cwd, scope),
        "present": True,
    }


def events_since(cwd: str, scope: str, cursor: str,
                 kinds: Optional[Iterable[str]] = None) -> Tuple[List[dict], str]:
    """``(events, cursor)`` — the reading's two delivery fields."""
    found = reading(cwd, scope, cursor, kinds)
    return found["events"], found["cursor"]


def compose_block(watcher: str, lines: Sequence[str]) -> str:
    """One fyi block for a watcher: the frame the protocol tells it to trust."""
    head = [
        "---",
        "# claunch cflow: watch -- machine-generated, not typed by the user",
        f"watcher: {watcher}",
        f"events: {len(lines)}",
    ]
    body = [f"- {line}" for line in lines]
    tail = [
        "protocol: these are state changes of runs you asked to watch ("
        "`subscriptions` lists them; `runs` reads one now). Your own workflow "
        "decides what to do with them; a run that ended is not an answer.",
        "---",
    ]
    return "\n".join(head + body + tail)


def may_watch(descendant, connected, target: str, self_name: str) -> bool:
    """Whether ``self_name`` may read/subscribe to ``target``.

    Two relations qualify (the design's D1): the target is in the watcher's
    own subtree (``descendant(handle)``), or the two are connected in the
    mesh (``connected(handle)``). Everything else is refused, including
    ``self`` — watching yourself is what ``flow_status`` already does.
    """
    if not target or target == self_name:
        return False
    try:
        if descendant(target):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        return bool(connected(target))
    except Exception:  # noqa: BLE001
        return False
