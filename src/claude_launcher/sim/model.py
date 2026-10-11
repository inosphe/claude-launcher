"""World-model declarations: what a session writes and the engine walks.

A model is a plain JSON object::

    {
      "machines":    [{"id", "states": [...], "initial", "note"?}],
      "vars":        [{"name", "init", "domain"?: [...], "note"?}],
      "events":      [{"name", "kind": "actor|system|fault", "available"?: cond, "note"?}],
      "rules":       [{"id", "on": {"event"}, "when"?: cond, "do": [effects],
                       "priority"?, "kind"?: "effect|available",
                       "source": [{"ref", "quote"}], "provisional"?: {"why"},
                       "status": "proposed|accepted|disputed|retracted"}],
      "invariants":  [{"id", "holds": cond, "source": [...], "note"?}],
      "checkpoints": {"name": {"sm"?: {...}, "vars"?: {...}}}
    }

Two rules shape what goes in. A rule says where it came from (``source``: a
file path in the project and a verbatim quote) or says that it is a filled-in
guess (``provisional.why``) -- the walk runs on both, and findings that lean on
a guess say so. And the state is Markov: everything the next step needs is in
the machines and variables, never in the history of how the walk got there.

``actor`` events can be chosen by the walk whenever available; ``system``
events only happen through ``emit``/``schedule``; ``fault`` events (crash,
timeout, restart) are chosen like actor events -- the model's way of saying
"this can interrupt at any time".
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from . import expr

EVENT_KINDS = ("actor", "system", "fault")
RULE_STATUS = ("proposed", "accepted", "disputed", "retracted")
#: Statuses the walk applies (disputed counts until judged, so its cell does
#: not also show up as a gap).
LIVE_STATUS = ("proposed", "accepted", "disputed")
SECTIONS = ("machines", "vars", "events", "rules", "invariants")
KEY_OF = {"machines": "id", "vars": "name", "events": "name", "rules": "id", "invariants": "id"}


def empty() -> dict:
    return {s: [] for s in SECTIONS} | {"checkpoints": {}}


def normalize(model: Optional[dict]) -> dict:
    out = empty()
    for k, v in (model or {}).items():
        if k in out:
            out[k] = copy.deepcopy(v)
    return out


def known_of(model: dict) -> expr.Known:
    return expr.Known(
        sm={m["id"]: set(m.get("states") or []) for m in model["machines"] if isinstance(m, dict) and "id" in m},
        vars={v["name"] for v in model["vars"] if isinstance(v, dict) and "name" in v},
        events={e["name"] for e in model["events"] if isinstance(e, dict) and "name" in e},
    )


def _name_ok(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 200


def check_model(model: dict) -> List[str]:
    """Shape and reference problems, one line each; empty means walkable.
    Sources are checked for shape here; whether a quote is really in its
    file is :func:`claude_launcher.sim.store.check_sources`."""
    out: List[str] = []
    for section in SECTIONS:
        if not isinstance(model.get(section), list):
            out.append(f"{section} must be a list")
            continue
        seen = set()
        for i, item in enumerate(model[section]):
            key = KEY_OF[section]
            if not isinstance(item, dict) or not _name_ok(item.get(key)):
                out.append(f"{section}[{i}]: needs a {key}")
                continue
            if item[key] in seen:
                out.append(f"{section}: {item[key]} appears twice")
            seen.add(item[key])
    if out:
        return out
    known = known_of(model)
    for m in model["machines"]:
        states = m.get("states")
        if not isinstance(states, list) or not states or not all(_name_ok(s) for s in states):
            out.append(f"machine {m['id']}: states must be a non-empty list of names")
        elif m.get("initial") not in states:
            out.append(f"machine {m['id']}: initial {m.get('initial')!r} is not one of its states")
    for v in model["vars"]:
        if "init" not in v or isinstance(v["init"], (dict, list)):
            out.append(f"var {v['name']}: init must be a scalar")
        dom = v.get("domain")
        if dom is not None and (not isinstance(dom, list) or not dom or any(isinstance(x, (dict, list)) for x in dom)):
            out.append(f"var {v['name']}: domain must be a non-empty list of scalars")
    for e in model["events"]:
        if e.get("kind", "actor") not in EVENT_KINDS:
            out.append(f"event {e['name']}: kind must be one of {', '.join(EVENT_KINDS)}")
        if "available" in e:
            out.extend(expr.check_cond(e["available"], f"event {e['name']}.available", known))
    for r in model["rules"]:
        at = f"rule {r['id']}"
        on = r.get("on")
        if not isinstance(on, dict) or not _name_ok(on.get("event")):
            out.append(f"{at}: on.event is missing")
        elif on["event"] not in known.events:
            out.append(f"{at}: on.event {on['event']} is not a declared event")
        if r.get("kind", "effect") not in ("effect", "available"):
            out.append(f"{at}: kind must be effect or available")
        if r.get("status", "proposed") not in RULE_STATUS:
            out.append(f"{at}: status must be one of {', '.join(RULE_STATUS)}")
        if "priority" in r and not isinstance(r["priority"], (int, float)):
            out.append(f"{at}: priority must be a number")
        if "when" in r:
            out.extend(expr.check_cond(r["when"], f"{at}.when", known))
        out.extend(expr.check_effects(r.get("do", []), f"{at}.do", known))
        out.extend(_check_source(r, at))
    for inv in model["invariants"]:
        at = f"invariant {inv['id']}"
        out.extend(expr.check_cond(inv.get("holds"), f"{at}.holds", known))
        out.extend(_check_source(inv, at))
    cps = model.get("checkpoints")
    if not isinstance(cps, dict):
        out.append("checkpoints must be an object")
    else:
        for name, cp in cps.items():
            if not isinstance(cp, dict):
                out.append(f"checkpoint {name}: must be an object")
                continue
            for sm, st in (cp.get("sm") or {}).items():
                if st not in known.sm.get(sm, ()):
                    out.append(f"checkpoint {name}: {sm} has no state {st}")
            for v in (cp.get("vars") or {}):
                if v not in known.vars:
                    out.append(f"checkpoint {name}: unknown variable {v}")
    return out


def _check_source(item: dict, at: str) -> List[str]:
    prov = item.get("provisional")
    if prov is not None:
        if not isinstance(prov, dict) or not str(prov.get("why") or "").strip():
            return [f"{at}: provisional needs a why"]
        return []
    src = item.get("source")
    if not isinstance(src, list) or not src:
        return [f"{at}: needs a source ([{{ref, quote}}]) or provisional.why -- "
                "a rule nothing says is a guess and must be marked as one"]
    out = []
    for i, s in enumerate(src):
        if not isinstance(s, dict) or not _name_ok(s.get("ref")) or not str(s.get("quote") or "").strip():
            out.append(f"{at}.source[{i}]: needs ref and quote")
    return out


def effective(model: dict) -> dict:
    """The model the engine walks: availability rules folded into their
    events (any one live rule makes the event available)."""
    m = normalize(model)
    avail: Dict[str, list] = {}
    for r in m["rules"]:
        if r.get("kind") == "available" and r.get("status", "proposed") in LIVE_STATUS:
            avail.setdefault(r["on"]["event"], []).append(r.get("when", True))
    for e in m["events"]:
        conds = avail.get(e["name"])
        if conds:
            e["available"] = True if True in conds else (conds[0] if len(conds) == 1 else {"any": conds})
    return m


def stats(model: dict) -> dict:
    m = normalize(model)
    rules = [r for r in m["rules"] if r.get("status", "proposed") in LIVE_STATUS]
    return {
        "machines": len(m["machines"]), "vars": len(m["vars"]), "events": len(m["events"]),
        "rules": len(rules), "provisional": sum(1 for r in rules if r.get("provisional")),
        "invariants": len(m["invariants"]), "checkpoints": len(m["checkpoints"]),
    }


def apply_patch(model: dict, patch: dict) -> dict:
    """Upsert and remove by key. ``patch`` has any of the sections as lists
    (an item replaces the one with the same key, or is added), ``checkpoints``
    as an object (``null`` removes one), and ``remove: {section: [keys]}``."""
    m = normalize(model)
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    unknown = set(patch) - set(SECTIONS) - {"checkpoints", "remove"}
    if unknown:
        raise ValueError(f"unknown patch keys: {', '.join(sorted(unknown))}")
    for section, keys in (patch.get("remove") or {}).items():
        if section not in KEY_OF:
            raise ValueError(f"remove: unknown section {section}")
        drop = set(keys or [])
        m[section] = [x for x in m[section] if x.get(KEY_OF[section]) not in drop]
    for section in SECTIONS:
        items = patch.get(section)
        if items is None:
            continue
        if not isinstance(items, list):
            raise ValueError(f"{section} must be a list")
        key = KEY_OF[section]
        for item in items:
            if not isinstance(item, dict) or not _name_ok(item.get(key)):
                raise ValueError(f"{section}: every item needs a {key}")
            item = copy.deepcopy(item)
            if section == "rules":
                item.setdefault("status", "proposed")
                item.setdefault("do", [])
            idx = next((i for i, x in enumerate(m[section]) if x.get(key) == item[key]), None)
            if idx is None:
                m[section].append(item)
            else:
                m[section][idx] = item
    cps = patch.get("checkpoints")
    if cps is not None:
        if not isinstance(cps, dict):
            raise ValueError("checkpoints must be an object")
        for name, cp in cps.items():
            if cp is None:
                m["checkpoints"].pop(name, None)
            else:
                m["checkpoints"][name] = copy.deepcopy(cp)
    return m


def initial_state(model: dict, checkpoint: Optional[str] = None, seed: int = 1) -> dict:
    sm = {m["id"]: m["initial"] for m in model["machines"]}
    s = {"sm": sm, "prev": dict(sm), "vars": {v["name"]: v.get("init") for v in model["vars"]},
         "clock": 0, "timers": [], "rng": seed & 0xFFFFFFFF, "seq": 0}
    if checkpoint:
        cp = model["checkpoints"].get(checkpoint)
        if cp is None:
            raise ValueError(f"no checkpoint {checkpoint!r}")
        s["sm"].update(cp.get("sm") or {})
        s["prev"] = dict(s["sm"])
        s["vars"].update(cp.get("vars") or {})
    return s


def clone_state(s: dict) -> dict:
    return {"sm": dict(s["sm"]), "prev": dict(s["prev"]), "vars": dict(s["vars"]),
            "clock": s["clock"], "timers": [dict(t) for t in s["timers"]],
            "rng": s["rng"], "seq": s["seq"]}
