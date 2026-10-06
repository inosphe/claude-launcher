"""Typed start inputs of a v2 definition (``docs/cflow-v2.md`` 2.6).

A definition declares ``inputs:`` at the top level; the compiler checks the
declaration with :func:`schema` and keeps it in ``chart.meta['inputs']``.
Whoever starts a run (the dashboard's form, ``--input``, the MCP spawn tool)
hands the raw values to :func:`normalize`, and the runtime folds the result
into the chart with :func:`apply` before it writes ``chart.json`` -- so the
snapshot records what the run was started with.

Types:

=============  ==============================================================
type           value
=============  ==============================================================
``string``     one line of text
``text``       text, several lines
``int``        an integer
``bool``       true / false
``enum``       one of ``choices``
``list``       a list of strings (form/CLI text: one per line, or commas)
``invariants`` a list of ``{text, probe}``: rules the run must keep
``acceptance`` a list of ``{text, probe}``: what must hold when it finishes
=============  ==============================================================

What :func:`apply` does with the values:

- ``data['inputs']`` holds every input (prompts read ``${inputs.goal}``).
- Each ``invariants`` item is a rule ``input-invariant-<n>`` (why: "user-
  declared invariant", delivered to every task). An item with a ``probe`` is
  also an ``always`` monitor of the same name: the probe exiting 0 is the
  invariant holding; a non-zero exit halts the run (``on_violation: halt``).
- The ``acceptance`` items are listed in ``spec['acceptance']`` (``[{id,
  text, probe}]``), which the runtime checks before the run finishes ok (a
  probe's exit 0 = met), and together are one rule ``input-acceptance``
  delivered to every task, so the last task's prompt carries them for the
  agent's own check of the text-only items.

Probes are shell commands a person typed. They run as written; no run value
is ever substituted into them (the same rule as ``verify``).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import ir

T_STRING = "string"
T_TEXT = "text"
T_INT = "int"
T_BOOL = "bool"
T_ENUM = "enum"
T_LIST = "list"
T_INVARIANTS = "invariants"
T_ACCEPTANCE = "acceptance"
TYPES = (T_STRING, T_TEXT, T_INT, T_BOOL, T_ENUM, T_LIST, T_INVARIANTS, T_ACCEPTANCE)
#: Types whose value is a list.
LIST_TYPES = (T_LIST, T_INVARIANTS, T_ACCEPTANCE)
#: Types whose items are ``{text, probe}``.
CHECK_TYPES = (T_INVARIANTS, T_ACCEPTANCE)

KEYS = ("type", "label", "help", "required", "default", "choices", "placeholder")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RULE_INVARIANT = "input-invariant-{n}"
RULE_ACCEPTANCE = "input-acceptance"
INVARIANT_WHY = "user-declared invariant"
ACCEPTANCE_WHY = "user-declared acceptance criteria"
#: Seconds between two runs of an invariant's probe.
MONITOR_EVERY = 300
#: The header the compiler puts above delivered rule texts (frontend).
RULES_HEADER = "── 규칙 ──"

_TRUE = ("true", "yes", "y", "1", "on")
_FALSE = ("false", "no", "n", "0", "off", "")


class InputsError(ValueError):
    """A declaration that does not hold together (compile time)."""


# --------------------------------------------------------------------------- #
# declaration
# --------------------------------------------------------------------------- #
def schema(raw: Any) -> Dict[str, Dict[str, Any]]:
    """The ``inputs:`` declaration checked and filled in: every entry has
    every key of :data:`KEYS` (``required`` defaults to false). Raises
    :class:`InputsError` naming the input."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputsError(f"inputs must be a mapping of name -> declaration, got {type(raw).__name__}")
    out: Dict[str, Dict[str, Any]] = {}
    for name, decl in raw.items():
        name = str(name)
        if not NAME_RE.match(name):
            raise InputsError(f"input {name!r}: a name is letters, digits and _ (not starting "
                              f"with a digit)")
        if isinstance(decl, str):
            decl = {"type": decl}
        if decl is None:
            decl = {}
        if not isinstance(decl, dict):
            raise InputsError(f"input {name!r}: the declaration must be a mapping")
        unknown = [k for k in decl if k not in KEYS]
        if unknown:
            raise InputsError(f"input {name!r}: unknown key {unknown[0]!r} (allowed: {', '.join(KEYS)})")
        typ = decl.get("type", T_STRING)
        if typ not in TYPES:
            raise InputsError(f"input {name!r}: unknown type {typ!r} (allowed: {', '.join(TYPES)})")
        required = decl.get("required", False)
        if not isinstance(required, bool):
            raise InputsError(f"input {name!r}: required must be true or false")
        for key in ("label", "help", "placeholder"):
            if decl.get(key) is not None and not isinstance(decl[key], str):
                raise InputsError(f"input {name!r}: {key} must be a string")
        choices = decl.get("choices")
        if typ == T_ENUM:
            if not isinstance(choices, list) or not choices \
                    or not all(isinstance(c, str) and c for c in choices):
                raise InputsError(f"input {name!r}: an enum needs choices, a list of strings")
            if len(set(choices)) != len(choices):
                raise InputsError(f"input {name!r}: choices repeat a value")
        elif choices is not None:
            raise InputsError(f"input {name!r}: choices apply to type enum only")
        entry = {
            "type": typ,
            "label": decl.get("label") or name,
            "help": decl.get("help") or "",
            "required": required,
            "default": None,
            "choices": list(choices) if typ == T_ENUM else None,
            "placeholder": decl.get("placeholder") or "",
        }
        if decl.get("default") is not None:
            value, err = _coerce(entry, decl["default"], strict=True)
            if err:
                raise InputsError(f"input {name!r}: default {decl['default']!r} {err}")
            entry["default"] = value
        out[name] = entry
    return out


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
def _empty(decl: Dict[str, Any]) -> Any:
    if decl["type"] in LIST_TYPES:
        return []
    if decl["type"] == T_BOOL:
        return False
    return None


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) \
        or (isinstance(value, (list, dict)) and not value)


def _text_items(text: str) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    if len([ln for ln in lines if ln]) <= 1 and "," in text:
        lines = [p.strip() for p in text.split(",")]
    return [ln for ln in lines if ln]


def _check_item(item: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        return None, "items must be {text, probe} objects or strings"
    extra = [k for k in item if k not in ("text", "probe")]
    if extra:
        return None, f"unknown item key {extra[0]!r} (allowed: text, probe)"
    text = item.get("text")
    probe = item.get("probe")
    if not isinstance(text, str) or not text.strip():
        return None, "every item needs a non-empty text"
    if probe is not None and not isinstance(probe, str):
        return None, "a probe must be a shell command string"
    probe = probe.strip() if isinstance(probe, str) and probe.strip() else None
    return {"text": text.strip(), "probe": probe}, None


def _coerce(decl: Dict[str, Any], value: Any, strict: bool = False) -> Tuple[Any, Optional[str]]:
    """``(value, None)`` or ``(None, why)``. ``strict`` (a declared default)
    refuses the text spellings a form or CLI would send."""
    typ = decl["type"]
    if typ in (T_STRING, T_TEXT):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, "is not text"
        if strict and not isinstance(value, str):
            return None, "is not text"
        value = str(value)
        if typ == T_STRING and "\n" in value.strip():
            return None, "must be one line (use type text for several)"
        return value, None
    if typ == T_INT:
        if isinstance(value, bool):
            return None, "is not an integer"
        if isinstance(value, int):
            return value, None
        if isinstance(value, str) and not strict and re.match(r"^\s*-?\d+\s*$", value):
            return int(value), None
        return None, "is not an integer"
    if typ == T_BOOL:
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str) and not strict and value.strip().lower() in _TRUE + _FALSE:
            return value.strip().lower() in _TRUE, None
        return None, "is not true or false"
    if typ == T_ENUM:
        if isinstance(value, str) and value in decl["choices"]:
            return value, None
        return None, f"is not one of {', '.join(decl['choices'])}"
    if typ == T_LIST:
        if isinstance(value, str) and not strict:
            return _text_items(value), None
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return [x.strip() for x in value if x.strip()], None
        return None, "is not a list of strings"
    # invariants / acceptance
    if isinstance(value, str) and not strict:
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError as exc:
                return None, f"is not valid JSON: {exc}"
        else:
            value = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not isinstance(value, list):
        return None, "is not a list of {text, probe} items"
    items = []
    for raw in value:
        if isinstance(raw, dict) and _is_empty(raw.get("text")) and _is_empty(raw.get("probe")):
            continue  # an empty form row
        item, err = _check_item(raw)
        if err:
            return None, err
        items.append(item)
    return items, None


def normalize(schema_: Dict[str, Dict[str, Any]], raw: Optional[Dict[str, Any]]
              ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """``(values, errors)`` for the raw values a form, CLI or tool sent.

    Strings are coerced (``int``, ``bool``, ``list`` from newline or comma
    text, check lists from JSON or one text per line); an absent or empty
    value takes the default, else the type's empty value (``None``, ``[]``,
    ``false``). ``errors`` maps an input name (or an unknown key) to why it
    was refused: a required input with no value (``None``, empty text and
    an empty list do not satisfy it), a value of the wrong type, a key the
    schema does not declare."""
    raw = raw or {}
    errors: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return {}, {"_": "inputs must be an object of name -> value"}
    for key in raw:
        if key not in schema_:
            errors[str(key)] = ("unknown input (declared: "
                                f"{', '.join(schema_) or 'none'})")
    for name, decl in schema_.items():
        value = raw.get(name)
        if not _is_empty(value):
            value, err = _coerce(decl, value)
            if err:
                errors[name] = f"{decl['label']} {err}"
                continue
        if _is_empty(value):
            value = copy.deepcopy(decl["default"]) if decl["default"] is not None \
                else _empty(decl)
        if decl["required"] and _is_empty(value):
            errors[name] = f"{decl['label']} is required"
            continue
        values[name] = value
    return values, errors


def check(schema_: Dict[str, Dict[str, Any]], raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """:func:`normalize` that raises :class:`InputsError` (message lists
    every field; ``.errors`` holds the mapping)."""
    values, errors = normalize(schema_, raw)
    if errors:
        exc = InputsError("invalid inputs: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        exc.errors = errors  # type: ignore[attr-defined]
        raise exc
    return values


# --------------------------------------------------------------------------- #
# folding into the chart
# --------------------------------------------------------------------------- #
def _probe_name(rule_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", rule_id)


def _deliver(chart: ir.Chart, rule_id: str, text: str) -> None:
    for node in chart.nodes.values():
        if node.type != ir.T_TASK:
            continue
        prompt = node.props.get("prompt") or ""
        if RULES_HEADER in prompt:
            prompt = prompt.rstrip("\n") + "\n" + text + "\n"
        else:
            prompt = prompt.rstrip("\n") + ("\n\n" if prompt.strip() else "") \
                + RULES_HEADER + "\n" + text + "\n"
        node.props["prompt"] = prompt
        node.props["rules"] = list(node.props.get("rules") or []) + [rule_id]
        node.props["text_id"] = ir.text_id(node.props.get("title") or "", prompt,
                                           node.props.get("done_when") or "")


def apply(chart: ir.Chart, values: Dict[str, Any]) -> ir.Chart:
    """A copy of ``chart`` with the start inputs folded in (module doc).
    ``values`` are :func:`normalize`'s. The original chart is not changed."""
    out = ir.Chart.from_dict(copy.deepcopy(chart.to_dict()))
    if out.meta.get("inputs_applied"):
        raise InputsError(f"{chart.name}: inputs were already applied to this chart")
    decls = out.meta.get("inputs") or {}
    out.data = dict(out.data or {})
    out.data["inputs"] = copy.deepcopy(values)
    out.meta["inputs_applied"] = True
    out.rules = dict(out.rules or {})
    out.spec = dict(out.spec or {})
    n = 0
    acceptance: List[Dict[str, Any]] = []
    for name, decl in decls.items():
        items = values.get(name) or []
        if decl.get("type") == T_INVARIANTS:
            for item in items:
                n += 1
                rid = RULE_INVARIANT.format(n=n)
                text = f"── 사용자 불변식 {n} ({decl.get('label') or name}) ──\n{item['text']}"
                if item.get("probe"):
                    text += f"\n(기계 확인: `{item['probe']}` — 종료 코드 0이 지켜짐이다)"
                out.rules[rid] = {"text": text, "why": INVARIANT_WHY, "deliver": "all",
                                  "effect": None}
                _deliver(out, rid, text)
                if item.get("probe"):
                    pname = _probe_name(rid)
                    mons = dict(out.spec.get("monitors") or {})
                    mons[rid] = {"always": f"$probe.{pname} != null",
                                 "probe": {pname: item["probe"]},
                                 "every": MONITOR_EVERY, "on_violation": "halt"}
                    out.spec["monitors"] = mons
        elif decl.get("type") == T_ACCEPTANCE:
            for item in items:
                acceptance.append({"id": f"input-acceptance-{len(acceptance) + 1}",
                                   "input": name, "text": item["text"],
                                   "probe": item.get("probe")})
    if acceptance:
        out.spec["acceptance"] = list(out.spec.get("acceptance") or []) + acceptance
        lines = ["── 완료 기준 (사용자 선언) — 런이 끝나기 전에 전부 충족돼야 한다 ──"]
        for item in acceptance:
            how = f" — 기계 확인: `{item['probe']}`(종료 코드 0)" if item.get("probe") \
                else " — 기계 확인 없음: 마지막 작업에서 스스로 대조해 보고에 적는다"
            lines.append(f"- {item['text']}{how}")
        text = "\n".join(lines)
        out.rules[RULE_ACCEPTANCE] = {"text": text, "why": ACCEPTANCE_WHY, "deliver": "all",
                                      "effect": None}
        _deliver(out, RULE_ACCEPTANCE, text)
    return out


def view(schema_: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The schema as an ordered list (what a form draws)."""
    return [{"name": name, **copy.deepcopy(decl)} for name, decl in (schema_ or {}).items()]


__all__ = ("TYPES", "InputsError", "schema", "normalize", "check", "apply", "view")
