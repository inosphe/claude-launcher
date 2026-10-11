"""The small JSON expression language of world-model rules.

A rule's guard and effects are JSON written by a session and computed by
code. There is no text syntax: checking and evaluating the JSON a model
produced leaves fewer places to go wrong than parsing.

Conditions are three-valued: true, false, unknown. A sentence not yet turned
into an expression (``{"prose": ...}``) is unknown, and the walk never makes
one up -- when it picks a branch under an unknown guard it records the
assumption in the trace (:mod:`.engine`).

Values::

    scalar | {"var": name} | {"sm": machine}
    | {"op": "+|-|*|/|min|max", "args": [value, ...]}

Conditions::

    true | {"all": [cond...]} | {"any": [cond...]} | {"not": cond}
    | {"sm": machine, "is": state | [states]}
    | {"var": name, "<eq|ne|lt|le|gt|ge>": value}     (exactly one operator)
    | {"prose": text}                                  (always unknown)

Effects::

    {"sm": machine, "to": state | "$prev"}   ("$prev": back to the previous state)
    {"set": var, "value": value}  {"add": var, "value": value}
    {"emit": event}                          (handled within the same step)
    {"schedule": event, "in": value}  {"unschedule": event}
    {"roll": probability, "then": [effects], "else": [effects]}
    {"prose": text}                          (applied as "outside the model")
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

Scalar = Union[int, float, str, bool, None]
Truth = Union[bool, str]  # True, False or UNKNOWN
UNKNOWN = "unknown"

CMP_OPS = ("eq", "ne", "lt", "le", "gt", "ge")
ARITH_OPS = ("+", "-", "*", "/", "min", "max")
#: ``to`` value that returns a machine to the state it was in before.
PREV = "$prev"


class ExprError(Exception):
    pass


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def canon(v: Any) -> str:
    """Stable JSON text, for comparing expressions."""
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


class Env:
    """The world side an expression reads."""

    __slots__ = ("vars", "sm")

    def __init__(self, vars: Dict[str, Scalar], sm: Dict[str, str]):
        self.vars = vars
        self.sm = sm


def eval_value(e: Any, env: Env) -> Scalar:
    if not _is_obj(e):
        if isinstance(e, list):
            raise ExprError(f"not a value expression: {canon(e)}")
        return e
    if "var" in e:
        if e["var"] not in env.vars:
            raise ExprError(f"unknown variable: {e['var']}")
        return env.vars[e["var"]]
    if "sm" in e:
        if e["sm"] not in env.sm:
            raise ExprError(f"unknown state machine: {e['sm']}")
        return env.sm[e["sm"]]
    if "op" in e:
        xs = []
        for a in e.get("args") or []:
            v = eval_value(a, env)
            if not _is_num(v):
                raise ExprError(f"{e['op']} argument is not a number: {canon(v)}")
            xs.append(v)
        if not xs:
            raise ExprError(f"{e['op']} has no arguments")
        op = e["op"]
        if op == "+":
            return sum(xs)
        if op == "-":
            if len(xs) == 1:
                return -xs[0]
            out = xs[0]
            for x in xs[1:]:
                out -= x
            return out
        if op == "*":
            out = 1
            for x in xs:
                out *= x
            return out
        if op == "/":
            out = xs[0]
            for x in xs[1:]:
                if x == 0:
                    raise ExprError("division by zero")
                out = out / x
            return out
        if op == "min":
            return min(xs)
        if op == "max":
            return max(xs)
    raise ExprError(f"unknown value expression: {canon(e)}")


def _cmp(op: str, a: Scalar, b: Scalar) -> bool:
    if op == "eq":
        return a == b and type(a) is type(b) or (_is_num(a) and _is_num(b) and a == b)
    if op == "ne":
        return not _cmp("eq", a, b)
    if not _is_num(a) or not _is_num(b):
        raise ExprError(f"{op} compares numbers only: {canon(a)} / {canon(b)}")
    return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]


def cmp_of(c: dict) -> Tuple[str, Any]:
    ops = [op for op in CMP_OPS if op in c]
    if len(ops) != 1:
        raise ExprError(f"a variable comparison has exactly one operator: {canon(c)}")
    return ops[0], c[ops[0]]


def eval_cond(c: Any, env: Env) -> Truth:
    """Three-valued: any false makes ``all`` false, any true makes ``any`` true."""
    if c is True:
        return True
    if not _is_obj(c):
        raise ExprError(f"unknown condition: {canon(c)}")
    if "all" in c:
        unknown = False
        for x in c["all"]:
            t = eval_cond(x, env)
            if t is False:
                return False
            if t == UNKNOWN:
                unknown = True
        return UNKNOWN if unknown else True
    if "any" in c:
        unknown = False
        for x in c["any"]:
            t = eval_cond(x, env)
            if t is True:
                return True
            if t == UNKNOWN:
                unknown = True
        return UNKNOWN if unknown else False
    if "not" in c:
        t = eval_cond(c["not"], env)
        return t if t == UNKNOWN else (not t)
    if "prose" in c:
        return UNKNOWN
    if "sm" in c and "is" in c:
        if c["sm"] not in env.sm:
            raise ExprError(f"unknown state machine: {c['sm']}")
        want = c["is"] if isinstance(c["is"], list) else [c["is"]]
        return env.sm[c["sm"]] in want
    if "var" in c:
        op, rhs = cmp_of(c)
        return _cmp(op, eval_value({"var": c["var"]}, env), eval_value(rhs, env))
    raise ExprError(f"unknown condition: {canon(c)}")


def atoms_of(c: Any, out: Optional[List[Any]] = None) -> List[Any]:
    """The atomic predicates of a condition (variable comparisons, machine
    tests, prose), each once. Guard predicates are the walk's abstraction:
    two states that agree on all of them are one cell."""
    out = [] if out is None else out
    if c is True or not _is_obj(c):
        return out
    if "all" in c or "any" in c:
        for x in c.get("all", c.get("any")) or []:
            atoms_of(x, out)
    elif "not" in c:
        atoms_of(c["not"], out)
    elif not any(canon(o) == canon(c) for o in out):
        out.append(c)
    return out


def writes_of(effects: Iterable[Any], out: Optional[Set[str]] = None) -> Set[str]:
    """The places effects write: ``sm:<machine>``, ``var:<name>``,
    ``timer:<event>``. emit and prose write nothing."""
    out = set() if out is None else out
    for e in effects or []:
        if not _is_obj(e):
            continue
        if "sm" in e and "to" in e:
            out.add(f"sm:{e['sm']}")
        elif "set" in e:
            out.add(f"var:{e['set']}")
        elif "add" in e:
            out.add(f"var:{e['add']}")
        elif "schedule" in e:
            out.add(f"timer:{e['schedule']}")
        elif "unschedule" in e:
            out.add(f"timer:{e['unschedule']}")
        elif "roll" in e:
            writes_of(e.get("then") or [], out)
            writes_of(e.get("else") or [], out)
    return out


# -- shape checks -------------------------------------------------------------

class Known:
    """Names a model declares, for checking references."""

    def __init__(self, sm: Dict[str, Set[str]], vars: Set[str], events: Set[str]):
        self.sm = sm
        self.vars = vars
        self.events = events


def _sm_states(known: Optional[Known], sm: str, states: List[str], at: str) -> List[str]:
    if known is None:
        return []
    names = known.sm.get(sm)
    if names is None:
        return [f"{at}: unknown state machine {sm}"]
    return [f"{at}: {sm} has no state {s}" for s in states if s not in names]


def check_value(v: Any, at: str, known: Optional[Known] = None) -> List[str]:
    if v is None or isinstance(v, (bool, int, float, str)):
        return []
    if not _is_obj(v):
        return [f"{at}: not a value expression"]
    if "var" in v:
        if not isinstance(v["var"], str):
            return [f"{at}: var must be a name"]
        return [f"{at}: unknown variable {v['var']}"] if known and v["var"] not in known.vars else []
    if "sm" in v:
        if not isinstance(v["sm"], str):
            return [f"{at}: sm must be a name"]
        return [f"{at}: unknown state machine {v['sm']}"] if known and v["sm"] not in known.sm else []
    if "op" in v:
        args = v.get("args")
        if v["op"] not in ARITH_OPS or not isinstance(args, list) or not args:
            return [f"{at}: not an {{op, args}} expression"]
        return [p for i, a in enumerate(args) for p in check_value(a, f"{at}.args[{i}]", known)]
    return [f"{at}: unknown value expression ({', '.join(v)})"]


def check_cond(c: Any, at: str, known: Optional[Known] = None) -> List[str]:
    if c is True:
        return []
    if not _is_obj(c):
        return [f"{at}: a condition must be true or an object"]
    keys = list(c)
    if "all" in c or "any" in c:
        items = c.get("all", c.get("any"))
        if len(keys) != 1 or not isinstance(items, list):
            return [f"{at}: all/any takes exactly one list"]
        return [p for i, x in enumerate(items) for p in check_cond(x, f"{at}.{keys[0]}[{i}]", known)]
    if "not" in c:
        return check_cond(c["not"], f"{at}.not", known) if len(keys) == 1 else [f"{at}: not takes one condition"]
    if "prose" in c:
        ok = isinstance(c["prose"], str) and c["prose"].strip() and len(keys) == 1
        return [] if ok else [f"{at}: prose takes one sentence"]
    if "sm" in c and "is" in c:
        states = c["is"] if isinstance(c["is"], list) else [c["is"]]
        if len(keys) != 2 or not isinstance(c["sm"], str) or not states or not all(isinstance(s, str) for s in states):
            return [f"{at}: not an {{sm, is}} test"]
        return _sm_states(known, c["sm"], states, at)
    if "var" in c:
        if not isinstance(c["var"], str):
            return [f"{at}: var must be a name"]
        ops = [op for op in CMP_OPS if op in c]
        if len(ops) != 1 or len(keys) != 2:
            return [f"{at}: a variable comparison has exactly one operator ({'|'.join(CMP_OPS)})"]
        out = [f"{at}: unknown variable {c['var']}"] if known and c["var"] not in known.vars else []
        return out + check_value(c[ops[0]], f"{at}.{ops[0]}", known)
    return [f"{at}: unknown condition shape ({', '.join(keys)})"]


def check_effects(effects: Any, at: str, known: Optional[Known] = None) -> List[str]:
    if not isinstance(effects, list):
        return [f"{at}: effects must be a list"]
    out: List[str] = []
    for i, e in enumerate(effects):
        here = f"{at}[{i}]"
        if not _is_obj(e):
            out.append(f"{here}: an effect must be an object")
        elif "sm" in e and "to" in e:
            if not isinstance(e["sm"], str) or not isinstance(e["to"], str):
                out.append(f"{here}: {{sm, to}} takes names")
            elif e["to"] == PREV:
                if known and e["sm"] not in known.sm:
                    out.append(f"{here}: unknown state machine {e['sm']}")
            else:
                out.extend(_sm_states(known, e["sm"], [e["to"]], here))
        elif "set" in e or "add" in e:
            name = e.get("set", e.get("add"))
            if not isinstance(name, str):
                out.append(f"{here}: set/add takes a variable name")
                continue
            if known and name not in known.vars:
                out.append(f"{here}: unknown variable {name}")
            out.extend(check_value(e.get("value"), f"{here}.value", known))
        elif "emit" in e:
            if not isinstance(e["emit"], str) or not e["emit"]:
                out.append(f"{here}: emit takes an event name")
            elif known and e["emit"] not in known.events:
                out.append(f"{here}: unknown event {e['emit']}")
        elif "schedule" in e:
            if not isinstance(e["schedule"], str):
                out.append(f"{here}: schedule takes an event name")
                continue
            if known and e["schedule"] not in known.events:
                out.append(f"{here}: unknown event {e['schedule']}")
            out.extend(check_value(e.get("in"), f"{here}.in", known))
        elif "unschedule" in e:
            if not isinstance(e["unschedule"], str):
                out.append(f"{here}: unschedule takes an event name")
        elif "roll" in e:
            out.extend(check_value(e["roll"], f"{here}.roll", known))
            out.extend(check_effects(e.get("then"), f"{here}.then", known))
            if "else" in e:
                out.extend(check_effects(e["else"], f"{here}.else", known))
        elif "prose" in e:
            if not isinstance(e["prose"], str) or not e["prose"].strip():
                out.append(f"{here}: prose takes a sentence")
        else:
            out.append(f"{here}: unknown effect shape ({', '.join(e)})")
    return out
