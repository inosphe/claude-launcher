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

A ``string`` input may declare ``options: {from, of, empty}``: its value is
picked from a list the host fills at run time instead of a fixed ``choices``
(the form draws a combobox, docs/cflow-v2.md 2.6). ``from`` names the list:
``profiles`` (the launcher's profiles), ``harnesses`` (the harnesses a
profile may run) or ``models`` (the model presets and model choices a
profile's harness takes). ``harnesses`` and ``models`` depend on a profile:
``of`` names the ``profiles`` input that holds it; without ``of``, or while
that input is empty, the profile is what an empty value of it means.
``empty`` on a ``profiles`` input says what that is: ``session`` (default,
the run's own session's profile) or ``delegate`` (the daemon's
``delegate_profile`` setting). The lists come from a *catalog* the caller
hands :func:`normalize` and :func:`settle` (``claude_launcher.input_options``
builds one); without a catalog the engine does not check them -- the
engine has no launcher config of its own. A value outside its list is
refused like an enum's, and so is a parent change that leaves a dependent's
value outside the parent's new list.

An input declared ``set_by: [agent]`` may be written once more after the
run started, while it is still empty, by the run's driver
(``runtime.set_input`` through ``flow_input`` / ``claunch cflow2 input``).
An input declared ``set_by: [user]`` may be changed by a person at any time
(``claunch cflow2 input --user``, the run page, ``POST /api/cflow2/input``);
it may be a ``bool``. :func:`settle` checks either write the way
:func:`normalize` checks a start.

Probes are shell commands a person typed. They run as written; no run value
is ever substituted into them (the same rule as ``verify``).

Constraints across inputs (top-level ``input_constraints:``) are checked by
:func:`constraints` at compile time and kept in ``chart.meta['input_constraints']``.
Each is ``{assert, fields, message}``: ``assert`` is an expression
(``cflow2/expr.py``) over ``$inputs.<name>`` that must be true for the values
to be accepted; ``fields`` (default: the inputs it reads) are the inputs a
form marks when it is false. :func:`normalize` evaluates them after the
per-field checks, so a start, the dashboard's ``POST /api/cflow2/inputs/check``
and the CLI refuse the same values with the same message.
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

KEYS = ("type", "label", "help", "required", "default", "choices", "placeholder", "precheck",
        "set_by", "options")
#: What ``options.from`` may name (the module docstring): the lists a host's
#: catalog answers.
OPT_PROFILES = "profiles"
OPT_HARNESSES = "harnesses"
OPT_MODELS = "models"
OPTION_SOURCES = (OPT_PROFILES, OPT_HARNESSES, OPT_MODELS)
#: Lists that are a profile's, and so take ``of``.
OPTION_DEPENDENT = (OPT_HARNESSES, OPT_MODELS)
OPTION_KEYS = ("from", "of", "empty")
#: What an empty ``profiles`` input stands for (``options.empty``).
EMPTY_SESSION = "session"
EMPTY_DELEGATE = "delegate"
EMPTY_MEANINGS = (EMPTY_SESSION, EMPTY_DELEGATE)
#: What ``precheck:`` may name: a readiness check the start form runs before
#: the user may turn the input on (``gh``: GET /api/tools/gh?cwd= says the
#: client is installed and signed in to the repository's host). Advisory --
#: the run checks again when it acts on the value.
PRECHECKS = ("gh",)
#: Who ``set_by:`` may let write an input after the run started
#: (:func:`settle`): ``agent`` is the run's driver, through ``flow_input`` or
#: ``claunch cflow2 input``, and fills an empty input once; ``user`` is a
#: person, through ``claunch cflow2 input --user``, the dashboard's run page
#: or ``POST /api/cflow2/input``, and may change the value at any time
#: (claunch-umq1f).
SETTER_AGENT = "agent"
SETTER_USER = "user"
SETTERS = (SETTER_AGENT, SETTER_USER)
#: Types an input the agent fills after start may have. ``bool`` has no
#: empty value (an unset one is false), and the check lists are folded into
#: rules and monitors at start (:func:`apply`), which a later write would not
#: redo.
SETTABLE_TYPES = (T_STRING, T_TEXT, T_INT, T_ENUM, T_LIST)
#: Types a person may change after start: the agent's, and ``bool`` -- a
#: person overwrites, so a value that is never empty is no obstacle. The
#: check lists stay out for the reason above.
USER_SETTABLE_TYPES = SETTABLE_TYPES + (T_BOOL,)
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


CONSTRAINT_KEYS = ("assert", "fields", "message")
#: The error key a violated constraint is reported under when it names no
#: field (an ``errors`` mapping is name -> why).
CONSTRAINT_ERROR_KEY = "_constraints"


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
        precheck = decl.get("precheck")
        if precheck is not None and precheck not in PRECHECKS:
            raise InputsError(f"input {name!r}: unknown precheck {precheck!r} "
                              f"(allowed: {', '.join(PRECHECKS)})")
        set_by = decl.get("set_by")
        if set_by is not None:
            if isinstance(set_by, str):
                set_by = [set_by]
            if not isinstance(set_by, list) or not all(isinstance(x, str) for x in set_by):
                raise InputsError(f"input {name!r}: set_by must be a list of "
                                  f"{', '.join(SETTERS)}")
            bad = [x for x in set_by if x not in SETTERS]
            if bad:
                raise InputsError(f"input {name!r}: unknown set_by {bad[0]!r} "
                                  f"(allowed: {', '.join(SETTERS)})")
            if SETTER_AGENT in set_by and typ not in SETTABLE_TYPES:
                raise InputsError(f"input {name!r}: set_by {SETTER_AGENT} applies to types "
                                  f"{', '.join(SETTABLE_TYPES)}, not {typ}")
            if SETTER_USER in set_by and typ not in USER_SETTABLE_TYPES:
                raise InputsError(f"input {name!r}: set_by {SETTER_USER} applies to types "
                                  f"{', '.join(USER_SETTABLE_TYPES)}, not {typ}")
        options = _options_decl(name, typ, decl.get("options"))
        entry = {
            "type": typ,
            "label": decl.get("label") or name,
            "help": decl.get("help") or "",
            "required": required,
            "default": None,
            "choices": list(choices) if typ == T_ENUM else None,
            "placeholder": decl.get("placeholder") or "",
            "precheck": precheck,
            "set_by": sorted(set(set_by or [])),
            "options": options,
        }
        if decl.get("default") is not None:
            value, err = _coerce(entry, decl["default"], strict=True)
            if err:
                raise InputsError(f"input {name!r}: default {decl['default']!r} {err}")
            entry["default"] = value
        out[name] = entry
    for name, entry in out.items():
        parent = (entry.get("options") or {}).get("of")
        if parent is None:
            continue
        pdecl = out.get(parent)
        if pdecl is None:
            raise InputsError(f"input {name!r}: options.of names {parent!r}, which is not "
                              f"declared")
        if ((pdecl.get("options") or {}).get("from")) != OPT_PROFILES:
            raise InputsError(f"input {name!r}: options.of names {parent!r}, which is not an "
                              f"input with options from {OPT_PROFILES}")
    return out


def _options_decl(name: str, typ: str, raw: Any) -> Optional[Dict[str, Any]]:
    """``options:`` checked: ``{from, of, empty}`` with every key present
    (``of``/``empty`` ``None`` where they do not apply)."""
    if raw is None:
        return None
    if typ != T_STRING:
        raise InputsError(f"input {name!r}: options apply to type string only")
    if isinstance(raw, str):
        raw = {"from": raw}
    if not isinstance(raw, dict):
        raise InputsError(f"input {name!r}: options must be a mapping (from, of, empty)")
    unknown = [k for k in raw if k not in OPTION_KEYS]
    if unknown:
        raise InputsError(f"input {name!r}: unknown options key {unknown[0]!r} "
                          f"(allowed: {', '.join(OPTION_KEYS)})")
    src = raw.get("from")
    if src not in OPTION_SOURCES:
        raise InputsError(f"input {name!r}: options.from must be one of "
                          f"{', '.join(OPTION_SOURCES)}")
    of = raw.get("of")
    if of is not None:
        if src not in OPTION_DEPENDENT:
            raise InputsError(f"input {name!r}: options.of applies to "
                              f"{', '.join(OPTION_DEPENDENT)} only")
        if not isinstance(of, str) or not NAME_RE.match(of):
            raise InputsError(f"input {name!r}: options.of must name another input")
        if of == name:
            raise InputsError(f"input {name!r}: options.of names the input itself")
    empty = raw.get("empty")
    if empty is not None:
        if src != OPT_PROFILES:
            raise InputsError(f"input {name!r}: options.empty applies to {OPT_PROFILES} only")
        if empty not in EMPTY_MEANINGS:
            raise InputsError(f"input {name!r}: options.empty must be one of "
                              f"{', '.join(EMPTY_MEANINGS)}")
    elif src == OPT_PROFILES:
        empty = EMPTY_SESSION
    return {"from": src, "of": of, "empty": empty}


def _input_refs(text: str) -> List[str]:
    """The input names an assert reads (``$inputs.<name>``), in order."""
    from . import expr

    names: List[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, tuple) or not node:
            return
        if node[0] == "ref":
            if node[1] == "inputs" and node[2] and node[2][0][0] == "field":
                if node[2][0][1] not in names:
                    names.append(node[2][0][1])
            for seg in node[2]:
                if seg[0] == "index":
                    walk(seg[1])
            return
        for item in node[1:]:
            if isinstance(item, (list, tuple)):
                if isinstance(item, list):
                    for sub in item:
                        walk(sub)
                else:
                    walk(item)

    walk(expr.parse(text).ast)
    return names


def constraints(raw: Any, schema_: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ``input_constraints:`` declaration checked against the inputs
    schema: a list of ``{assert, fields, message}``. The assert must parse,
    read only ``$inputs`` (no collection functions) and name declared
    inputs; ``fields`` defaults to the inputs it reads. Raises
    :class:`InputsError` naming the constraint (1-based)."""
    from . import expr

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InputsError("input_constraints must be a list of {assert, fields, message}")
    out: List[Dict[str, Any]] = []
    for n, item in enumerate(raw, 1):
        where = f"input_constraints[{n}]"
        if isinstance(item, str):
            item = {"assert": item}
        if not isinstance(item, dict):
            raise InputsError(f"{where}: must be a mapping with assert (or the assert text)")
        unknown = [k for k in item if k not in CONSTRAINT_KEYS]
        if unknown:
            raise InputsError(f"{where}: unknown key {unknown[0]!r} "
                              f"(allowed: {', '.join(CONSTRAINT_KEYS)})")
        text = item.get("assert")
        if not isinstance(text, str) or not text.strip():
            raise InputsError(f"{where}: assert must be a non-empty expression")
        try:
            roots = expr.references(text)
            calls = expr.collection_calls(text)
            reads = _input_refs(text)
        except expr.ExprError as exc:
            raise InputsError(f"{where}: assert does not parse: {exc}") from None
        other = sorted(r for r in roots if r != "inputs")
        if other:
            raise InputsError(f"{where}: assert may read only $inputs, not ${other[0]}")
        if calls:
            raise InputsError(f"{where}: assert may not call collection functions")
        undeclared = [r for r in reads if r not in schema_]
        if undeclared:
            raise InputsError(f"{where}: $inputs.{undeclared[0]} is not a declared input")
        fields = item.get("fields")
        if fields is None:
            fields = reads
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise InputsError(f"{where}: fields must be a list of input names")
        bad = [f for f in fields if f not in schema_]
        if bad:
            raise InputsError(f"{where}: field {bad[0]!r} is not a declared input")
        message = item.get("message")
        if message is not None and (not isinstance(message, str) or not message.strip()):
            raise InputsError(f"{where}: message must be a non-empty string")
        out.append({"assert": text.strip(), "fields": list(fields),
                    "message": (message or "").strip() or f"constraint not met: {text.strip()}"})
    return out


def violations(constraints_: Optional[List[Dict[str, Any]]], values: Dict[str, Any]
               ) -> List[Dict[str, Any]]:
    """The constraints that do not hold for ``values`` (normalized), each
    ``{assert, fields, message}``. An assert that cannot be evaluated counts
    as not holding, with the evaluation error appended to its message."""
    from . import expr

    out: List[Dict[str, Any]] = []
    for c in constraints_ or []:
        try:
            ok = expr.truthy(expr.evaluate(c["assert"], {"inputs": values}))
            message = c["message"]
        except expr.ExprError as exc:
            ok, message = False, f"{c['message']} (cannot evaluate: {exc})"
        if not ok:
            out.append({**c, "message": message})
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


# --------------------------------------------------------------------------- #
# options lists (a host's catalog)
# --------------------------------------------------------------------------- #
#: A catalog answers the lists ``options:`` names. Any object with these
#: methods; ``None`` from a profile's list means "cannot say" (an unknown or
#: unreadable profile -- its own input reports that), and the value is not
#: checked against it:
#:
#: - ``profiles() -> list``
#: - ``harnesses(profile) -> list | None``
#: - ``models(profile) -> list | None``
#: - ``empty_profile(meaning) -> str | None`` -- what an empty ``profiles``
#:   input stands for (:data:`EMPTY_MEANINGS`); ``None`` when unknown.
Catalog = Any


def option_profile(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any], name: str,
                   catalog: Catalog) -> Optional[str]:
    """The profile input ``name``'s list is of (its ``options.of`` value,
    or what an empty one means); ``None`` for a ``profiles`` list."""
    opts = (schema_.get(name) or {}).get("options") or {}
    if opts.get("from") not in OPTION_DEPENDENT:
        return None
    of = opts.get("of")
    value = (values or {}).get(of) if of else None
    if not _is_empty(value):
        return str(value)
    meaning = ((schema_.get(of) or {}).get("options") or {}).get("empty") if of else None
    return catalog.empty_profile(meaning or EMPTY_SESSION)


def option_items(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any], name: str,
                 catalog: Catalog) -> Optional[List[str]]:
    """The values input ``name`` may take now; ``None`` when the catalog
    cannot say (or the input declares no ``options``)."""
    opts = (schema_.get(name) or {}).get("options") or {}
    src = opts.get("from")
    if src == OPT_PROFILES:
        return list(catalog.profiles())
    if src not in OPTION_DEPENDENT:
        return None
    profile = option_profile(schema_, values, name, catalog)
    if not profile:
        return None
    got = catalog.harnesses(profile) if src == OPT_HARNESSES else catalog.models(profile)
    return None if got is None else list(got)


def _option_error(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any], name: str,
                  catalog: Catalog) -> Optional[str]:
    decl = schema_[name]
    value = (values or {}).get(name)
    if not decl.get("options") or _is_empty(value):
        return None
    items = option_items(schema_, values, name, catalog)
    if items is None or value in items:
        return None
    shown = ", ".join(items) or "none"
    src = decl["options"]["from"]
    if src == OPT_PROFILES:
        return f"{decl['label']} {value!r} is not a profile (profiles: {shown})"
    profile = option_profile(schema_, values, name, catalog)
    what, verb = ("a harness", "may run") if src == OPT_HARNESSES else ("a model", "takes")
    return f"{decl['label']} {value!r} is not {what} profile {profile!r} {verb} (choices: {shown})"


def option_errors(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any],
                  catalog: Catalog) -> Dict[str, str]:
    """name -> why, for every input with ``options`` whose value is outside
    its list. Inputs absent from ``values`` (already refused) are skipped."""
    out: Dict[str, str] = {}
    if catalog is None:
        return out
    for name in schema_ or {}:
        if name not in (values or {}):
            continue
        err = _option_error(schema_, values, name, catalog)
        if err:
            out[name] = err
    return out


def option_lists(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any],
                 catalog: Catalog) -> Dict[str, Dict[str, Any]]:
    """What a form fills its comboboxes with: name -> ``{from, profile,
    items}`` for every input with ``options`` (``items`` ``None`` when the
    catalog cannot say, ``profile`` the profile a dependent list is of)."""
    out: Dict[str, Dict[str, Any]] = {}
    if catalog is None:
        return out
    for name, decl in (schema_ or {}).items():
        if not decl.get("options"):
            continue
        out[name] = {"from": decl["options"]["from"],
                     "profile": option_profile(schema_, values, name, catalog),
                     "items": option_items(schema_, values, name, catalog)}
    return out


def normalize(schema_: Dict[str, Dict[str, Any]], raw: Optional[Dict[str, Any]],
              constraints_: Optional[List[Dict[str, Any]]] = None,
              catalog: Catalog = None
              ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """``(values, errors)`` for the raw values a form, CLI or tool sent.

    Strings are coerced (``int``, ``bool``, ``list`` from newline or comma
    text, check lists from JSON or one text per line); an absent or empty
    value takes the default, else the type's empty value (``None``, ``[]``,
    ``false``). ``errors`` maps an input name (or an unknown key) to why it
    was refused: a required input with no value (``None``, empty text and
    an empty list do not satisfy it), a value of the wrong type, a key the
    schema does not declare.

    ``constraints_`` (:func:`constraints`) are evaluated when every input
    they read passed its own check; a violated one is reported under each
    of its ``fields`` (or :data:`CONSTRAINT_ERROR_KEY` when it has none).

    ``catalog`` (see :data:`Catalog`) checks the inputs that declare
    ``options``; without one they are checked as plain strings."""
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
    for name, err in option_errors(schema_, values, catalog).items():
        errors.setdefault(name, err)
        values.pop(name, None)
    ready = [c for c in constraints_ or []
             if all(r in values for r in _input_refs(c["assert"]))]
    for c in violations(ready, values):
        for key in c["fields"] or [CONSTRAINT_ERROR_KEY]:
            if key not in errors:
                errors[key] = c["message"]
    return values, errors


def check(schema_: Dict[str, Dict[str, Any]], raw: Optional[Dict[str, Any]],
          constraints_: Optional[List[Dict[str, Any]]] = None,
          catalog: Catalog = None) -> Dict[str, Any]:
    """:func:`normalize` that raises :class:`InputsError` (message lists
    every field; ``.errors`` holds the mapping)."""
    values, errors = normalize(schema_, raw, constraints_, catalog)
    if errors:
        exc = InputsError("invalid inputs: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
        exc.errors = errors  # type: ignore[attr-defined]
        raise exc
    return values


def settle(schema_: Dict[str, Dict[str, Any]], values: Dict[str, Any], name: str,
           raw: Any, setter: str,
           constraints_: Optional[List[Dict[str, Any]]] = None,
           catalog: Catalog = None) -> Any:
    """The value ``setter`` may write into the started run's input ``name``
    (docs/cflow-v2.md 2.6), checked the way a start checks it. ``values`` is
    the run's current ``data.inputs``. Raises :class:`InputsError` when the
    input is undeclared, its ``set_by`` does not name ``setter``, the value
    is of the wrong type, or a constraint does not hold with it.

    ``agent`` fills an empty input once: refused when the input already
    holds a value (the message carries it) or the value is empty. ``user``
    overwrites whatever the input holds; an empty value clears it unless the
    input is required.

    With a ``catalog`` an input with ``options`` is checked against its
    list, and a ``profiles`` input is refused when the new value leaves a
    dependent (``options.of`` naming it) holding a value outside its new
    list -- the dependent is changed or cleared first."""
    decl = (schema_ or {}).get(name)
    if decl is None:
        raise InputsError(f"unknown input {name!r} (declared: {', '.join(schema_ or {}) or 'none'})")
    if setter not in (decl.get("set_by") or []):
        raise InputsError(f"input {name!r} is fixed when the run starts; its declaration has "
                          f"no set_by: [{setter}]")
    current = (values or {}).get(name)
    if setter == SETTER_USER:
        if _is_empty(raw) and decl["type"] != T_BOOL:
            if decl.get("required"):
                raise InputsError(f"{decl['label']} is required")
            value = _empty(decl)
        else:
            value, err = _coerce(decl, raw)
            if err:
                raise InputsError(f"{decl['label']} {err}")
            # ' , ' coerces to [] for a list: still a clear, so required holds
            if decl.get("required") and decl["type"] != T_BOOL and _is_empty(value):
                raise InputsError(f"{decl['label']} is required")
    else:
        if not _is_empty(current):
            exc = InputsError(f"input {name!r} already holds "
                              f"{json.dumps(current, ensure_ascii=False)}; "
                              f"it is written only while empty")
            exc.current = current  # type: ignore[attr-defined]
            raise exc
        if _is_empty(raw):
            raise InputsError(f"input {name!r}: an empty value writes nothing")
        value, err = _coerce(decl, raw)
        if err:
            raise InputsError(f"{decl['label']} {err}")
        if _is_empty(value):
            raise InputsError(f"input {name!r}: an empty value writes nothing")
    merged = {**(values or {}), name: value}
    if catalog is not None:
        err = _option_error(schema_, merged, name, catalog)
        if err:
            raise InputsError(err)
        for dep, ddecl in schema_.items():
            if (ddecl.get("options") or {}).get("of") != name:
                continue
            err = _option_error(schema_, merged, dep, catalog)
            if err:
                raise InputsError(f"input {name!r}: {err}; change or clear {dep!r} first")
    ready = [c for c in constraints_ or []
             if name in _input_refs(c["assert"])
             and all(r in merged for r in _input_refs(c["assert"]))]
    broken = violations(ready, merged)
    if broken:
        raise InputsError(f"input {name!r}: {broken[0]['message']}")
    return value


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


__all__ = ("TYPES", "InputsError", "schema", "constraints", "violations", "normalize", "check",
           "settle", "apply", "view", "option_items", "option_errors", "option_lists")
