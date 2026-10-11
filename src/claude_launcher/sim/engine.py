"""The deterministic walk over a world model.

* :func:`step` -- (state, event) -> next state. Matching rules are applied by
  code; nothing here calls a model.
* :func:`explore` -- seeded walk over a cell archive (Go-Explore): pick a known
  abstract state, restore it, walk a few steps, keep new cells. The same
  model and seed give the same result.
* :func:`replay` -- rerun a witness (checkpoint, world seed, event sequence,
  assumptions). A finding's evidence, and after the model changes, its
  regression test.

An unknown guard (prose not yet formalized) is never filled in silently: the
walk picks an answer with the policy RNG and writes it into the trace as an
assumption, and a finding that depends on one carries it.

Finding kinds:

* ``unhandled`` -- a reached (machine, state, event) cell: the machine has
  rules for the event, none matched in this state. Or an event nobody handles.
* ``conflict`` -- two rules with the same priority write the same place
  differently. Neither is applied.
* ``invariant`` -- an invariant evaluated false after a step.
* ``stuck`` -- no actor/fault event is available and no timer is pending.
* ``loop`` -- an event chain (emit) longer than :data:`MAX_CHAIN`.
* ``eval-error`` -- an expression failed (unknown name, type error).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Set

from . import expr, model as M
from .rng import Rng

#: The macro-time event: jump to the next pending timer.
WAIT = "$wait"
MAX_CHAIN = 16


class _Index:
    def __init__(self, model: dict):
        self.by_event: Dict[str, List[dict]] = {}
        self.writes: Dict[str, Set[str]] = {}
        self.machines_of: Dict[str, Set[str]] = {}
        self.events_of: Dict[str, Set[str]] = {}
        self.atoms: List = []
        self.provisional: Set[str] = set()
        self.walkable = [e["name"] for e in model["events"] if e.get("kind", "actor") in ("actor", "fault")]
        self.event_decl = {e["name"]: e for e in model["events"]}

        def add_atoms(c):
            if c is None:
                return
            for a in expr.atoms_of(c):
                if isinstance(a, dict) and "var" in a and not any(expr.canon(x) == expr.canon(a) for x in self.atoms):
                    self.atoms.append(a)

        for r in model["rules"]:
            if r.get("status", "proposed") not in M.LIVE_STATUS or r.get("kind") == "available":
                continue
            if r.get("provisional"):
                self.provisional.add(r["id"])
            self.by_event.setdefault(r["on"]["event"], []).append(r)
            w = expr.writes_of(r.get("do") or [])
            self.writes[r["id"]] = w
            ms = set()
            if "when" in r:
                for a in expr.atoms_of(r["when"]):
                    if isinstance(a, dict) and "is" in a:
                        ms.add(a["sm"])
            ms.update(k[3:] for k in w if k.startswith("sm:"))
            self.machines_of[r["id"]] = ms
            for m in ms:
                self.events_of.setdefault(m, set()).add(r["on"]["event"])
            add_atoms(r.get("when"))
        for inv in model["invariants"]:
            add_atoms(inv.get("holds"))
        for e in model["events"]:
            add_atoms(e.get("available"))
        for lst in self.by_event.values():
            lst.sort(key=lambda r: (-(r.get("priority") or 0), r["id"]))


def index_of(model: dict) -> _Index:
    idx = model.get("_index")
    if idx is None:
        idx = _Index(model)
        model["_index"] = idx
    return idx


def prepare(model: dict) -> dict:
    """The walkable form of a stored model (availability folded in)."""
    m = M.effective(model)
    index_of(m)
    return m


def _env(s: dict) -> expr.Env:
    return expr.Env(s["vars"], s["sm"])


def _apply(s: dict, rule_id: str, effects, queue: List[str], out_of_model: List[str]):
    for e in effects or []:
        if "sm" in e and "to" in e:
            cur = s["sm"][e["sm"]]
            nxt = s["prev"][e["sm"]] if e["to"] == expr.PREV else e["to"]
            if nxt != cur:
                s["prev"][e["sm"]] = cur
                s["sm"][e["sm"]] = nxt
        elif "set" in e:
            s["vars"][e["set"]] = expr.eval_value(e.get("value"), _env(s))
        elif "add" in e:
            cur = s["vars"].get(e["add"])
            d = expr.eval_value(e.get("value"), _env(s))
            if not expr._is_num(cur) or not expr._is_num(d):
                raise expr.ExprError(f"add works on numbers only: {e['add']}")
            s["vars"][e["add"]] = cur + d
        elif "emit" in e:
            queue.append(e["emit"])
        elif "schedule" in e:
            d = expr.eval_value(e.get("in"), _env(s))
            if not expr._is_num(d) or d < 0:
                raise expr.ExprError(f"schedule.in must be a number >= 0: {expr.canon(d)}")
            s["timers"] = [t for t in s["timers"] if t["event"] != e["schedule"]]
            s["timers"].append({"event": e["schedule"], "due": s["clock"] + d, "seq": s["seq"]})
            s["seq"] += 1
        elif "unschedule" in e:
            s["timers"] = [t for t in s["timers"] if t["event"] != e["unschedule"]]
        elif "roll" in e:
            p = expr.eval_value(e["roll"], _env(s))
            if not expr._is_num(p):
                raise expr.ExprError(f"roll takes a probability: {expr.canon(p)}")
            r = Rng(0, s["rng"])
            hit = r.next() < p
            s["rng"] = r.state()
            _apply(s, rule_id, e.get("then") if hit else e.get("else"), queue, out_of_model)
        elif "prose" in e:
            out_of_model.append(f"{rule_id}: {e['prose']}")


def step(model: dict, state: dict, event: str,
         decide: Optional[Callable[[str], bool]] = None) -> dict:
    """Handle one event, then the events its effects emit, then check the
    invariants. ``decide(rule_id)`` answers unknown guards; without it they
    count as false and are listed in ``unresolved``."""
    idx = index_of(model)
    s = M.clone_state(state)
    res = {"state": s, "fired": [], "matched": [], "findings": [], "unresolved": [], "out_of_model": []}
    queue: List[str] = []
    first = event
    if event == WAIT:
        if not s["timers"]:
            res["findings"].append({"kind": "stuck", "key": "wait-without-timer", "event": event,
                                    "detail": "waited with no timer pending"})
            return res
        s["timers"].sort(key=lambda t: (t["due"], t["seq"]))
        t = s["timers"].pop(0)
        s["clock"] = max(s["clock"], t["due"])
        first = t["event"]
    queue.append(first)
    n = 0
    while queue:
        if n >= MAX_CHAIN:
            res["findings"].append({"kind": "loop", "key": f"loop|{first}", "event": first,
                                    "detail": f"event chain passed {MAX_CHAIN}: {' -> '.join(queue[:4])} ..."})
            break
        n += 1
        ev = queue.pop(0)
        before = dict(s["sm"])
        candidates = idx.by_event.get(ev, [])
        matched = []
        for r in candidates:
            try:
                t = True if "when" not in r else expr.eval_cond(r["when"], _env(s))
            except expr.ExprError as exc:
                res["findings"].append({"kind": "eval-error", "key": f"eval-error|{r['id']}", "event": ev,
                                        "rules": [r["id"]], "detail": str(exc)})
                continue
            if t == expr.UNKNOWN:
                if decide is not None:
                    t = decide(r["id"])
                else:
                    res["unresolved"].append(r["id"])
                    t = False
            if t:
                matched.append(r)
        res["matched"].extend(r["id"] for r in matched)

        picked: List[dict] = []
        conflicted: Set[str] = set()
        for r in matched:
            w = idx.writes[r["id"]]
            blocked = False
            for p in picked:
                pw = idx.writes[p["id"]]
                if not (w & pw):
                    continue
                if (p.get("priority") or 0) > (r.get("priority") or 0):
                    blocked = True
                elif expr.canon(p.get("do")) == expr.canon(r.get("do")):
                    blocked = True
                else:
                    ids = sorted([p["id"], r["id"]])
                    res["findings"].append({
                        "kind": "conflict", "key": f"conflict|{'|'.join(ids)}", "event": ev, "rules": ids,
                        "detail": f"two rules with the same priority write {', '.join(sorted(w & pw))} differently"})
                    conflicted.update(ids)
                    blocked = True
            if not blocked:
                picked.append(r)
        for r in picked:
            if r["id"] in conflicted:
                continue
            try:
                _apply(s, r["id"], r.get("do"), queue, res["out_of_model"])
                res["fired"].append(r["id"])
            except expr.ExprError as exc:
                res["findings"].append({"kind": "eval-error", "key": f"eval-error|{r['id']}", "event": ev,
                                        "rules": [r["id"]], "detail": str(exc)})

        for m, evs in idx.events_of.items():
            if ev not in evs or any(m in idx.machines_of[r["id"]] for r in matched):
                continue
            res["findings"].append({
                "kind": "unhandled", "key": f"unhandled|{m}|{before[m]}|{ev}", "event": ev,
                "machine": m, "state": before[m],
                "detail": f"{m} is in {before[m]!r} and no rule handles {ev!r} there"})
        if not candidates and not any(ev in evs for evs in idx.events_of.values()):
            res["findings"].append({"kind": "unhandled", "key": f"unhandled|-|-|{ev}", "event": ev,
                                    "detail": f"no rule handles {ev!r} at all"})
        elif not matched and candidates and all(not idx.machines_of[r["id"]] for r in candidates):
            at = signature(model, s).split("#")[1]
            res["findings"].append({"kind": "unhandled", "key": f"unhandled|-|{at}|{ev}", "event": ev,
                                    "detail": f"no rule for {ev!r} matches the current values"})

    for inv in model["invariants"]:
        try:
            if expr.eval_cond(inv["holds"], _env(s)) is False:
                res["findings"].append({"kind": "invariant", "key": f"invariant|{inv['id']}",
                                        "invariant": inv["id"], "event": event,
                                        "detail": f"invariant {inv['id']} broke"})
        except expr.ExprError as exc:
            res["findings"].append({"kind": "eval-error", "key": f"eval-error|{inv['id']}",
                                    "invariant": inv["id"], "detail": str(exc)})
    return res


def _available(model: dict, s: dict, name: str):
    e = index_of(model).event_decl.get(name)
    if e is None or "available" not in e:
        return True
    try:
        return expr.eval_cond(e["available"], _env(s))
    except expr.ExprError:
        return expr.UNKNOWN


def available_events(model: dict, s: dict) -> List[str]:
    """Events the walk may choose now. An unknown availability counts as
    available -- the walk errs toward reaching too much."""
    out = [e for e in index_of(model).walkable if _available(model, s, e) is not False]
    if s["timers"]:
        out.append(WAIT)
    return out


def signature(model: dict, s: dict) -> str:
    """The abstract state: machine states, the truth of every guard
    predicate over variables, and the names of pending timers."""
    idx = index_of(model)
    sm = "|".join(str(s["sm"][m["id"]]) for m in model["machines"])
    preds = []
    for a in idx.atoms:
        try:
            t = expr.eval_cond(a, _env(s))
            preds.append("?" if t == expr.UNKNOWN else ("1" if t else "0"))
        except expr.ExprError:
            preds.append("x")
    timers = ",".join(sorted({t["event"] for t in s["timers"]}))
    return f"{sm}#{''.join(preds)}#{timers}"


MAX_INPUT_STARTS = 16


def input_starts(model: dict, rng: Rng, cap: int = MAX_INPUT_STARTS) -> List[dict]:
    """Start-value combinations of variables with a ``domain`` that no live
    rule writes (inputs the world hands the system). The first is always
    the plain init values."""
    written = set()
    for r in model["rules"]:
        if r.get("status", "proposed") in M.LIVE_STATUS:
            written.update(w[4:] for w in expr.writes_of(r.get("do") or []) if w.startswith("var:"))
    combos: List[dict] = [{}]
    for v in model["vars"]:
        dom = v.get("domain")
        if not dom or v["name"] in written:
            continue
        combos = [c if x == v.get("init") else {**c, v["name"]: x} for c in combos for x in dom]
    seen, uniq = set(), []
    for c in combos:
        k = expr.canon(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    if len(uniq) <= cap:
        return uniq
    first, rest = uniq[0], uniq[1:]
    for i in range(len(rest) - 1, 0, -1):
        j = int(rng.next() * (i + 1))
        rest[i], rest[j] = rest[j], rest[i]
    return [first] + rest[:cap - 1]


def explore(model: dict, seed: int = 1, budget: int = 2000, horizon: int = 12,
            checkpoint: Optional[str] = None, policy: str = "novelty",
            world_seed: Optional[int] = None) -> dict:
    """Grow a cell archive from the start state. Same model, same options:
    same result."""
    idx = index_of(model)
    rng = Rng(seed)
    wseed = seed if world_seed is None else world_seed
    start = M.initial_state(model, checkpoint, wseed)
    archive: Dict[str, dict] = {}
    cells: List[dict] = []

    def add_cell(state, path, prov, inputs):
        sig = signature(model, state)
        c = archive.get(sig)
        if c is None:
            c = {"sig": sig, "state": M.clone_state(state), "path": [dict(p) for p in path], "visits": 0,
                 "tried": set(), "provisional": sorted(prov), "inputs": inputs or {}}
            archive[sig] = c
            cells.append(c)
        return c

    for inputs in input_starts(model, rng):
        st = M.clone_state(start)
        st["vars"].update(inputs)
        add_cell(st, [], set(), inputs)

    problems: Dict[str, dict] = {}
    tried: Set[str] = set()
    fired: Dict[str, int] = {}
    steps = assumptions = 0

    while steps < budget:
        # A stuck cell never takes a step, so a walk whose every cell is stuck
        # (no events at all, say) would never use up its budget.
        if all(c.get("stuck") for c in cells):
            break
        if policy == "random":
            cell = rng.pick(cells)
        else:
            w = [1 / math.sqrt(1 + c["visits"]) for c in cells]
            x = rng.next() * sum(w)
            i = 0
            while i < len(cells) - 1 and x >= w[i]:
                x -= w[i]
                i += 1
            cell = cells[i]
        cell["visits"] += 1
        state = M.clone_state(cell["state"])
        path = [dict(p) for p in cell["path"]]
        here = cell
        prov = set(cell["provisional"])

        def witness(p):
            out = {"checkpoint": checkpoint, "world_seed": wseed, "steps": [dict(x) for x in p]}
            if cell["inputs"]:
                out["inputs"] = dict(cell["inputs"])
            return out

        for _ in range(horizon):
            if steps >= budget:
                break
            avail = available_events(model, state)
            if not avail:
                here["stuck"] = True
                key = f"stuck|{here['sig']}"
                if key not in problems:
                    problems[key] = {"finding": {"kind": "stuck", "key": key, "state": dict(state["sm"]),
                                                 "detail": "no event can happen and no timer is pending"},
                                     "witness": witness(path), "count": 1, "assumed": []}
                break
            fresh = [e for e in avail if e not in here["tried"]] if policy == "novelty" else []
            ev = rng.pick(fresh or avail)
            here["tried"].add(ev)
            assume: Dict[str, bool] = {}

            def decide(rule_id):
                nonlocal assumptions
                if rule_id not in assume:
                    assume[rule_id] = rng.next() < 0.5
                    assumptions += 1
                return assume[rule_id]

            res = step(model, state, ev, decide)
            steps += 1
            for rid in res["fired"]:
                fired[rid] = fired.get(rid, 0) + 1
                if rid in idx.provisional:
                    prov.add(rid)
            for m, evs in idx.events_of.items():
                if ev in evs:
                    tried.add(f"{m}\x00{state['sm'][m]}\x00{ev}")
            path.append({"event": ev, "assume": dict(assume)} if assume else {"event": ev})
            for f in res["findings"]:
                p = problems.get(f["key"])
                if p:
                    p["count"] += 1
                else:
                    problems[f["key"]] = {"finding": f, "witness": witness(path), "count": 1,
                                          "assumed": sorted(assume), "provisional": sorted(prov)}
            state = res["state"]
            here = add_cell(state, path, prov, cell["inputs"])

    return {"cells": len(cells), "problems": list(problems.values()), "steps": steps,
            "tried": sorted(tried), "assumptions": assumptions, "fired": fired}


class StaleWitness(Exception):
    """The witness meets an unknown guard it has no assumption for: the
    model changed and the trace no longer takes the same path."""


def replay(model: dict, w: dict) -> dict:
    state = M.initial_state(model, w.get("checkpoint"), w.get("world_seed", 1))
    state["vars"].update(w.get("inputs") or {})
    out = []
    fired: Set[str] = set()
    for i, t in enumerate(w.get("steps") or []):
        assume = t.get("assume") or {}

        def decide(rule_id, i=i, t=t, assume=assume):
            if rule_id not in assume:
                raise StaleWitness(f"step {i + 1} ({t['event']}): rule {rule_id} has an unknown guard "
                                   "and the witness has no assumption for it")
            return assume[rule_id]

        res = step(model, state, t["event"], decide)
        fired.update(res["fired"])
        out.append({k: v for k, v in res.items() if k != "state"} | {"sm": dict(res["state"]["sm"]),
                                                                      "vars": dict(res["state"]["vars"])})
        state = res["state"]
    return {"state": state, "steps": out, "fired": sorted(fired)}


def reproduces(model: dict, w: dict, key: str) -> bool:
    """Does replaying the witness raise the same finding at its last step?"""
    try:
        r = replay(model, w)
    except StaleWitness:
        return False
    if key.startswith("stuck|"):
        return not available_events(model, r["state"]) and key == f"stuck|{signature(model, r['state'])}"
    return bool(r["steps"]) and any(f["key"] == key for f in r["steps"][-1]["findings"])


def coverage(model: dict, tried: List[str]) -> dict:
    """The (machine, state, event) grid: cells the rules address, how many
    the walk reached, and how many have a rule."""
    idx = index_of(model)
    handled: Set[str] = set()
    for r in (x for lst in idx.by_event.values() for x in lst):
        for a in expr.atoms_of(r.get("when", True)):
            if isinstance(a, dict) and "is" in a:
                for st in (a["is"] if isinstance(a["is"], list) else [a["is"]]):
                    handled.add(f"{a['sm']}\x00{st}\x00{r['on']['event']}")
    total = 0
    grid = []
    for m in model["machines"]:
        evs = sorted(idx.events_of.get(m["id"], ()))
        total += len(evs) * len(m["states"])
        grid.append({"machine": m["id"], "states": m["states"], "events": evs})
    reached = set(tried)
    return {"cells": total, "reached": len(reached), "grid": grid,
            "reached_cells": sorted(reached), "handled_cells": sorted(handled)}
