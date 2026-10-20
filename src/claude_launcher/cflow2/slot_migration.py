"""Move run slots written before the daemon qualifier (claunch-9jmdz).

Until claunch-9jmdz a slot was ``<cwd>/.cflow2/runs/<session>``. Session names
are numbered per daemon, so the default daemon and a named instance with
sessions in one directory shared ``runs/s15``. A slot is now
``runs/<session>@<daemon>`` (:mod:`.store`). This module moves an old slot to
its qualified name, once, by the daemon that owns it, when that daemon starts
(:mod:`..daemon.cflow2_slots` runs it; nothing else applies it).

Which daemon owns an old slot (:func:`attribute`):

1. the daemon its run recorded at start (``state["daemon"]``, written by every
   run started since the qualifier), else
2. the daemons that had a session of that name alive when the run started
   (``created_at <= started``, not exited before it): exactly one is the owner,
   else
3. exactly one daemon that has a session of that name at all.

Anything else -- no candidate, two candidates, an unreadable run -- is left
where it is and reported. A slot left in place is still read and written by
the old name (:func:`.store.legacy_dir`), as before the qualifier.

A move renames the directory under the slot's lock, rewrites the scope
strings the run keeps (its own ``scope``, a nested run's ``parent`` link, a
delegation's ``nested``, a side run's ``main``), records the daemon, and
journals ``slot_moved``.
It leaves a symlink at the old name pointing at the new one: a process that
started before the move (an agent's MCP server runs the code it was started
with) still finds its run. New code does not list or read the link
(:func:`.store.list_scopes`). Archives (``.cflow2/archive/<old name>``) stay.

Moving is idempotent: a moved slot is a link, which is not a candidate again.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .. import atomic
from . import store as store_mod

log = logging.getLogger(__name__)

#: ``{daemon id: {session name: (created_at, exited_at)}}`` -- ISO timestamps,
#: None when unknown / still running.
Sessions = Mapping[str, Mapping[str, Tuple[Optional[str], Optional[str]]]]

MOVE = "move"
LEAVE = "leave"
OTHER = "other"  # another daemon's: that daemon moves it when it starts

EVENT = "slot_moved"


@dataclass
class Entry:
    cwd: str
    slot: str
    action: str
    owner: Optional[str] = None
    target: Optional[str] = None
    why: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Report:
    daemon: str
    entries: List[Entry] = field(default_factory=list)

    def of(self, action: str) -> List[Entry]:
        return [e for e in self.entries if e.action == action]

    def as_dict(self) -> Dict[str, Any]:
        return {"daemon": self.daemon,
                "moved": [e.as_dict() for e in self.of(MOVE)],
                "left": [e.as_dict() for e in self.of(LEAVE)],
                "other": [e.as_dict() for e in self.of(OTHER)]}


def _when(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else None


def legacy_slots(cwd: str) -> List[Path]:
    """The slots in ``cwd`` written before the qualifier: real directories
    holding a run whose name has no ``@<daemon>`` (``default`` excluded)."""
    runs = store_mod.resolve_cwd(cwd) / store_mod.BASE_DIR / "runs"
    if not runs.is_dir():
        return []
    out = []
    for p in sorted(runs.iterdir()):
        if p.is_symlink() or not p.is_dir():
            continue
        if store_mod.DAEMON_SEP in p.name or store_mod.main_scope(p.name) == store_mod.DEFAULT_SCOPE:
            continue
        if (p / store_mod.STATE_FILE).is_file() or (p / store_mod.checkpoint.FILE).exists():
            out.append(p)
    return out


def _state(path: Path) -> Optional[Dict[str, Any]]:
    st = store_mod.RunStore.at(path)
    try:
        state = st.load_state()
    except store_mod.StoreError:
        return None
    return state if isinstance(state, dict) else None


def attribute(name: str, state: Dict[str, Any], sessions: Sessions) -> Tuple[Optional[str], str]:
    """The daemon an old slot ``name`` belongs to, and why; (None, why) when
    it cannot be told."""
    stamp = state.get(store_mod.DAEMON_KEY)
    if stamp:
        return str(stamp), "recorded at start"
    session = store_mod.session_of(name)
    holders = [d for d, names in sessions.items() if session in names]
    if not holders:
        return None, f"no daemon has a session {session!r}"
    started = _when(state.get("started"))
    if started is not None:
        alive = []
        for d in holders:
            created, exited = sessions[d][session]
            c, e = _when(created), _when(exited)
            if c is not None and c > started:
                continue
            if e is not None and e < started:
                continue
            alive.append(d)
        if len(alive) == 1:
            return alive[0], f"its session {session!r} was alive at the run's start"
        if len(alive) > 1:
            return None, f"sessions {session!r} of {', '.join(sorted(alive))} were all alive at the run's start"
    if len(holders) == 1:
        return holders[0], f"the only daemon with a session {session!r}"
    return None, f"sessions {session!r} on {', '.join(sorted(holders))}; the run's start does not tell them apart"


def requalify(state: Dict[str, Any], daemon: str) -> Dict[str, Any]:
    """``state`` with every scope string it keeps qualified by ``daemon``."""
    out = dict(state)
    if out.get("scope"):
        out["scope"] = store_mod.qualify(str(out["scope"]), daemon)
    out[store_mod.DAEMON_KEY] = daemon
    link = out.get("parent")  # a nested run's link to its parent's slot
    if isinstance(link, dict) and link.get("scope"):
        out["parent"] = dict(link, scope=store_mod.qualify(str(link["scope"]), daemon))
    dels = out.get("delegations")
    if isinstance(dels, dict):
        fixed = {}
        for did, rec in dels.items():
            nested = rec.get("nested") if isinstance(rec, dict) else None
            if isinstance(nested, dict) and nested.get("scope"):
                rec = dict(rec, nested=dict(nested, scope=store_mod.qualify(str(nested["scope"]), daemon)))
            fixed[did] = rec
        out["delegations"] = fixed
    calls = out.get("calls")  # dynamic calls' callee runs (claunch-z17c9)
    if isinstance(calls, dict):
        fixed = {}
        for cid, rec in calls.items():
            nested = rec.get("nested") if isinstance(rec, dict) else None
            if isinstance(nested, dict) and nested.get("scope"):
                rec = dict(rec, nested=dict(nested, scope=store_mod.qualify(str(nested["scope"]), daemon)))
            fixed[cid] = rec
        out["calls"] = fixed
    side = out.get("side")
    if isinstance(side, dict) and side.get("main"):
        out["side"] = dict(side, main=store_mod.qualify(str(side["main"]), daemon))
    return out


def move(path: Path, target_name: str, daemon: str) -> Path:
    """Move the old slot ``path`` to ``target_name`` beside it, under its lock,
    rewrite its scope strings and leave a link at the old name."""
    target = path.parent / target_name
    st = store_mod.RunStore.at(path)
    with st.lock():
        if target.exists():
            raise store_mod.StoreError(f"{target} already exists")
        state = st.load_state()
        # a scanner or indexer may hold a handle inside the slot for a moment
        # (WinError 5 / 32): the retry absorbs that, a real conflict still raises
        atomic.replace(path, target)
        moved = store_mod.RunStore.at(target)
        moved.commit(requalify(state, daemon),
                     [{"event": EVENT, "from": path.name, "to": target_name}])
    try:
        os.symlink(target_name, path, target_is_directory=True)
    except OSError as exc:  # Windows without the privilege: old processes lose the run
        log.warning("cflow2 slot %s moved to %s; no link left at the old name: %s",
                    path, target_name, exc)
    return target


def migrate(cwds: Iterable[str], daemon: str, sessions: Sessions, *,
            apply: bool = False) -> Report:
    """Plan (and with ``apply`` perform) the moves of ``daemon``'s old slots
    in ``cwds``. Another daemon's slots are reported, never touched."""
    report = Report(daemon)
    seen = set()
    for cwd in cwds:
        try:
            resolved = str(store_mod.resolve_cwd(cwd))
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        for path in legacy_slots(resolved):
            state = _state(path)
            if state is None:
                report.entries.append(Entry(resolved, path.name, LEAVE, why="run unreadable"))
                continue
            owner, why = attribute(path.name, state, sessions)
            if owner is None:
                report.entries.append(Entry(resolved, path.name, LEAVE, why=why))
                continue
            target = store_mod.qualify(path.name, owner)
            if owner != daemon:
                report.entries.append(Entry(resolved, path.name, OTHER, owner, target, why))
                continue
            entry = Entry(resolved, path.name, MOVE, owner, target, why)
            if (path.parent / target).exists():
                entry.action, entry.why = LEAVE, f"{target} already exists"
            elif apply:
                try:
                    move(path, target, owner)
                except (store_mod.StoreError, OSError) as exc:
                    entry.action, entry.why = LEAVE, f"move failed: {exc}"
            report.entries.append(entry)
    return report
