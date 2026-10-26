"""cflow v2 surface syntax -> statechart IR.

Spec: ``docs/cflow-v2.md`` sections 1-3 and 8. The output is an
:class:`ir.Chart` that has passed :func:`ir.validate`; semantic checks (spec,
effects, reachability) are ``check``'s job.

Decisions this module makes where the spec leaves room:

- A list where a tree is expected is shorthand for ``seq``.
- ``with:`` (call/group) and ``return``'s ``value:`` store each entry as
  ``{"expr": text}`` when the value is a string containing ``$`` (parsed with
  ``expr.parse``), and ``{"lit": value}`` otherwise.
- A task without ``executor:`` gets an empty executor list: it runs wherever
  its nearest delegating ancestor runs (inline at the top).
- A task without ``returns:`` gets ``{"ok": {}}``.
- Rule text is appended to the prompt under ``── 규칙 ──`` for rules listed in
  the task's own ``rules:`` (in that order) and then for rules whose
  ``deliver`` names the task or is ``all`` (in rule declaration order). The
  task's ``rules`` prop holds the ids in the same order. A task naming a rule
  that does not exist keeps the id (``check`` reports ``unknown-rule``).
- ``text_id`` is computed from the final prompt template (fragments expanded,
  rule text appended; ``${...}`` value references left as-is).
- Node ids: an author ``id`` or the task name; a task used twice becomes
  ``work``, ``work#2``. Unnamed nodes get ``<parent>.<index-or-key>``; the
  body root is ``body``. Inside an inlined ``call`` the named ids are prefixed
  with ``<call-site id>/``.
- A procedure (``procedures:`` entry) has keys ``description, params, returns,
  nodes, body``; its tasks are the defining file's ``nodes:`` plus its own
  ``nodes:``. A separate file with ``kind: procedure`` is a procedure named
  after the file, found through ``resolve_include``.
- ``par`` / ``group`` settle default to ``all`` / ``allSettled``.
- ``verify`` is stored as ``{run, timeout}`` (a string or argv list is the
  ``run``; ``command`` is accepted for ``run``); ``ask`` as ``{prompt, from,
  otherwise, default_option, timeout, on_decline, options}`` (see
  :func:`_ask`).
- A ``scope`` node carries its procedure's ``params`` declarations; the
  runtime applies their defaults when the scope starts.
- Commands the engine runs (task ``verify``, wait ``probe``, monitor
  ``probe``) are never templates: ``${...}`` in them is a compile error. A
  value reaches a command through the environment (``CFLOW_IN_<NAME>`` for
  params, ``CFLOW_DATA_<NAME>`` for top-level data scalars), so no run value
  is ever spliced into shell text.
- ``include``/``call``/``group.run`` take a definition *name*: a path (or a
  name holding a path separator or a ``.yaml`` suffix) is refused. The name
  is looked up only through ``resolve_include`` (the v2 layers).
- Top-level ``inputs`` (typed start inputs) is checked by
  :func:`inputs.schema` and kept in ``chart.meta['inputs']``.
- Top-level ``max_visits`` (integer >= 0, default
  :data:`ir.DEFAULT_MAX_VISITS`; 0 is no limit) lands in
  ``chart.meta['max_visits']``.
- Top-level ``collections`` are normalized by :func:`collections.normalize`
  into ``chart.meta['collections']`` (always present, ``{}`` when none).
  ``type:`` names a ``collection_types`` entry of this file or of an
  included file (found by name through the layers, like everything else
  ``include`` brings in). ``sides`` (side run name -> definition name,
  doc 2.9) is kept in ``chart.meta['sides']`` when present.
- A task or combinator mapping may carry ``do:`` (collection effects) and
  ``triggers:`` (daemon side requests). They land in the node's ``do`` and
  ``triggers`` props, only when declared (see :func:`_do_list` and
  :func:`_triggers` for the stored shapes). Values in them are expression
  strings (2.4), parsed here and evaluated by the runtime; a non-string
  scalar becomes its expression text (``3``, ``true``, ``null``).
- ``wait: {until, seconds, consume}`` stores ``until`` (expression text) and
  ``consume`` (list of an address or ``{addr, name}``) on the wait node, only
  when ``until`` is given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

from . import collections as _collections
from . import question as _question
from . import ir
from . import store as _store

ResolveInclude = Callable[[str], Optional[Path]]


class FrontendError(Exception):
    """A v2 definition that does not compile. Messages lead with ``file:line``."""


# ---------------------------------------------------------------------------
# YAML with line numbers


class _Map(dict):
    """A mapping that remembers where it and each of its keys started."""

    line: int = 0
    key_lines: Dict[Any, int]


class _Seq(list):
    line: int = 0
    item_lines: List[int]


class _Loader(yaml.SafeLoader):
    """SafeLoader with line tracking and YAML 1.2 booleans: only
    ``true``/``false`` are booleans, so the common key ``on:`` (and ``yes``,
    ``no``, ``off`` as option names) stay strings."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_map(loader: _Loader, node: yaml.MappingNode) -> _Map:
    loader.flatten_mapping(node)
    out = _Map()
    out.line = node.start_mark.line + 1
    out.key_lines = {}
    for knode, vnode in node.value:
        key = loader.construct_object(knode, deep=True)
        try:
            hash(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(
                None, None, "unhashable mapping key", knode.start_mark
            ) from None
        if key in out:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", knode.start_mark
            )
        out[key] = loader.construct_object(vnode, deep=True)
        out.key_lines[key] = knode.start_mark.line + 1
    return out


def _construct_seq(loader: _Loader, node: yaml.SequenceNode) -> _Seq:
    out = _Seq(loader.construct_object(v, deep=True) for v in node.value)
    out.line = node.start_mark.line + 1
    out.item_lines = [v.start_mark.line + 1 for v in node.value]
    return out


_Loader.add_constructor("tag:yaml.org,2002:map", _construct_map)
_Loader.add_constructor("tag:yaml.org,2002:seq", _construct_seq)


def _load_yaml(text: str, file: str) -> Any:
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{file}:{mark.line + 1}" if mark is not None else file
        problem = getattr(exc, "problem", None) or str(exc)
        raise FrontendError(f"{where}: YAML error: {problem}") from None


def _plain(value: Any) -> Any:
    """Strip the line-tracking subclasses (IR holds plain JSON data)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _line(value: Any, default: int = 0) -> int:
    return int(getattr(value, "line", 0) or default)


def _key_line(mapping: Any, key: Any, default: int = 0) -> int:
    lines = getattr(mapping, "key_lines", None) or {}
    return int(lines.get(key) or _line(mapping, default))


# ---------------------------------------------------------------------------
# vocabulary

TOP_KEYS = (
    "cflow", "name", "description", "kind", "default_role", "filter_roles",
    "default_child_cflow", "priority", "params", "returns", "data", "include",
    "fragments", "rules", "procedures", "nodes", "body", "spec", "max_visits",
    "inputs", "input_constraints", "collections", "collection_types", "question_types",
    "sides", "recur",
)
#: Top-level keys a ``kind: library`` file may carry (docs/cflow-v2.md 2.10).
LIBRARY_KEYS = ("cflow", "name", "description", "kind", "include", "fragments", "rules",
                "procedures", "collection_types", "question_types")
TASK_KEYS = (
    "title", "prompt", "done_when", "verify", "ask", "executor", "needs",
    "returns", "effects", "paths", "budget", "rules", "strict_budget",
    "do", "triggers", "restart", "force_target", "session_workflow", "calls", "sims",
)
#: ``$data.inputs`` is the run's start inputs (docs 2.6): written by the start
#: request and, for an input declared ``set_by: [agent]``, once by the driver
#: through ``flow_input``, or for ``set_by: [user]`` by a person at any time
#: (claunch-umq1f) -- never by a node. A ``bind`` or ``data:`` key of this
#: name is a compile error (claunch-7d4nt).
DATA_INPUTS = "inputs"

COMMON_KEYS = ("id", "executor", "needs", "budget", "bind", "on", "do", "triggers",
               "force_target")
#: ``do:`` operations (docs/cflow-v2.md 2.7) and the moments they run at.
DO_OPS = ("push", "set", "remove", "consume")
MOMENT_ENTER = "enter"
MOMENT_LEAVE = "leave"
MOMENTS = (MOMENT_ENTER, MOMENT_LEAVE)
#: ``triggers:`` actions (2.8). ``push`` is spelled as a ``push:`` key.
TRIGGER_ACTIONS = ("briefing", "checks", "enqueue-landing")
TRIGGER_PUSH = "push"
COMBINATORS = (
    "seq", "waterfall", "cond", "choose", "foreach", "loop", "par", "retry",
    "timeout", "try", "call", "wait", "group",
)
CONTROLS = ("continue", "break", "raise", "return")
#: Sibling keys a combinator/control takes besides the common ones.
SIBLINGS = {
    "try": ("catch", "finally"),
    "call": ("with", "cases"),
    "return": ("value",),
}
RETURN_TYPES = ("str", "int", "float", "bool", "list", "dict", "any")
PROC_KEYS = ("description", "params", "returns", "nodes", "body")
PARAM_KEYS = ("type", "required", "default", "description")
#: A procedure param of this type takes a tree (``with: {p: <tree>}``) that the
#: body places with ``splice: p`` (docs 2.2, block params).
PARAM_BLOCK = "block"
#: The tree mapping head that places a block param.
SPLICE = "splice"
RULE_KEYS = ("text", "why", "deliver", "effect")
SPEC_KEYS = ("invariants", "paths", "ensures", "monitors")
MONITOR_KEYS = ("always", "probe", "every", "on_violation", "when", "within", "expect")
ON_VIOLATION = ("halt", "notify", "record")
END = "end"
FOREACH_DEFAULT_MAX = 1000

_FRAG_RE = re.compile(r"\$\{frag:([^}]*)\}")
#: A value reference in text (``${...}``), refused in commands the engine runs.
_TEMPLATE_RE = re.compile(r"\$\{[^}]*\}")
#: Where a command reads run values instead (docs/cflow-v2.md 2.2 verify).
_ENV_HINT = ("commands are not templates -- read run values from the environment: "
             "CFLOW_IN_<NAME> for params, CFLOW_DATA_<NAME> for top-level data scalars")
#: Default ``wait.undecided_max``: consecutive undecided probe results (exit
#: code other than 0 or 1) before the wait finishes ``undecidable``.
WAIT_UNDECIDED_MAX = 3
_RULES_HEADER = "── 규칙 ──"


# ---------------------------------------------------------------------------
# compile context


@dataclass
class _Proc:
    name: str
    body: Any
    tasks: Dict[str, Tuple[Any, str]]  # name -> (raw task mapping, file)
    params: Dict[str, Any]
    returns: Dict[str, Any]
    file: str
    line: int


@dataclass
class _Scope:
    """What a tree being compiled sits inside."""

    file: str
    tasks: Dict[str, Tuple[Any, str]]
    prefix: str = ""                      # "<call-site>/" inside an inlined call
    loops: Tuple[Tuple[str, str], ...] = ()  # (id, label), innermost last
    trys: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()  # (id, catch outcomes)
    procs: Tuple[str, ...] = ()           # call stack of procedure names
    in_proc: bool = False
    #: the T_SCOPE node of the call this tree sits in (None: the run itself);
    #: a ``return`` here ends that call
    scope_id: Optional[str] = None
    #: block params of the procedure being compiled -> (raw tree, the call
    #: site's scope, line): ``splice`` compiles the tree in that scope
    blocks: Optional[Dict[str, Tuple[Any, Optional["_Scope"], int]]] = None
    #: inside a spliced block: returns name their target explicitly
    in_splice: bool = False
    #: inside a par branch (its own region): a block spliced here does not
    #: reach the call site's loops either
    in_region: bool = False


@dataclass
class _Ctx:
    file: str
    resolve_include: Optional[ResolveInclude]
    fragments: Dict[str, Tuple[str, str, int]] = field(default_factory=dict)  # name -> (text, file, line)
    rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rule_src: Dict[str, str] = field(default_factory=dict)
    procs: Dict[str, _Proc] = field(default_factory=dict)
    proc_src: Dict[str, str] = field(default_factory=dict)
    included: Set[str] = field(default_factory=set)
    including: List[str] = field(default_factory=list)
    nodes: Dict[str, ir.Node] = field(default_factory=dict)
    author_ids: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    frag_cache: Dict[str, str] = field(default_factory=dict)
    #: collection_types name -> (raw declaration, file, line)
    collection_types: Dict[str, Tuple[Any, str, int]] = field(default_factory=dict)
    #: question_types name -> (normalized declaration, file, line)
    question_types: Dict[str, Tuple[Dict[str, Any], str, int]] = field(default_factory=dict)
    #: collections an included file declares -> (raw declaration, file, line):
    #: its procedures may operate on them, so the including chart gets them
    #: unless it declares the same name itself
    included_collections: Dict[str, Tuple[Any, str, int]] = field(default_factory=dict)
    #: ``sides`` of included files -> (declaration, file, line); the
    #: including file's own declaration of a name replaces the included one
    included_sides: Dict[str, Tuple[Any, str, int]] = field(default_factory=dict)
    #: task nodes marked ``force_target`` through their task definition: a
    #: task is reused, so the mark applies where the position can be rebuilt
    #: and is dropped (with a warning) where it cannot (docs 2.11)
    task_marks: Set[str] = field(default_factory=set)

    def err(self, file: str, line: int, msg: str) -> FrontendError:
        return FrontendError(f"{file}:{line}: {msg}" if line else f"{file}: {msg}")


def _where(file: str, line: int) -> str:
    return f"{file}:{line}" if line else file


def _check_keys(ctx: _Ctx, file: str, mapping: Any, allowed, what: str) -> None:
    for key in mapping:
        if key not in allowed:
            raise ctx.err(
                file, _key_line(mapping, key),
                f"unknown key {key!r} in {what} (allowed: {', '.join(allowed)})",
            )


def _need_map(ctx: _Ctx, file: str, value: Any, what: str, line: int = 0) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ctx.err(file, _line(value, line), f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _need_list(ctx: _Ctx, file: str, value: Any, what: str, line: int = 0) -> List[Any]:
    if not isinstance(value, list):
        raise ctx.err(file, _line(value, line), f"{what} must be a list, got {type(value).__name__}")
    return value


def _str_list(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    items = _need_list(ctx, file, value, what, line)
    for item in items:
        if not isinstance(item, str):
            raise ctx.err(file, _line(value, line), f"{what} entries must be strings, got {item!r}")
    return list(items)


def _int(ctx: _Ctx, file: str, value: Any, what: str, line: int, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ctx.err(file, line, f"{what} must be an integer >= {minimum}, got {value!r}")
    return value


def _number(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ctx.err(file, line, f"{what} must be a non-negative number, got {value!r}")
    return value


def _expr_text(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> str:
    if isinstance(value, bool) or value is None:
        text = {True: "true", False: "false", None: "null"}[value]
    elif isinstance(value, (int, float)):
        text = repr(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ctx.err(file, line, f"{what} must be an expression string, got {type(value).__name__}")
    from . import expr as _expr

    try:
        _expr.parse(text)
    except _expr.ExprError as exc:
        raise ctx.err(file, line, f"{what}: bad expression {text!r}: {exc}") from None
    return text


def _values(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> Dict[str, Dict[str, Any]]:
    """``with:`` / ``value:`` entries -> ``{name: {"expr": t} | {"lit": v}}``.
    A string holding ``$`` is an expression, anything else a literal; the
    explicit forms ``{lit: V}`` and ``{expr: E}`` say which (a literal string
    with ``$`` in it, an expression without one)."""
    if value is None:
        return {}
    mapping = _need_map(ctx, file, value, what, line)
    out: Dict[str, Dict[str, Any]] = {}
    for key, raw in mapping.items():
        kline = _key_line(mapping, key, line)
        if isinstance(raw, dict) and len(raw) == 1 and next(iter(raw)) in ("lit", "expr"):
            form = next(iter(raw))
            if form == "lit":
                out[str(key)] = {"lit": _plain(raw["lit"])}
            else:
                out[str(key)] = {"expr": _expr_text(ctx, file, raw["expr"], f"{what}.{key}", kline)}
        elif isinstance(raw, str) and "$" in raw:
            out[str(key)] = {"expr": _expr_text(ctx, file, raw, f"{what}.{key}", kline)}
        else:
            out[str(key)] = {"lit": _plain(raw)}
    return out


def _executor(ctx: _Ctx, file: str, value: Any, line: int) -> List[str]:
    items = _str_list(ctx, file, value, "executor", line)
    removed = [item for item in items if item in ir.REMOVED_EXECUTORS]
    if removed:
        ctx.warnings.append(f"{file}:{line}: executor {', '.join(removed)} was removed "
                            f"from the engine and is skipped")
        items = [item for item in items if item not in ir.REMOVED_EXECUTORS]
    for item in items:
        if item not in ir.EXECUTORS:
            raise ctx.err(file, line, f"unknown executor {item!r} (allowed: {', '.join(ir.EXECUTORS)})")
    return items


def _budget(ctx: _Ctx, file: str, value: Any, line: int) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    mapping = _need_map(ctx, file, value, "budget", line)
    _check_keys(ctx, file, mapping, ("seconds", "usd"), "budget")
    out: Dict[str, Any] = {}
    if "seconds" in mapping:
        out["seconds"] = _int(ctx, file, mapping["seconds"], "budget.seconds", line, 1)
    if "usd" in mapping:
        out["usd"] = _number(ctx, file, mapping["usd"], "budget.usd", line)
    return out or None


def _returns(ctx: _Ctx, file: str, value: Any, line: int) -> Dict[str, Dict[str, str]]:
    mapping = _need_map(ctx, file, value, "returns", line)
    out: Dict[str, Dict[str, str]] = {}
    for outcome, fields in mapping.items():
        oline = _key_line(mapping, outcome, line)
        if fields is None:
            out[str(outcome)] = {}
            continue
        fmap = _need_map(ctx, file, fields, f"returns.{outcome}", oline)
        out[str(outcome)] = {}
        for fname, ftype in fmap.items():
            base = ftype[:-1] if isinstance(ftype, str) and ftype.endswith("?") else ftype
            qname = _question.return_type(ftype) if isinstance(ftype, str) else None
            if qname is not None:
                # question:<type> or question:$params.<p>; which types exist
                # is check's job (unknown-question-type)
                if not (_NAME_RE.match(qname) or _PARAM_REF_BARE.match(qname)):
                    raise ctx.err(file, _key_line(fmap, fname, oline),
                                  f"returns.{outcome}.{fname}: {ftype!r} must name a question "
                                  f"type or $params.<name>")
                out[str(outcome)][str(fname)] = ftype
                continue
            if base not in RETURN_TYPES:
                raise ctx.err(
                    file, _key_line(fmap, fname, oline),
                    f"returns.{outcome}.{fname}: unknown type {ftype!r} "
                    f"(allowed: {', '.join(RETURN_TYPES)}, optional with '?')",
                )
            out[str(outcome)][str(fname)] = ftype
    return out


def _params(ctx: _Ctx, file: str, value: Any, line: int) -> Dict[str, Any]:
    if value is None:
        return {}
    mapping = _need_map(ctx, file, value, "params", line)
    out: Dict[str, Any] = {}
    for name, decl in mapping.items():
        pline = _key_line(mapping, name, line)
        if decl is None:
            decl = {}
        decl = _need_map(ctx, file, decl, f"params.{name}", pline)
        _check_keys(ctx, file, decl, PARAM_KEYS, f"params.{name}")
        if decl.get("type") == PARAM_BLOCK:
            # the default stays a raw tree: it is compiled where it is spliced
            out[str(name)] = {k: (v if k == "default" else _plain(v)) for k, v in decl.items()}
            continue
        out[str(name)] = _plain(decl)
    return out


def _workflow_params(ctx: _Ctx, file: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    out = _params(ctx, file, doc.get("params"), _key_line(doc, "params"))
    for name, decl in out.items():
        if decl.get("type") == PARAM_BLOCK:
            raise ctx.err(file, _key_line(doc, "params"),
                          f"params.{name}: type block is a procedure param (a run is "
                          f"started, not called, so nothing passes it a tree)")
    return out


# ---------------------------------------------------------------------------
# fragments, rules, include, procedures


def _expand(ctx: _Ctx, text: str, file: str, line: int, stack: Tuple[str, ...] = ()) -> str:
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1).strip()
        if name not in ctx.fragments:
            known = ", ".join(sorted(ctx.fragments)) or "(none)"
            raise ctx.err(file, line, f"unknown fragment {name!r} (known: {known})")
        if name in stack:
            cycle = " -> ".join(stack[stack.index(name):] + (name,))
            raise ctx.err(file, line, f"fragment cycle: {cycle}")
        if name not in ctx.frag_cache:
            body, ffile, fline = ctx.fragments[name]
            ctx.frag_cache[name] = _expand(ctx, body, ffile, fline, stack + (name,))
        return ctx.frag_cache[name].rstrip("\n")

    return _FRAG_RE.sub(repl, text)


def _merge_named(ctx: _Ctx, table: Dict[str, Any], srcs: Dict[str, str], name: str,
                 value: Any, where: str, what: str) -> None:
    if name in table:
        prior = srcs.get(name, "?")
        if prior == where:
            return
        raise FrontendError(f"{where}: {what} {name!r} is already defined at {prior}")
    table[name] = value
    srcs[name] = where


def _absorb(ctx: _Ctx, doc: Dict[str, Any], file: str) -> None:
    """Register a document's fragments, rules and procedures (and its includes)."""
    if "include" in doc:
        for name in _str_list(ctx, file, doc["include"], "include", _key_line(doc, "include")):
            _include(ctx, name, file, _key_line(doc, "include"))

    frags = doc.get("fragments")
    if frags is not None:
        frags = _need_map(ctx, file, frags, "fragments", _key_line(doc, "fragments"))
        for name, text in frags.items():
            fline = _key_line(frags, name)
            if not isinstance(text, str):
                raise ctx.err(file, fline, f"fragment {name!r} must be a string")
            prior = ctx.fragments.get(str(name))
            if prior is not None and (prior[1], prior[2]) != (file, fline):
                raise ctx.err(file, fline, f"fragment {name!r} is already defined at "
                                           f"{_where(prior[1], prior[2])}")
            ctx.fragments[str(name)] = (text, file, fline)

    rules = doc.get("rules")
    if rules is not None:
        rules = _need_map(ctx, file, rules, "rules", _key_line(doc, "rules"))
        for rid, raw in rules.items():
            rline = _key_line(rules, rid)
            raw = _need_map(ctx, file, raw, f"rule {rid!r}", rline)
            _check_keys(ctx, file, raw, RULE_KEYS, f"rule {rid!r}")
            if not isinstance(raw.get("text"), str) or not raw.get("text").strip():
                raise ctx.err(file, rline, f"rule {rid!r} needs a non-empty text")
            deliver = raw.get("deliver")
            if deliver is None:
                deliver_v: Any = []
            elif deliver == "all":
                deliver_v = "all"
            else:
                deliver_v = _str_list(ctx, file, deliver, f"rule {rid!r} deliver", rline)
            effect = raw.get("effect")
            if effect is not None:
                effect = _need_map(ctx, file, effect, f"rule {rid!r} effect", rline)
                _check_keys(ctx, file, effect, ("deny", "target"), f"rule {rid!r} effect")
                if not isinstance(effect.get("deny"), str):
                    raise ctx.err(file, rline, f"rule {rid!r} effect needs deny: <effect>")
                effect = _plain(effect)
            entry = {"text": raw["text"], "why": raw.get("why"), "deliver": deliver_v,
                     "effect": effect}
            _merge_named(ctx, ctx.rules, ctx.rule_src, str(rid), entry, _where(file, rline), "rule")

    ctypes = doc.get("collection_types")
    if ctypes is not None:
        ctypes = _need_map(ctx, file, ctypes, "collection_types", _key_line(doc, "collection_types"))
        for tname, raw in ctypes.items():
            tline = _key_line(ctypes, tname)
            prior = ctx.collection_types.get(str(tname))
            if prior is not None and (prior[1], prior[2]) != (file, tline):
                raise ctx.err(file, tline, f"collection type {tname!r} is already defined at "
                                           f"{_where(prior[1], prior[2])}")
            raw = _plain(_need_map(ctx, file, raw, f"collection type {tname!r}", tline))
            try:
                _collections.normalize(str(tname), raw, None, allow_unset_env=True)
            except _collections.CollectionError as exc:
                raise ctx.err(file, tline, f"collection type {tname!r}: "
                                           f"{_collections._strip(str(tname), exc)}") from None
            ctx.collection_types[str(tname)] = (raw, file, tline)

    qtypes = doc.get("question_types")
    if qtypes is not None:
        qtypes = _need_map(ctx, file, qtypes, "question_types", _key_line(doc, "question_types"))
        for tname, raw in qtypes.items():
            tline = _key_line(qtypes, tname)
            prior = ctx.question_types.get(str(tname))
            if prior is not None and (prior[1], prior[2]) != (file, tline):
                raise ctx.err(file, tline, f"question type {tname!r} is already defined at "
                                           f"{_where(prior[1], prior[2])}")
            try:
                decl = _question.normalize(str(tname), _plain(raw))
            except _question.QuestionError as exc:
                raise ctx.err(file, tline, str(exc)) from None
            ctx.question_types[str(tname)] = (decl, file, tline)

    tasks = _file_tasks(ctx, doc, file)
    procs = doc.get("procedures")
    if procs is not None:
        procs = _need_map(ctx, file, procs, "procedures", _key_line(doc, "procedures"))
        for pname, raw in procs.items():
            pline = _key_line(procs, pname)
            raw = _need_map(ctx, file, raw, f"procedure {pname!r}", pline)
            _check_keys(ctx, file, raw, PROC_KEYS, f"procedure {pname!r}")
            if "body" not in raw:
                raise ctx.err(file, pline, f"procedure {pname!r} needs a body")
            own = dict(tasks)
            local = raw.get("nodes")
            if local is not None:
                local = _need_map(ctx, file, local, f"procedure {pname!r} nodes", pline)
                for tname, traw in local.items():
                    if tname in own:
                        raise ctx.err(file, _key_line(local, tname),
                                      f"procedure {pname!r} task {tname!r} shadows a file-level task")
                    own[str(tname)] = (traw, file)
            proc = _Proc(
                name=str(pname), body=raw["body"], tasks=own,
                params=_params(ctx, file, raw.get("params"), pline),
                returns=_plain(_returns(ctx, file, raw["returns"], pline)) if raw.get("returns") is not None else {},
                file=file, line=pline,
            )
            _merge_named(ctx, ctx.procs, ctx.proc_src, str(pname), proc, _where(file, pline), "procedure")


def _file_tasks(ctx: _Ctx, doc: Dict[str, Any], file: str) -> Dict[str, Tuple[Any, str]]:
    raw = doc.get("nodes")
    if raw is None:
        return {}
    raw = _need_map(ctx, file, raw, "nodes", _key_line(doc, "nodes"))
    out: Dict[str, Tuple[Any, str]] = {}
    for name, task in raw.items():
        if name == END:
            raise ctx.err(file, _key_line(raw, name), "'end' is reserved and cannot name a task")
        out[str(name)] = (task, file)
    return out


def _read_doc(ctx: _Ctx, path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FrontendError(f"{path}: cannot read: {exc}") from None
    file = path.name
    doc = _load_yaml(text, file)
    return _check_header(ctx, doc, file)


def _check_header(ctx: _Ctx, doc: Any, file: str) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise FrontendError(f"{file}: a v2 definition must be a mapping")
    if doc.get("cflow") != ir.FORMAT_VERSION:
        raise ctx.err(file, _key_line(doc, "cflow", 1),
                      f"not a cflow v2 definition: the first key must be 'cflow: {ir.FORMAT_VERSION}'")
    _check_keys(ctx, file, doc, TOP_KEYS, "a v2 definition")
    kind = doc.get("kind", ir.KIND_WORKFLOW)
    if kind not in ir.KINDS:
        raise ctx.err(file, _key_line(doc, "kind"), f"kind must be one of {', '.join(ir.KINDS)}")
    if kind == ir.KIND_LIBRARY:
        _check_keys(ctx, file, doc, LIBRARY_KEYS, "a library (kind: library)")
    return doc


_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _definition_name(ctx: _Ctx, name: Any, file: str, line: int, what: str) -> str:
    """``name`` when it is a bare definition name; a path is refused (C3:
    definitions are found by name in the v2 layers, never by path)."""
    if not isinstance(name, str) or not _NAME_RE.match(name) or name.endswith((".yaml", ".yml")) \
            or name in (".", ".."):
        raise ctx.err(file, line, f"{what} {name!r}: give a definition name (a file stem in "
                      f"a v2 layer), not a path")
    return name


def _resolve(ctx: _Ctx, name: str, file: str, line: int, what: str) -> Path:
    name = _definition_name(ctx, name, file, line, what)
    path = ctx.resolve_include(name) if ctx.resolve_include else None
    if path is None:
        raise ctx.err(file, line, f"{what} {name!r}: no v2 definition by that name")
    return Path(path)


def _include(ctx: _Ctx, name: str, file: str, line: int) -> Optional[Dict[str, Any]]:
    path = _resolve(ctx, name, file, line, "include")
    key = str(path.resolve())
    if key in ctx.including:
        raise ctx.err(file, line, f"include cycle through {path.name}")
    if key in ctx.included:
        return None
    ctx.included.add(key)
    ctx.including.append(key)
    try:
        doc = _read_doc(ctx, path)
        _absorb(ctx, doc, path.name)
        cols = doc.get("collections")
        if cols is not None:
            cline = _key_line(doc, "collections", 1)
            cols = _need_map(ctx, path.name, cols, "collections", cline)
            for cname, raw in cols.items():
                # the first include that declares a name keeps it
                ctx.included_collections.setdefault(
                    str(cname), (raw, path.name, _key_line(cols, cname, cline)))
        _absorb_sides(ctx, doc, path.name)
    finally:
        ctx.including.pop()
    return doc


def _absorb_sides(ctx: _Ctx, doc: Dict[str, Any], file: str) -> None:
    """Keep an included file's ``sides``: a procedure it exports may read
    its side runs, so a file that calls that procedure has them too (doc
    2.5). The first include that declares a name keeps it."""
    raw = doc.get("sides")
    if raw is None:
        return
    kline = _key_line(doc, "sides", 1)
    raw = _need_map(ctx, file, raw, "sides", kline)
    for name, decl in raw.items():
        ctx.included_sides.setdefault(
            str(name), (_plain(decl), file, _key_line(raw, name, kline)))


def _procedure(ctx: _Ctx, name: str, file: str, line: int) -> _Proc:
    if name in ctx.procs:
        return ctx.procs[name]
    name = _definition_name(ctx, name, file, line, "call")
    path = ctx.resolve_include(name) if ctx.resolve_include else None
    if path is None:
        known = ", ".join(sorted(ctx.procs)) or "(none)"
        raise ctx.err(file, line, f"unknown procedure {name!r} (known: {known})")
    path = Path(path)
    doc = _read_doc(ctx, path)
    if doc.get("kind") != ir.KIND_PROCEDURE:
        hint = " -- a library is included, not called" if doc.get("kind") == ir.KIND_LIBRARY else ""
        raise ctx.err(file, line, f"{path.name} is not a procedure (kind: procedure){hint}")
    if "body" not in doc:
        raise FrontendError(f"{path.name}: a procedure file needs a body")
    key = str(path.resolve())
    if key not in ctx.included:
        ctx.included.add(key)
        _absorb(ctx, doc, path.name)
    proc = _Proc(
        name=name, body=doc["body"], tasks=_file_tasks(ctx, doc, path.name),
        params=_params(ctx, path.name, doc.get("params"), _key_line(doc, "params")),
        returns=_plain(_returns(ctx, path.name, doc["returns"], _key_line(doc, "returns")))
        if doc.get("returns") is not None else {},
        file=path.name, line=_line(doc, 1),
    )
    _merge_named(ctx, ctx.procs, ctx.proc_src, name, proc, _where(path.name, _line(doc, 1)), "procedure")
    return proc


# ---------------------------------------------------------------------------
# trees


def _alloc(ctx: _Ctx, base: str) -> str:
    if base not in ctx.nodes:
        return base
    n = 2
    while f"{base}#{n}" in ctx.nodes:
        n += 1
    return f"{base}#{n}"


def _add(ctx: _Ctx, node: ir.Node) -> ir.Node:
    ctx.nodes[node.id] = node
    return node


def _compile_tree(ctx: _Ctx, sc: _Scope, item: Any, parent: Optional[str],
                  path_id: str, line: int) -> str:
    """Compile one tree item; return its node id."""
    if isinstance(item, str):
        if item == END:
            nid = _alloc(ctx, path_id)
            _add(ctx, ir.Node(id=nid, type=ir.T_CONTROL, parent=parent,
                              src=_where(sc.file, line), props={"op": ir.CTL_END}))
            return nid
        return _compile_task(ctx, sc, item, parent, line)
    if isinstance(item, list):
        nid = _alloc(ctx, path_id)
        _add(ctx, ir.Node(id=nid, type=ir.T_SEQ, parent=parent,
                          src=_where(sc.file, _line(item, line)),
                          props={"children": [], "pass_prev": False}))
        ctx.nodes[nid].props["children"] = _compile_list(ctx, sc, item, nid, line)
        return nid
    if isinstance(item, dict):
        return _compile_mapping(ctx, sc, item, parent, path_id, _line(item, line))
    raise ctx.err(sc.file, line, f"a tree item must be a task name, a list or a mapping, got {item!r}")


def _compile_list(ctx: _Ctx, sc: _Scope, items: List[Any], parent: str, line: int) -> List[str]:
    lines = getattr(items, "item_lines", None) or []
    out = []
    for i, child in enumerate(items):
        cline = lines[i] if i < len(lines) else _line(items, line)
        out.append(_compile_tree(ctx, sc, child, parent, f"{parent}.{i}", cline))
    return out


def _compile_task(ctx: _Ctx, sc: _Scope, name: str, parent: Optional[str], line: int) -> str:
    if name not in sc.tasks:
        known = ", ".join(sorted(sc.tasks)) or "(none)"
        raise ctx.err(sc.file, line, f"unknown task {name!r} (known: {known}, or 'end')")
    raw, tfile = sc.tasks[name]
    raw = _need_map(ctx, tfile, raw, f"task {name!r}", line)
    tline = _line(raw, line)
    _check_keys(ctx, tfile, raw, TASK_KEYS, f"task {name!r}")
    title = raw.get("title") or name
    prompt = raw.get("prompt") or ""
    if not isinstance(title, str) or not isinstance(prompt, str):
        raise ctx.err(tfile, tline, f"task {name!r}: title and prompt must be strings")
    done_when = raw.get("done_when")
    if done_when is not None and not isinstance(done_when, str):
        raise ctx.err(tfile, tline, f"task {name!r}: done_when must be a string")
    prompt = _expand(ctx, prompt, tfile, _key_line(raw, "prompt", tline))

    own_rules = _str_list(ctx, tfile, raw.get("rules"), f"task {name!r} rules", tline)
    rule_ids = list(dict.fromkeys(own_rules))
    for rid, rule in ctx.rules.items():
        deliver = rule.get("deliver")
        if (deliver == "all" or (isinstance(deliver, list) and name in deliver)) and rid not in rule_ids:
            rule_ids.append(rid)
    texts = [ctx.rules[r]["text"].strip() for r in rule_ids if r in ctx.rules]
    if texts:
        prompt = prompt.rstrip("\n") + ("\n\n" if prompt.strip() else "") + _RULES_HEADER + "\n" + "\n".join(texts) + "\n"

    ask = raw.get("ask")
    if ask is not None:
        ask = _ask(ctx, tfile, name, ask, tline)
    verify = _verify(ctx, tfile, name, raw.get("verify"), _key_line(raw, "verify", tline))
    restart = _restart(ctx, tfile, name, raw.get("restart"), _key_line(raw, "restart", tline))
    strict = raw.get("strict_budget", False)
    if not isinstance(strict, bool):
        raise ctx.err(tfile, tline, f"task {name!r}: strict_budget must be true or false")
    returns = (_returns(ctx, tfile, raw["returns"], _key_line(raw, "returns", tline))
               if raw.get("returns") is not None else {ir.OUTCOME_OK: {}})

    executor = _executor(ctx, tfile, raw.get("executor"), tline)
    session_workflow = _session_workflow(ctx, tfile, name, raw, executor, tline)

    nid = _alloc(ctx, sc.prefix + name)
    node = ir.Node(
        id=nid, type=ir.T_TASK, parent=parent, src=_where(tfile, tline), label=name,
        executor=executor,
        needs=_str_list(ctx, tfile, raw.get("needs"), "needs", tline),
        budget=_budget(ctx, tfile, raw.get("budget"), tline),
        props={
            "title": title,
            "prompt": prompt,
            "text_id": ir.text_id(title, prompt, done_when or ""),
            "done_when": done_when,
            "verify": verify,
            "ask": ask,
            "restart": restart,
            "returns": _plain(returns),
            "effects": _str_list(ctx, tfile, raw.get("effects"), "effects", tline),
            # ``effects: []`` (declared: none) and no key (unknown) read the
            # same in ``effects``; the checker tells them apart by this.
            "effects_declared": raw.get("effects") is not None,
            "paths": _str_list(ctx, tfile, raw.get("paths"), "paths", tline),
            "rules": rule_ids,
            "strict_budget": strict,
        },
    )
    if session_workflow:
        node.props["session_workflow"] = session_workflow
    calls = _calls(ctx, tfile, name, raw, tline)
    if calls:
        node.props["calls"] = calls
    sims = _sims(ctx, tfile, name, raw, tline)
    if sims:
        node.props["sims"] = sims
    if _force_target(ctx, tfile, raw, node, tline):
        ctx.task_marks.add(nid)
    _effects_and_triggers(ctx, tfile, raw, node, tline)
    _add(ctx, node)
    return nid


def _sims(ctx: _Ctx, file: str, name: str, raw: Dict[str, Any], line: int) -> Optional[str]:
    """``sims: deny``: no simulation run while this task is active
    (claunch-efnbt.9, read by :mod:`claude_launcher.sim.run_gate`). The
    checker also refuses such a task naming a simulation procedure in
    ``calls`` (``sims-deny-call``)."""
    value = raw.get("sims")
    if value is not None and value != "deny":
        raise ctx.err(file, _key_line(raw, "sims", line),
                      f"task {name!r}: sims takes one value, 'deny'; got {value!r}")
    return value


def _session_workflow(ctx: _Ctx, file: str, name: str, raw: Dict[str, Any],
                      executor: List[str], line: int) -> Optional[str]:
    """``session_workflow: <v2 workflow>``: the run a ``session`` executor's
    child is started on, instead of none (claunch-pbh7k). A child with a run
    of its own is ended by the daemon when that run reaches ``done`` (the v2
    kill-on-end); one without stays until somebody kills it. The name is
    resolved when the child is spawned, from the layers of the run's cwd."""
    value = raw.get("session_workflow")
    if value is None:
        return None
    where = _key_line(raw, "session_workflow", line)
    if not isinstance(value, str) or not value.strip():
        raise ctx.err(file, where, f"task {name!r}: session_workflow must be a workflow name")
    if ir.EXEC_SESSION not in executor:
        raise ctx.err(file, where, f"task {name!r}: session_workflow names the run of a "
                                   f"'session' executor's child, and executor has no session")
    return value.strip()


def _calls(ctx: _Ctx, file: str, name: str, raw: Dict[str, Any], line: int) -> List[str]:
    """``calls: [<procedure>, ...]``: the procedures the task may call while
    it is active (``flow_call``, docs 2.12). Default deny: a task without
    the key calls nothing. Each entry is a definition name; it is resolved
    from the run's layers when the call is made, not here."""
    where = _key_line(raw, "calls", line)
    names = _str_list(ctx, file, raw.get("calls"), f"task {name!r} calls", where)
    return list(dict.fromkeys(_definition_name(ctx, n, file, where, "calls") for n in names))


def _force_target(ctx: _Ctx, file: str, raw: Dict[str, Any], node: ir.Node, line: int) -> bool:
    """``force_target: true`` (docs 2.11): a person may move the run here.
    Whether the position can be rebuilt is ``ir.validate``'s (force_path);
    a task definition's mark is first narrowed by :func:`_narrow_task_marks`."""
    value = raw.get(ir.FORCE_TARGET)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ctx.err(file, _key_line(raw, ir.FORCE_TARGET, line),
                      f"{ir.FORCE_TARGET} must be true or false")
    if value:
        node.props[ir.FORCE_TARGET] = True
    return value


def _narrow_task_marks(ctx: _Ctx, chart: ir.Chart) -> None:
    """Drop a task definition's ``force_target`` from the uses a move cannot
    rebuild (a catch, a par branch, a foreach body...), with a warning; the
    uses it can rebuild keep it. A mark written on a combinator mapping names
    one position and is refused here, where the error can name its line."""
    for nid, node in sorted(chart.nodes.items()):
        if not node.get(ir.FORCE_TARGET):
            continue
        _, problems = ir.force_path(chart, nid)
        if not problems:
            continue
        if nid not in ctx.task_marks:
            raise FrontendError(f"{node.src or ctx.file}: {ir.FORCE_TARGET} here cannot be a "
                                f"forced move's target: " + "; ".join(problems))
        if problems:
            node.props.pop(ir.FORCE_TARGET, None)
            chart.warnings.append(f"{node.src or ctx.file}: {ir.FORCE_TARGET} of task "
                                  f"{node.label!r} does not apply at {nid}: "
                                  + "; ".join(problems))


VERIFY_KEYS = ("run", "command", "timeout")
ASK_KEYS = ("prompt", "from", "otherwise", "timeout", "on_decline", "options",
            "brief", "format", "choices", "require_reason", "on_unroutable", "urgent")
#: ``${params.NAME}`` -- the one reference ask.format/choices/from may hold.
_PARAM_REF = re.compile(r"^\$\{\s*\$?params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}$")
_PARAM_REF_BARE = re.compile(r"^\$params\.([A-Za-z_][A-Za-z0-9_]*)$")
#: A whole-string value reference (``ask.brief``).
_WHOLE_REF = re.compile(r"^\$\{([^{}]+)\}$")
ASK_OPTION_KEYS = ("outcome", "description")
#: The outcome a declined approval finishes its task with, unless
#: ``on_decline`` names another.
ASK_DECLINED = "declined"
_OUTCOME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
#: The answer every ask accepts besides its options (a responder passing it on).
ASK_ABSTAIN = "abstain"
ASK_APPROVE = "approve"
ASK_DECLINE = "decline"


def _no_template(ctx: _Ctx, file: str, line: int, command: Any, what: str) -> None:
    """Refuse ``${...}`` in a command string or argv list (see module doc)."""
    parts = command if isinstance(command, list) else [command]
    for part in parts:
        if isinstance(part, str):
            m = _TEMPLATE_RE.search(part)
            if m:
                raise ctx.err(file, line, f"{what}: {m.group(0)!r} is not substituted; {_ENV_HINT}")


def _command(ctx: _Ctx, file: str, line: int, value: Any, what: str) -> Any:
    """A command the engine runs: a non-empty string or argv list, no ``${...}``."""
    if isinstance(value, list):
        if not value or not all(isinstance(x, (str, int, float)) and not isinstance(x, bool)
                                for x in value):
            raise ctx.err(file, line, f"{what}: an argv list needs string items")
        value = [str(x) for x in value]
    elif not isinstance(value, str) or not value.strip():
        raise ctx.err(file, line, f"{what} must be a command string or an argv list")
    _no_template(ctx, file, line, value, what)
    return _plain(value)


def _verify(ctx: _Ctx, file: str, name: str, value: Any, line: int) -> Optional[Dict[str, Any]]:
    """``verify`` as ``{run: str | [argv], timeout: float | None}``.

    Spellings: a command string, an argv list, or a mapping ``{run, timeout}``
    (``command`` is accepted for ``run``, the v1 key). ``timeout`` left out is
    ``None``; the runtime applies the v1 default (900 s)."""
    if value is None:
        return None
    timeout: Optional[float] = None
    if isinstance(value, dict):
        _check_keys(ctx, file, value, VERIFY_KEYS, f"task {name!r} verify")
        if "run" in value and "command" in value:
            raise ctx.err(file, line, f"task {name!r}: verify takes 'run' or 'command', not both")
        run = value.get("run", value.get("command"))
        if value.get("timeout") is not None:
            timeout = _number(ctx, file, value["timeout"], "verify.timeout", line)
            if timeout <= 0:
                raise ctx.err(file, line, f"task {name!r}: verify.timeout must be greater than 0")
    else:
        run = value
    if isinstance(run, list):
        if not run or not all(isinstance(x, (str, int, float)) for x in run):
            raise ctx.err(file, line, f"task {name!r}: a verify argv list needs string items")
        run = [str(x) for x in run]
    elif not isinstance(run, str) or not run.strip():
        raise ctx.err(file, line, f"task {name!r}: verify must be a command string, an argv "
                      f"list, or {{run, timeout}}")
    _no_template(ctx, file, line, run, f"task {name!r} verify")
    return {"run": _plain(run), "timeout": timeout}


RESTART_KEYS = ("windows", "linux", "timeout")
#: v1's bounds for a restart command's wall clock (``cflow.model``).
RESTART_TIMEOUT_DEFAULT = 120.0
RESTART_TIMEOUT_MAX = 900.0


def _restart(ctx: _Ctx, file: str, name: str, value: Any, line: int) -> Optional[Dict[str, Any]]:
    """``restart: {windows, linux, timeout}`` -- a project-local restart
    command the daemon's clock runs once per visit of the task (docs 2.2,
    restart). At least one platform; commands are run as written."""
    if value is None:
        return None
    spec = _need_map(ctx, file, value, f"task {name!r} restart", line)
    _check_keys(ctx, file, spec, RESTART_KEYS, f"task {name!r} restart")
    out: Dict[str, Any] = {"windows": None, "linux": None}
    for plat in ("windows", "linux"):
        if spec.get(plat) is not None:
            cmd = spec[plat]
            if not isinstance(cmd, str) or not cmd.strip():
                raise ctx.err(file, line, f"task {name!r}: restart.{plat} must be a command string")
            _no_template(ctx, file, line, cmd, f"task {name!r} restart.{plat}")
            refused = ir.restart_refusal(cmd)
            if refused:
                raise ctx.err(file, line, f"task {name!r}: restart.{plat} {refused}")
            out[plat] = cmd
    if out["windows"] is None and out["linux"] is None:
        raise ctx.err(file, line, f"task {name!r}: restart needs a windows or linux command")
    timeout = RESTART_TIMEOUT_DEFAULT
    if spec.get("timeout") is not None:
        timeout = _number(ctx, file, spec["timeout"], "restart.timeout", line)
        if timeout <= 0 or timeout > RESTART_TIMEOUT_MAX:
            raise ctx.err(file, line, f"task {name!r}: restart.timeout must be in "
                                      f"(0, {RESTART_TIMEOUT_MAX:g}]")
    out["timeout"] = timeout
    return out


def _outcome_name(ctx: _Ctx, file: str, line: int, value: Any, what: str) -> str:
    if not isinstance(value, str) or not _OUTCOME_RE.match(value):
        raise ctx.err(file, line, f"{what} must be an outcome name, got {value!r}")
    return value


def _ask_unroutable(ctx: _Ctx, file: str, name: str, ask: Dict[str, Any],
                    aline: int) -> Optional[str]:
    """``ask.on_unroutable``: the outcome the task finishes with when every
    candidate group ran out because the mesh roster gave no pool at all
    (several meshes and none named, no membership, no daemon) rather than
    because nobody holds the role (claunch-5itbf). Absent: ``otherwise``
    covers both, as before."""
    if ask.get("on_unroutable") is None:
        return None
    out = _outcome_name(ctx, file, aline, ask.get("on_unroutable"),
                        f"task {name!r}: ask.on_unroutable")
    if out == ir.OUTCOME_OK:
        raise ctx.err(file, aline, f"task {name!r}: ask.on_unroutable cannot be 'ok' -- "
                      f"an unasked question is not an answer")
    return out


def _ask_urgent(ctx: _Ctx, file: str, name: str, ask: Dict[str, Any], aline: int) -> bool:
    """``ask.urgent``: the ask wants a person's answer right away, so once it
    falls to a person the dashboard raises it on its notification channel
    (corner card, history, the bell's waiting count) like the daemon's own
    approval gates (claunch-x76l8.1). A literal boolean; default false."""
    value = ask.get("urgent", False)
    if not isinstance(value, bool):
        raise ctx.err(file, aline, f"task {name!r}: ask.urgent must be true or false")
    return value


def _ask(ctx: _Ctx, file: str, name: str, value: Any, line: int) -> Dict[str, Any]:
    """``ask`` normalised: ``{prompt, from: [{role, scope, connect}], otherwise,
    default_option, timeout, on_decline, options}``.

    The ``from``/``otherwise``/``timeout`` trio is read by the v1 parser
    (``cflow.model._parse_delegate``) so both formats accept and refuse the same
    declarations. Two shapes:

    - an approval (no ``options``): answered ``approve`` (the task is entered)
      or ``decline`` (the task finishes with outcome ``on_decline``, default
      ``declined``);
    - a decision (``options: {name: {outcome, description}}``): the answer is
      an option name and the task finishes with that option's ``outcome``
      (default: the option's name). ``otherwise: self:<option>`` takes that
      option when every candidate group ran out.
    """
    from ..cflow import model as v1

    ask = _need_map(ctx, file, value, f"task {name!r} ask", line)
    aline = _line(ask, line)
    _check_keys(ctx, file, ask, ASK_KEYS, f"task {name!r} ask")
    if "brief" in ask or "format" in ask:
        return _ask_brief(ctx, file, name, ask, aline)
    if "require_reason" in ask:
        raise ctx.err(file, aline, f"task {name!r}: ask.require_reason applies to an ask "
                      f"with a brief")
    if "choices" in ask:
        raise ctx.err(file, aline, f"task {name!r}: ask.choices fixes the option names of a "
                      f"brief ask (ask-choices-mismatch); this ask has no brief")
    if "prompt" not in ask:
        raise ctx.err(file, aline, f"task {name!r}: ask needs a prompt")
    prompt = ask.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ctx.err(file, aline, f"task {name!r}: ask.prompt must be a non-empty string")
    raw = _plain(ask)
    if _PARAM_REF.match(str(raw.get("from") or "")) if isinstance(raw.get("from"), str) else False:
        raise ctx.err(file, aline, f"task {name!r}: ask.from may be ${{params.<name>}} only on "
                      f"an ask with a brief")
    try:
        delegate = v1._parse_delegate(raw, f"task {name!r}: ask")
    except v1.WorkflowError as exc:
        raise ctx.err(file, aline, str(exc)) from None
    options: Optional[List[Dict[str, Any]]] = None
    if ask.get("options") is not None:
        oline0 = _key_line(ask, "options", aline)
        omap = _need_map(ctx, file, ask["options"], f"task {name!r} ask.options", oline0)
        if not omap:
            raise ctx.err(file, oline0, f"task {name!r}: ask.options is empty")
        options = []
        folded: Dict[str, str] = {}
        for oname, body in omap.items():
            oline = _key_line(omap, oname, oline0)
            oname = str(oname)
            if not _OUTCOME_RE.match(oname) or oname.lower() == ASK_ABSTAIN:
                raise ctx.err(file, oline, f"task {name!r}: bad ask option name {oname!r}")
            # Answers are matched without regard to case, so two names that
            # differ only in case could not both be chosen.
            if oname.lower() in folded:
                raise ctx.err(file, oline, f"task {name!r}: ask options {folded[oname.lower()]!r} "
                              f"and {oname!r} differ only in case; answers are matched "
                              f"case-insensitively")
            folded[oname.lower()] = oname
            if body is None:
                body = {}
            if isinstance(body, str):
                body = {"description": body}
            body = _need_map(ctx, file, body, f"ask option {oname!r}", oline)
            _check_keys(ctx, file, body, ASK_OPTION_KEYS, f"ask option {oname!r}")
            outcome = _outcome_name(ctx, file, oline, body.get("outcome", oname),
                                    f"task {name!r}: ask option {oname!r} outcome")
            options.append({"name": oname, "outcome": outcome,
                            "description": str(body.get("description") or "")})
        if ask.get("on_decline") is not None:
            raise ctx.err(file, aline, f"task {name!r}: ask.on_decline applies to an approval; "
                          f"an ask with options routes each option to its outcome")
    default = delegate.default_option
    if default is not None:
        if options is None:
            raise ctx.err(file, aline, f"task {name!r}: ask 'otherwise: self:{default}' names "
                          f"an option, and an approval has none -- use 'self' or 'human'")
        if default not in [o["name"] for o in options]:
            raise ctx.err(file, aline, f"task {name!r}: ask 'otherwise: self:{default}' names "
                          f"no option (options: {', '.join(o['name'] for o in options)})")
    on_decline = None
    if options is None:
        on_decline = _outcome_name(ctx, file, aline, raw.get("on_decline", ASK_DECLINED),
                                   f"task {name!r}: ask.on_decline")
        if on_decline == ir.OUTCOME_OK:
            raise ctx.err(file, aline, f"task {name!r}: ask.on_decline cannot be 'ok'")
    return {
        "prompt": prompt,
        "from": [{"role": c.role, "scope": c.scope, "connect": c.connect}
                 for c in delegate.candidates],
        "otherwise": delegate.otherwise,
        "default_option": default,
        "timeout": delegate.timeout,
        "on_decline": on_decline,
        "on_unroutable": _ask_unroutable(ctx, file, name, ask, aline),
        "urgent": _ask_urgent(ctx, file, name, ask, aline),
        "options": options,
    }


def _compile_mapping(ctx: _Ctx, sc: _Scope, m: Dict[str, Any], parent: Optional[str],
                     path_id: str, line: int) -> str:
    if SPLICE in m:
        return _compile_splice(ctx, sc, m, parent, path_id, line)
    heads = [k for k in m if k in COMBINATORS or k in CONTROLS]
    if len(heads) != 1:
        if not heads:
            raise ctx.err(sc.file, line, "a tree mapping needs one combinator "
                          f"({', '.join(COMBINATORS)}) or control ({', '.join(CONTROLS)}); "
                          f"got keys {', '.join(map(str, m))}")
        raise ctx.err(sc.file, line, f"a tree mapping has more than one combinator: {', '.join(heads)}")
    head = heads[0]
    if head in CONTROLS:
        _check_keys(ctx, sc.file, m, (head,) + SIBLINGS.get(head, ()), f"{head} control")
        return _compile_control(ctx, sc, head, m, parent, path_id, line)
    _check_keys(ctx, sc.file, m, (head,) + COMMON_KEYS + SIBLINGS.get(head, ()), f"{head}")

    author = m.get("id")
    if author is not None:
        if not isinstance(author, str) or not author or author == END:
            raise ctx.err(sc.file, _key_line(m, "id", line), f"bad id {author!r}")
        full = sc.prefix + author
        if full in ctx.author_ids or full in ctx.nodes:
            raise ctx.err(sc.file, _key_line(m, "id", line), f"duplicate id {author!r}")
        ctx.author_ids.add(full)
        nid = full
    else:
        nid = _alloc(ctx, path_id)
    hline = _key_line(m, head, line)
    node = _add(ctx, ir.Node(
        id=nid, type="", parent=parent, src=_where(sc.file, hline), label=author or "",
        executor=_executor(ctx, sc.file, m.get("executor"), line),
        needs=_str_list(ctx, sc.file, m.get("needs"), "needs", line),
        budget=_budget(ctx, sc.file, m.get("budget"), line),
        bind=None,
    ))
    bind = m.get("bind")
    if bind is not None:
        if not isinstance(bind, str) or not bind:
            raise ctx.err(sc.file, _key_line(m, "bind", line), "bind must be a variable name")
        if bind == DATA_INPUTS:
            raise ctx.err(sc.file, _key_line(m, "bind", line),
                          f"bind {DATA_INPUTS!r} is reserved: $data.{DATA_INPUTS} holds the "
                          f"run's start inputs, which no node writes (docs 2.6)")
        node.bind = bind

    builder = _BUILDERS[head]
    builder(ctx, sc, node, m[head], m, hline)
    _force_target(ctx, sc.file, m, node, line)
    _effects_and_triggers(ctx, sc.file, m, node, line)

    on = m.get("on")
    if on is not None:
        on = _need_map(ctx, sc.file, on, "on", _key_line(m, "on", line))
        for outcome, handler in on.items():
            oline = _key_line(on, outcome, line)
            node.on[str(outcome)] = _compile_tree(ctx, sc, handler, nid, f"{nid}.on.{outcome}", oline)
    return nid


def _compile_control(ctx: _Ctx, sc: _Scope, op: str, m: Dict[str, Any], parent: Optional[str],
                     path_id: str, line: int) -> str:
    value = m[op]
    props: Dict[str, Any] = {"op": op, "target": None, "outcome": None, "value": None}
    if op in (ir.CTL_CONTINUE, ir.CTL_BREAK):
        if not sc.loops:
            raise ctx.err(sc.file, line, f"{op} outside any loop")
        if value is None:
            props["target"] = sc.loops[-1][0]
        else:
            for lid, label in reversed(sc.loops):
                if value in (lid, label):
                    props["target"] = lid
                    break
            else:
                known = ", ".join(label or lid for lid, label in sc.loops)
                raise ctx.err(sc.file, line, f"{op} {value!r}: not an enclosing loop (enclosing: {known})")
    elif op == ir.CTL_RAISE:
        if not isinstance(value, str) or not value:
            raise ctx.err(sc.file, line, "raise needs an outcome name")
        props["outcome"] = value
        if not any(value in outs for _, outs in sc.trys):
            if sc.in_proc:
                ctx.warnings.append(
                    f"{_where(sc.file, line)}: raise {value!r} is not caught inside "
                    f"procedure {sc.procs[-1] if sc.procs else '(this file)'}; it leaves the procedure"
                )
            else:
                raise ctx.err(sc.file, line, f"raise {value!r}: no enclosing try catches it")
    elif op == ir.CTL_RETURN:
        if not isinstance(value, str) or not value:
            raise ctx.err(sc.file, line, "return needs an outcome name")
        props["outcome"] = value
        props["value"] = _values(ctx, sc.file, m.get("value"), "return value", line)
        if sc.in_splice:
            # a block's return ends the call (or run) it was written in, not
            # the procedure it was spliced into (docs 2.2, hygiene)
            props["target"] = sc.scope_id or ir.RETURN_RUN
    nid = _alloc(ctx, path_id)
    _add(ctx, ir.Node(id=nid, type=ir.T_CONTROL, parent=parent, src=_where(sc.file, line), props=props))
    return nid


def _compile_splice(ctx: _Ctx, sc: _Scope, m: Dict[str, Any], parent: Optional[str],
                    path_id: str, line: int) -> str:
    """``{splice: <block param>}``: the block the call site passed, compiled in
    the call site's scope (its tasks, loops, catches, ``$params``, the target
    of its ``return``) and placed here. Node ids carry this procedure's
    prefix; nodes keep the call site's file:line."""
    _check_keys(ctx, sc.file, m, (SPLICE,), "splice")
    name = m[SPLICE]
    blocks = sc.blocks or {}
    if not isinstance(name, str) or name not in blocks:
        known = ", ".join(sorted(blocks)) or "(none)"
        where = f"procedure {sc.procs[-1]!r}" if sc.procs else "a workflow body"
        raise ctx.err(sc.file, line, f"splice {name!r}: not a block param of {where} "
                                     f"(block params: {known})")
    raw, site, bline = blocks[name]
    nid = _alloc(ctx, path_id)
    if site is None:
        # a default block is the procedure's own text: compiled right here,
        # in the procedure's scope (its loops, its $params) -- no call site
        node = _add(ctx, ir.Node(id=nid, type=ir.T_SEQ, parent=parent,
                                 src=_where(sc.file, line),
                                 props={"children": [], "pass_prev": False,
                                        "splice": {"block": name, "call": None}}))
        node.props["children"] = [_compile_tree(ctx, sc, raw, nid, f"{nid}.{name}", bline)]
        return nid
    node = _add(ctx, ir.Node(id=nid, type=ir.T_SEQ, parent=parent, src=_where(sc.file, line),
                             props={"children": [], "pass_prev": False,
                                    "splice": {"block": name, "call": sc.scope_id}}))
    # names resolve at the call site; a raise travels up from here (like any
    # outcome), so it is checked against the catches at the splice position,
    # which include the call site's
    inner = _Scope(**{**site.__dict__, "prefix": sc.prefix, "procs": sc.procs,
                      "trys": sc.trys, "in_splice": True})
    if sc.in_region:
        # spliced into a par branch: the branch is its own region, so the
        # call site's loops are out of reach (as for a continue written there)
        inner = _Scope(**{**inner.__dict__, "loops": (), "in_region": True})
    node.props["children"] = [_compile_tree(ctx, inner, raw, nid, f"{nid}.{name}", bline)]
    return nid


def _loop_scope(sc: _Scope, node: ir.Node) -> _Scope:
    return _Scope(**{**sc.__dict__, "loops": sc.loops + ((node.id, node.label),)})


def _sub(ctx: _Ctx, sc: _Scope, node: ir.Node, value: Any, key: str, line: int) -> str:
    return _compile_tree(ctx, sc, value, node.id, f"{node.id}.{key}", _line(value, line))


def _require(ctx: _Ctx, sc: _Scope, spec: Dict[str, Any], key: str, what: str, line: int) -> Any:
    if key not in spec or spec[key] is None:
        raise ctx.err(sc.file, _line(spec, line), f"{what} needs {key}")
    return spec[key]


def _b_seq(ctx, sc, node, value, m, line, pass_prev=False):
    node.type = ir.T_SEQ
    items = _need_list(ctx, sc.file, value, node.type if not pass_prev else "waterfall", line)
    node.props = {"children": [], "pass_prev": pass_prev}
    node.props["children"] = _compile_list(ctx, sc, items, node.id, line)


def _b_waterfall(ctx, sc, node, value, m, line):
    _b_seq(ctx, sc, node, value, m, line, pass_prev=True)


def _b_cond(ctx, sc, node, value, m, line):
    node.type = ir.T_COND
    items = _need_list(ctx, sc.file, value, "cond", line)
    lines = getattr(items, "item_lines", None) or []
    branches: List[Dict[str, Any]] = []
    node.props = {"branches": branches}
    for i, arm in enumerate(items):
        aline = lines[i] if i < len(lines) else line
        arm = _need_map(ctx, sc.file, arm, "cond arm", aline)
        if "else" in arm:
            _check_keys(ctx, sc.file, arm, ("else",), "cond else arm")
            if i != len(items) - 1:
                raise ctx.err(sc.file, aline, "cond: else must be the last arm")
            branches.append({"guard": None, "child": _sub(ctx, sc, node, arm["else"], "else", aline)})
            continue
        _check_keys(ctx, sc.file, arm, ("if", "then"), "cond arm")
        guard = _expr_text(ctx, sc.file, _require(ctx, sc, arm, "if", "cond arm", aline), "cond.if", aline)
        then = _require(ctx, sc, arm, "then", "cond arm", aline)
        branches.append({"guard": guard, "child": _sub(ctx, sc, node, then, f"if{i}", aline)})
    if not branches:
        raise ctx.err(sc.file, line, "cond needs at least one arm")


def _b_choose(ctx, sc, node, value, m, line):
    node.type = ir.T_CHOOSE
    spec = _need_map(ctx, sc.file, value, "choose", line)
    _check_keys(ctx, sc.file, spec, ("prompt", "chooser", "require_reason", "options"), "choose")
    chooser = spec.get("chooser", ir.CHOOSER_AGENT)
    if chooser not in ir.CHOOSERS:
        raise ctx.err(sc.file, line, f"choose.chooser must be one of {', '.join(ir.CHOOSERS)}")
    rr = spec.get("require_reason", False)
    if not isinstance(rr, bool):
        raise ctx.err(sc.file, line, "choose.require_reason must be true or false")
    prompt = spec.get("prompt") or ""
    if not isinstance(prompt, str):
        raise ctx.err(sc.file, line, "choose.prompt must be a string")
    prompt = _expand(ctx, prompt, sc.file, line)
    opts = _need_map(ctx, sc.file, _require(ctx, sc, spec, "options", "choose", line), "choose.options", line)
    options: List[Dict[str, Any]] = []
    node.props = {"prompt": prompt, "chooser": chooser, "require_reason": rr, "options": options}
    for name, opt in opts.items():
        oline = _key_line(opts, name, line)
        opt = _need_map(ctx, sc.file, opt, f"choose option {name!r}", oline)
        _check_keys(ctx, sc.file, opt, ("description", "then", "when", "interval"),
                    f"choose option {name!r}")
        guard = (_expr_text(ctx, sc.file, opt["when"], f"choose.options.{name}.when", oline)
                 if opt.get("when") is not None else None)
        interval = None
        if opt.get("interval") is not None:
            if chooser != ir.CHOOSER_AGENT:
                raise ctx.err(sc.file, oline, f"choose option {name!r}: interval paces the "
                                              f"agent's choice; it needs chooser: agent")
            interval = _number(ctx, sc.file, opt["interval"], f"choose option {name!r} interval",
                               oline)
            if interval <= 0:
                raise ctx.err(sc.file, oline, f"choose option {name!r}: interval must be "
                                              f"greater than 0")
        then = _require(ctx, sc, opt, "then", f"choose option {name!r}", oline)
        entry = {"name": str(name), "description": opt.get("description") or "",
                 "guard": guard, "child": _sub(ctx, sc, node, then, str(name), oline)}
        if interval is not None:
            entry["interval"] = interval
        options.append(entry)
    if not options:
        raise ctx.err(sc.file, line, "choose needs at least one option")


def _loop_props(**kw: Any) -> Dict[str, Any]:
    base = {"mode": None, "guard": None, "max": None, "child": None, "between": None,
            "every": None, "items": None, "as_name": None, "parallel": False, "carry": None}
    base.update(kw)
    return base


def _b_foreach(ctx, sc, node, value, m, line):
    node.type = ir.T_LOOP
    spec = _need_map(ctx, sc.file, value, "foreach", line)
    _check_keys(ctx, sc.file, spec, ("in", "as", "do", "par", "max"), "foreach")
    items = _expr_text(ctx, sc.file, _require(ctx, sc, spec, "in", "foreach", line), "foreach.in", line)
    as_name = spec.get("as", "item")
    if not isinstance(as_name, str) or not as_name:
        raise ctx.err(sc.file, line, "foreach.as must be a name")
    par = spec.get("par", False)
    if not isinstance(par, bool):
        raise ctx.err(sc.file, line, "foreach.par must be true or false")
    mx = _int(ctx, sc.file, spec.get("max", FOREACH_DEFAULT_MAX), "foreach.max", line, 1)
    node.props = _loop_props(mode=ir.LOOP_FOREACH, max=mx, items=items, as_name=as_name, parallel=par)
    body = _require(ctx, sc, spec, "do", "foreach", line)
    node.props["child"] = _sub(ctx, _loop_scope(sc, node), node, body, "do", line)


def _b_loop(ctx, sc, node, value, m, line):
    node.type = ir.T_LOOP
    spec = _need_map(ctx, sc.file, value, "loop", line)
    _check_keys(ctx, sc.file, spec, ("while", "until", "max", "do", "between", "every"), "loop")
    has = [k for k in ("while", "until") if k in spec]
    if len(has) != 1:
        raise ctx.err(sc.file, line, "loop needs exactly one of while / until")
    mode = has[0]
    guard = _expr_text(ctx, sc.file, spec[mode], f"loop.{mode}", line)
    if "max" not in spec:
        raise ctx.err(sc.file, line, "loop needs max (every loop is bounded)")
    mx = _int(ctx, sc.file, spec["max"], "loop.max", line, 1)
    every = _number(ctx, sc.file, spec["every"], "loop.every", line) if spec.get("every") is not None else None
    node.props = _loop_props(mode=mode, guard=guard, max=mx, every=every)
    lsc = _loop_scope(sc, node)
    node.props["child"] = _sub(ctx, lsc, node, _require(ctx, sc, spec, "do", "loop", line), "do", line)
    if spec.get("between") is not None:
        node.props["between"] = _sub(ctx, lsc, node, spec["between"], "between", line)


def _b_retry(ctx, sc, node, value, m, line):
    node.type = ir.T_LOOP
    spec = _need_map(ctx, sc.file, value, "retry", line)
    _check_keys(ctx, sc.file, spec, ("do", "max", "every", "until", "between", "carry"), "retry")
    if "max" not in spec:
        raise ctx.err(sc.file, line, "retry needs max (every loop is bounded)")
    mx = _int(ctx, sc.file, spec["max"], "retry.max", line, 1)
    guard = _expr_text(ctx, sc.file, spec["until"], "retry.until", line) if spec.get("until") is not None else None
    every = _number(ctx, sc.file, spec["every"], "retry.every", line) if spec.get("every") is not None else None
    carry = spec.get("carry", "summary")
    if carry not in ("summary", "resume"):
        raise ctx.err(sc.file, line, "retry.carry must be summary or resume")
    node.props = _loop_props(mode=ir.LOOP_RETRY, guard=guard, max=mx, every=every, carry=carry)
    lsc = _loop_scope(sc, node)
    node.props["child"] = _sub(ctx, lsc, node, _require(ctx, sc, spec, "do", "retry", line), "do", line)
    if spec.get("between") is not None:
        node.props["between"] = _sub(ctx, lsc, node, spec["between"], "between", line)


def _settle(ctx, sc, value, default, what, line) -> str:
    settle = default if value is None else value
    if not isinstance(settle, str):
        raise ctx.err(sc.file, line, f"{what}.settle must be a string")
    base, _, k = settle.partition(":")
    if base not in ir.SETTLE_MODES or (base == ir.SETTLE_QUORUM) != bool(k):
        raise ctx.err(sc.file, line, f"{what}.settle {settle!r}: use all, allSettled, race, any or quorum:K")
    if k and (not k.isdigit() or int(k) < 1):
        raise ctx.err(sc.file, line, f"{what}.settle {settle!r}: K must be a positive integer")
    return settle


def _b_par(ctx, sc, node, value, m, line):
    node.type = ir.T_PAR
    spec = _need_map(ctx, sc.file, value, "par", line)
    _check_keys(ctx, sc.file, spec, ("branches", "settle"), "par")
    branches = _need_map(ctx, sc.file, _require(ctx, sc, spec, "branches", "par", line), "par.branches", line)
    if not branches:
        raise ctx.err(sc.file, line, "par needs at least one branch")
    node.props = {"branches": {}, "settle": _settle(ctx, sc, spec.get("settle"), ir.SETTLE_ALL, "par", line)}
    # A branch is its own region: loops/trys outside do not reach into it.
    bsc = _Scope(**{**sc.__dict__, "loops": (), "in_region": True})
    for name, tree in branches.items():
        bline = _key_line(branches, name, line)
        node.props["branches"][str(name)] = _compile_tree(ctx, bsc, tree, node.id, f"{node.id}.{name}", bline)


def _b_timeout(ctx, sc, node, value, m, line):
    node.type = ir.T_TIMEOUT
    spec = _need_map(ctx, sc.file, value, "timeout", line)
    _check_keys(ctx, sc.file, spec, ("do", "seconds", "else"), "timeout")
    secs = _number(ctx, sc.file, _require(ctx, sc, spec, "seconds", "timeout", line), "timeout.seconds", line)
    node.props = {"child": None, "seconds": secs, "else_child": None}
    node.props["child"] = _sub(ctx, sc, node, _require(ctx, sc, spec, "do", "timeout", line), "do", line)
    if spec.get("else") is not None:
        node.props["else_child"] = _sub(ctx, sc, node, spec["else"], "else", line)


def _b_try(ctx, sc, node, value, m, line):
    node.type = ir.T_TRY
    catch = m.get("catch")
    if catch is not None:
        catch = _need_map(ctx, sc.file, catch, "catch", _key_line(m, "catch", line))
    if not catch and m.get("finally") is None:
        raise ctx.err(sc.file, line, "try needs catch or finally")
    outs = tuple(str(k) for k in (catch or {}))
    node.props = {"child": None, "catch": {}, "finally_child": None}
    tsc = _Scope(**{**sc.__dict__, "trys": sc.trys + ((node.id, outs),)})
    node.props["child"] = _sub(ctx, tsc, node, value, "try", line)
    for outcome, handler in (catch or {}).items():
        cline = _key_line(catch, outcome, line)
        node.props["catch"][str(outcome)] = _compile_tree(
            ctx, sc, handler, node.id, f"{node.id}.catch.{outcome}", cline)
    if m.get("finally") is not None:
        node.props["finally_child"] = _sub(ctx, sc, node, m["finally"], "finally", _key_line(m, "finally", line))


def _inline(ctx: _Ctx, sc: _Scope, scope_node: ir.Node, pname: str, with_raw: Any,
            line: int, key: str = "body", check_required: bool = True) -> None:
    if pname in sc.procs:
        cycle = " -> ".join(sc.procs[sc.procs.index(pname):] + (pname,))
        raise ctx.err(sc.file, line, f"recursive call: {cycle}")
    proc = _procedure(ctx, pname, sc.file, line)
    block_names = {pn for pn, decl in proc.params.items() if decl.get("type") == PARAM_BLOCK}
    raw_with = _need_map(ctx, sc.file, with_raw, "with", line) if with_raw is not None else {}
    with_ = _values(ctx, sc.file, {k: v for k, v in raw_with.items() if k not in block_names},
                    "with", line)
    for k in with_:
        if proc.params and k not in proc.params:
            raise ctx.err(sc.file, line, f"call {pname!r}: unknown parameter {k!r} "
                          f"(declared: {', '.join(proc.params)})")
    blocks: Dict[str, Tuple[Any, Optional[_Scope], int]] = {}
    callee_scope = _Scope(file=proc.file, tasks=proc.tasks, prefix=scope_node.id + "/",
                          loops=(), trys=sc.trys, procs=sc.procs + (pname,), in_proc=True,
                          scope_id=scope_node.id)
    for pn in block_names:
        decl = proc.params[pn]
        if pn in raw_with:
            if raw_with[pn] is None:
                raise ctx.err(sc.file, line, f"call {pname!r}: block parameter {pn!r} needs a "
                                             f"tree (a task name, a list or a mapping)")
            blocks[pn] = (raw_with[pn], sc, _key_line(raw_with, pn, line))
        elif decl.get("default") is not None:
            # a default block is the procedure's own text: compiled where it
            # is spliced, in the procedure's own scope (no call site)
            blocks[pn] = (decl["default"], None, proc.line)
        elif not check_required:
            raise ctx.err(sc.file, line, f"procedure {pname!r}: block parameter {pn!r} has no "
                                         f"default; a group member has no call site to pass it")
        else:
            raise ctx.err(sc.file, line, f"call {pname!r}: missing block parameter {pn!r}")
    for pn, decl in proc.params.items():
        if pn in block_names:
            continue
        if check_required and decl.get("required") and pn not in with_ and "default" not in decl:
            raise ctx.err(sc.file, line, f"call {pname!r}: missing required parameter {pn!r}")
    scope_node.type = ir.T_SCOPE
    scope_node.props = {"proc": pname, "child": None, "with_": with_, "returns": proc.returns,
                        "params": {pn: d for pn, d in proc.params.items()
                                   if pn not in block_names}}
    inner = _Scope(**{**callee_scope.__dict__, "blocks": blocks})
    scope_node.props["child"] = _compile_tree(ctx, inner, proc.body, scope_node.id,
                                              f"{scope_node.id}.{key}", proc.line)


#: The param a procedure must declare to take ``cases`` (its choice names),
#: and the field its ``ok`` return must carry (the choice taken).
CASES_PARAM = "choices"
CASES_FIELD = "decision"


def _b_call(ctx, sc, node, value, m, line):
    if not isinstance(value, str) or not value:
        raise ctx.err(sc.file, line, "call needs a procedure name")
    if m.get("cases") is not None:
        _call_cases(ctx, sc, node, value, m, line)
        return
    _inline(ctx, sc, node, value, m.get("with"), line)


def _call_cases(ctx: _Ctx, sc: _Scope, node: ir.Node, pname: str, m: Dict[str, Any],
                line: int) -> None:
    """``call: P, cases: {<choice>: <tree>}`` (docs 2.10, cases): the keys are
    P's ``choices`` and each tree runs when P returns ok with that
    ``decision``. Compiles to ``seq[call (choices = the keys), cond on the
    decision]``; the trees are the call site's own text, so they compile in
    its scope. Only a procedure that declares a ``choices`` param and an ok
    return with ``decision`` takes cases."""
    cline = _key_line(m, "cases", line)
    cases = _need_map(ctx, sc.file, m["cases"], "cases", cline)
    proc = _procedure(ctx, pname, sc.file, line)
    if CASES_PARAM not in proc.params \
            or CASES_FIELD not in ((proc.returns or {}).get(ir.OUTCOME_OK) or {}):
        raise ctx.err(sc.file, cline, f"call {pname!r}: cases needs a procedure that declares "
                                      f"a {CASES_PARAM!r} param and an ok return with "
                                      f"{CASES_FIELD!r}")
    with_raw = _need_map(ctx, sc.file, m.get("with"), "with", line) \
        if m.get("with") is not None else {}
    if CASES_PARAM in with_raw:
        raise ctx.err(sc.file, cline, f"call {pname!r}: with.{CASES_PARAM} and cases together "
                                      f"-- the cases keys are the choices")
    if not cases:
        raise ctx.err(sc.file, cline, "cases needs at least one choice")
    # outcomes P can end with besides ok: an answer named like one would be
    # read as that outcome (ask-user's reframe), so they are not choices
    reserved = {ir.OUTCOME_FAIL} | {o for o in (proc.returns or {}) if o != ir.OUTCOME_OK}
    names: List[str] = []
    for key, tree in cases.items():
        kline = _key_line(cases, key, cline)
        if not isinstance(key, str) or not _OUTCOME_RE.match(key):
            raise ctx.err(sc.file, kline, f"cases: {key!r} is not a choice name")
        if key.lower() in {r.lower() for r in reserved}:
            raise ctx.err(sc.file, kline, f"cases: {key!r} is an outcome of {pname!r} "
                                          f"({', '.join(sorted(reserved))}), not a choice -- "
                                          f"handle it with on or catch")
        if key.lower() in _question.RESERVED_NAMES:
            # the keys become a question's option names, and no brief may
            # name an option like this: the ask could never be posed
            # (claunch-gpccp: end-decision's approve)
            raise ctx.err(sc.file, kline, f"cases: {key!r} cannot be an option name "
                                          f"(reserved: {', '.join(_question.RESERVED_NAMES)})")
        if any(key.lower() == n.lower() for n in names):
            raise ctx.err(sc.file, kline, f"cases: duplicate choice {key!r}")
        if tree is None:
            raise ctx.err(sc.file, kline, f"cases.{key}: every choice needs a tree "
                                          f"(write [] for a choice that does nothing)")
        names.append(key)
    # the node becomes the wrapper: its id, on and the caller's view stay here
    node.type = ir.T_SEQ
    node.props = {"children": [], "pass_prev": False, "cases": names}
    ask = _add(ctx, ir.Node(id=_alloc(ctx, f"{node.id}.call"), type="", parent=node.id,
                            src=node.src, bind=node.bind))
    node.bind = None
    _inline(ctx, sc, ask, pname, {**with_raw, CASES_PARAM: names}, line)
    pick = _add(ctx, ir.Node(id=_alloc(ctx, f"{node.id}.cases"), type=ir.T_COND,
                             parent=node.id, src=_where(sc.file, cline)))
    # an unlabelled node's result is kept under its id (runtime.node_key)
    ref = f"$result['{ask.id}'].value.{CASES_FIELD}"
    branches = []
    for i, key in enumerate(names):
        kline = _key_line(cases, key, cline)
        branches.append({"guard": f"{ref} == '{key}'",
                         "child": _sub(ctx, sc, pick, cases[key], f"case.{key}", kline)})
    pick.props = {"branches": branches}
    node.props["children"] = [ask.id, pick.id]


CHECK_ITEM_KEYS = ("id", "describe", "probe")


def _wait_checks(ctx: _Ctx, file: str, value: Any, line: int) -> List[Dict[str, Any]]:
    """``wait.checks``: ``[{id, describe, probe}]`` -- every probe must exit 0
    in the same poll (docs 2.3, wait checks)."""
    items = _need_list(ctx, file, value, "wait.checks", line)
    if not items:
        raise ctx.err(file, line, "wait.checks needs at least one item")
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, raw in enumerate(items):
        iline = _line(raw, line)
        item = _need_map(ctx, file, raw, f"wait.checks[{i}]", iline)
        _check_keys(ctx, file, item, CHECK_ITEM_KEYS, f"wait.checks[{i}]")
        cid = item.get("id")
        if not isinstance(cid, str) or not _OUTCOME_RE.match(cid):
            raise ctx.err(file, iline, f"wait.checks[{i}].id must be a name")
        if cid in seen:
            raise ctx.err(file, iline, f"wait.checks: duplicate id {cid!r}")
        seen.add(cid)
        if item.get("probe") is None:
            raise ctx.err(file, iline, f"wait.checks[{i}] needs a probe")
        probe = _command(ctx, file, iline, item["probe"], f"wait.checks.{cid}.probe")
        describe = item.get("describe")
        if describe is not None and not isinstance(describe, str):
            raise ctx.err(file, iline, f"wait.checks.{cid}.describe must be a string")
        out.append({"id": cid, "describe": describe or "", "probe": probe})
    return out


def _b_wait(ctx, sc, node, value, m, line):
    node.type = ir.T_WAIT
    spec = _need_map(ctx, sc.file, value, "wait", line)
    _check_keys(ctx, sc.file, spec, ("probe", "poll", "seconds", "describe", "undecided_max",
                                     "until", "consume", "checks"), "wait")
    until = spec.get("until")
    if spec.get("probe") is None and spec.get("seconds") is None and until is None \
            and spec.get("checks") is None:
        raise ctx.err(sc.file, line, "wait needs probe, checks, until or seconds")
    if spec.get("checks") is not None and (spec.get("probe") is not None or until is not None):
        raise ctx.err(sc.file, _key_line(spec, "checks", line),
                      "wait.checks cannot be used with probe or until (checks is a list of "
                      "probes)")
    if until is not None:
        if spec.get("probe") is not None:
            raise ctx.err(sc.file, line, "wait.until cannot be used with probe (until is "
                                         "decided by the engine, probe by a command)")
        for k in ("poll", "undecided_max"):
            if spec.get(k) is not None:
                raise ctx.err(sc.file, _key_line(spec, k, line), f"wait.{k} applies to a probe")
    elif spec.get("consume") is not None:
        raise ctx.err(sc.file, _key_line(spec, "consume", line), "wait.consume applies to until")
    probe = spec.get("probe")
    checks = None
    if probe is not None:
        probe = _command(ctx, sc.file, _key_line(spec, "probe", line), probe, "wait.probe")
    elif spec.get("checks") is not None:
        checks = _wait_checks(ctx, sc.file, spec["checks"], _key_line(spec, "checks", line))
    elif spec.get("undecided_max") is not None:
        raise ctx.err(sc.file, line, "wait.undecided_max applies to a probe")
    probing = probe is not None or checks is not None
    undecided = (_int(ctx, sc.file, spec.get("undecided_max", WAIT_UNDECIDED_MAX),
                      "wait.undecided_max", line, 1) if probing else None)
    node.props = {
        "probe": probe,
        "undecided_max": undecided,
        "poll": _number(ctx, sc.file, spec["poll"], "wait.poll", line) if spec.get("poll") is not None else None,
        "seconds": _number(ctx, sc.file, spec["seconds"], "wait.seconds", line)
        if spec.get("seconds") is not None else None,
        "describe": spec.get("describe"),
    }
    if checks is not None:
        node.props["checks"] = checks
    if until is not None:
        uline = _key_line(spec, "until", line)
        node.props["until"] = _expr_text(ctx, sc.file, until, "wait.until", uline)
        cline = _key_line(spec, "consume", line)
        raw = spec.get("consume")
        if raw is None:
            raw = []
        elif isinstance(raw, (str, dict)):
            raw = [raw]
        raw = _need_list(ctx, sc.file, raw, "wait.consume", cline)
        node.props["consume"] = [_consume_entry(ctx, sc.file, a, "wait.consume", cline)
                                 for a in raw]


def _consume_entry(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> Any:
    """A consume target: a log address (the whole log), or ``{from: ADDR,
    name: NAME}`` -- only the records named NAME (doc 2.7: a cursor per name,
    so consuming one signal leaves the others fresh). IR: the address string
    or ``{addr, name}``."""
    if isinstance(value, dict):
        _check_keys(ctx, file, value, ("from", "name"), what)
        if not isinstance(value.get("name"), str) or not value.get("name"):
            raise ctx.err(file, line, f"{what}: {{from, name}} needs a name string")
        return {"addr": _address(ctx, file, value.get("from"), what, line),
                "name": str(value["name"])}
    return _address(ctx, file, value, what, line)


def _b_group(ctx, sc, node, value, m, line):
    node.type = ir.T_GROUP
    spec = _need_map(ctx, sc.file, value, "group", line)
    _check_keys(ctx, sc.file, spec, ("name", "run", "limit", "settle", "do",
                                      "member_executor", "allow"), "group")
    run = _require(ctx, sc, spec, "run", "group", line)
    if not isinstance(run, str):
        raise ctx.err(sc.file, line, "group.run must be a procedure name")
    name = spec.get("name") or node.label or node.id
    node.props = {
        "name": str(name), "child": None, "member": None,
        "limit": _int(ctx, sc.file, spec.get("limit", 3), "group.limit", line, 1),
        "settle": _settle(ctx, sc, spec.get("settle"), ir.SETTLE_ALL_SETTLED, "group", line),
        # ``allow``: a guard read at each ``flow_fork``; false refuses the
        # member (the group still runs its do-part and joins, with fewer or
        # no members). A run input switches a group on and off this way.
        "allow": (_expr_text(ctx, sc.file, spec["allow"], "group.allow", line)
                  if spec.get("allow") is not None else None),
    }
    node.props["child"] = _sub(ctx, sc, node, _require(ctx, sc, spec, "do", "group", line), "do", line)
    mid = _alloc(ctx, f"{node.id}.member")
    member = _add(ctx, ir.Node(id=mid, type=ir.T_SCOPE, parent=node.id, src=_where(sc.file, line)))
    # Members are separate regions; the group's loops do not reach into them.
    # Members get their ``with`` from ``flow_fork`` at run time.
    _inline(ctx, _Scope(**{**sc.__dict__, "loops": ()}), member, run, None, line,
            check_required=False)
    node.props["member"] = mid
    # ``member_executor``: where each member runs, as a node's ``executor``
    # does for that node -- the member scope is the delegation point, so every
    # ``flow_fork`` opens a delegation of its own (``fork``: a quick-fork of
    # the driver per member, claunch-dm09i.1). The group itself stays inline.
    if spec.get("member_executor") is not None:
        member.executor = _executor(ctx, sc.file, spec.get("member_executor"), line)


def _ask_brief(ctx: _Ctx, file: str, name: str, ask: Dict[str, Any], aline: int) -> Dict[str, Any]:
    """An ask whose question and options come from an earlier result
    (docs/cflow-v2.md 2.10): ``{brief, format, choices, prompt, from,
    from_param, ...}``. ``brief`` is stored as an expression; ``format``,
    ``choices`` and ``from`` may each be one ``${params.<p>}`` reference,
    stored as ``"$params.<p>"`` and resolved when the ask opens."""
    from ..cflow import model as v1

    what = f"task {name!r}"
    if "brief" not in ask or "format" not in ask:
        raise ctx.err(file, aline, f"{what}: ask.brief and ask.format go together "
                      f"(ask-format-without-brief)")
    for key in ("options", "on_decline"):
        if key in ask:
            raise ctx.err(file, aline, f"{what}: ask.{key} cannot go with ask.brief -- the "
                          f"options come from the brief (ask-brief-with-options)")
    brief = ask.get("brief")
    m = _WHOLE_REF.match(brief.strip()) if isinstance(brief, str) else None
    if not m:
        raise ctx.err(file, aline, f"{what}: ask.brief must be one value reference "
                      f"${{...}} and nothing else (ask-brief-ref)")
    inner = m.group(1).strip()
    brief_expr = _expr_text(ctx, file, inner if inner.startswith("$") else "$" + inner,
                            f"{what} ask.brief", aline)
    prompt = ask.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ctx.err(file, aline, f"{what}: ask.prompt must be a string")

    def param_or(value: Any, key: str) -> Optional[str]:
        if isinstance(value, str):
            pm = _PARAM_REF.match(value.strip())
            if pm:
                return f"$params.{pm.group(1)}"
        return None

    fmt = ask.get("format")
    fmt_v = param_or(fmt, "format")
    if fmt_v is None:
        if not isinstance(fmt, str) or not _NAME_RE.match(fmt):
            raise ctx.err(file, aline, f"{what}: ask.format must be a question type name or "
                          f"${{params.<name>}}")
        fmt_v = fmt
    choices = ask.get("choices")
    choices_v: Any = None
    if choices is not None:
        choices_v = param_or(choices, "choices")
        if choices_v is None:
            if not isinstance(choices, list) or not choices:
                raise ctx.err(file, aline, f"{what}: ask.choices must be a list of option names "
                              f"or ${{params.<name>}}")
            folded: Set[str] = set()
            for c in choices:
                if not isinstance(c, str) or not _OUTCOME_RE.match(c) or \
                        c.lower() in _question.RESERVED_NAMES:
                    raise ctx.err(file, aline, f"{what}: bad ask.choices name {c!r}")
                if c.lower() in folded:
                    raise ctx.err(file, aline, f"{what}: ask.choices repeats {c!r}")
                folded.add(c.lower())
            choices_v = list(choices)
    req = ask.get("require_reason", False)
    req_v: Any = param_or(req, "require_reason")
    if req_v is None:
        if not isinstance(req, bool):
            raise ctx.err(file, aline, f"{what}: ask.require_reason must be true, false or "
                          f"${{params.<name>}}")
        req_v = req
    raw = _plain(ask)
    from_param = param_or(raw.get("from"), "from")
    if from_param is not None:
        raw.pop("from")
    for key in ("brief", "format", "choices", "prompt", "require_reason", "urgent"):
        raw.pop(key, None)
    try:
        delegate = v1._parse_delegate(raw, f"{what}: ask")
    except v1.WorkflowError as exc:
        raise ctx.err(file, aline, str(exc)) from None
    default = delegate.default_option
    if default is not None:
        if not isinstance(choices_v, list):
            raise ctx.err(file, aline, f"{what}: ask 'otherwise: self:{default}' needs literal "
                          f"ask.choices naming that option (ask-choices-mismatch)")
        if default.lower() not in [c.lower() for c in choices_v]:
            raise ctx.err(file, aline, f"{what}: ask 'otherwise: self:{default}' names no "
                          f"choice ({', '.join(choices_v)}) (ask-choices-mismatch)")
    return {
        "prompt": prompt or "",
        "brief": brief_expr,
        "format": fmt_v,
        "choices": choices_v,
        "require_reason": req_v,
        "from": [{"role": c.role, "scope": c.scope, "connect": c.connect}
                 for c in delegate.candidates],
        "from_param": from_param,
        "otherwise": delegate.otherwise,
        "default_option": default,
        "timeout": delegate.timeout,
        "on_decline": None,
        "on_unroutable": _ask_unroutable(ctx, file, name, ask, aline),
        "urgent": _ask_urgent(ctx, file, name, ask, aline),
        "options": None,
    }


_BUILDERS: Dict[str, Callable[..., None]] = {
    "seq": _b_seq, "waterfall": _b_waterfall, "cond": _b_cond, "choose": _b_choose,
    "foreach": _b_foreach, "loop": _b_loop, "par": _b_par, "retry": _b_retry,
    "timeout": _b_timeout, "try": _b_try, "call": _b_call, "wait": _b_wait,
    "group": _b_group,
}


# ---------------------------------------------------------------------------
# collections: declarations, do: effects, triggers (docs/cflow-v2.md 2.7, 2.8)


def _collection_decls(ctx: _Ctx, file: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    raw = doc.get("collections")
    types = {name: entry[0] for name, entry in ctx.collection_types.items()}
    out: Dict[str, Any] = {}
    own = set()
    if raw is not None:
        own = {str(n) for n in _need_map(ctx, file, raw, "collections",
                                          _key_line(doc, "collections", 1))}
    # an included file's collections come along (its procedures use them);
    # this file's own declaration of a name replaces the included one
    for name, (decl, ifile, iline) in ctx.included_collections.items():
        if name in own:
            continue
        try:
            out[name] = _collections.normalize(name, _plain(decl), types)
        except _collections.CollectionError as exc:
            raise ctx.err(ifile, iline, str(exc)) from None
    if raw is None:
        return out
    cline = _key_line(doc, "collections", 1)
    raw = _need_map(ctx, file, raw, "collections", cline)
    for name, decl in raw.items():
        dline = _key_line(raw, name, cline)
        decl = _need_map(ctx, file, decl, f"collection {name!r}", dline)
        try:
            out[str(name)] = _collections.normalize(str(name), _plain(decl), types)
        except _collections.CollectionError as exc:
            raise ctx.err(file, dline, str(exc)) from None
    return out


def _address(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> str:
    """A collection address, syntax-checked (which collection exists and
    which targets are allowed where is ``check``'s job)."""
    try:
        _collections.parse_address(value)
    except _collections.CollectionError as exc:
        raise ctx.err(file, line, f"{what}: {exc}") from None
    return str(value).strip()


def _expr_map(ctx: _Ctx, file: str, value: Any, what: str, line: int) -> Dict[str, str]:
    mapping = _need_map(ctx, file, value, what, line)
    out: Dict[str, str] = {}
    for fname, fval in mapping.items():
        if not isinstance(fname, str) or not fname:
            raise ctx.err(file, line, f"{what}: bad field name {fname!r}")
        out[fname] = _expr_text(ctx, file, fval, f"{what}.{fname}", _key_line(mapping, fname, line))
    return out


def _moment(ctx: _Ctx, file: str, entry: Dict[str, Any], default: str, what: str, line: int) -> str:
    at = entry.get("at", default)
    if at not in MOMENTS:
        raise ctx.err(file, _key_line(entry, "at", line),
                      f"{what}: at must be {' or '.join(MOMENTS)}, got {at!r}")
    return at


def _bool_key(ctx: _Ctx, file: str, entry: Dict[str, Any], what: str, line: int) -> None:
    if True in entry or False in entry:
        raise ctx.err(file, line, f"{what} has a boolean key -- a YAML 1.1 reader turns "
                                  f"'on:'/'off:' into booleans; spell the moment 'at:'")


def _do_list(ctx: _Ctx, file: str, value: Any, line: int) -> List[Dict[str, Any]]:
    """``do:`` -> ``[{op, addr, at, when, ...}]``.

    ``push``: ``item`` ({field: expr}); ``set``: ``key`` (expr), ``fields``
    ({field: expr}); ``remove``: ``key``; ``consume``: no more. ``at`` is
    ``enter`` or ``leave`` (default), ``when`` an expression or None.
    """
    entries = _need_list(ctx, file, value, "do", line)
    lines = getattr(entries, "item_lines", None) or []
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(entries):
        eline = lines[i] if i < len(lines) else line
        entry = _need_map(ctx, file, entry, "a do: entry", eline)
        _bool_key(ctx, file, entry, "a do: entry", eline)
        ops = [k for k in entry if k in DO_OPS]
        if len(ops) != 1:
            raise ctx.err(file, eline, f"a do: entry needs exactly one of {', '.join(DO_OPS)}")
        op = ops[0]
        _check_keys(ctx, file, entry, (op, "at", "when"), f"do: {op}")
        body = entry[op]
        what = f"do: {op}"
        rec: Dict[str, Any] = {"op": op}
        if op == "consume":
            target = _consume_entry(ctx, file, body, what, eline)
            if isinstance(target, dict):
                rec.update(target)
            else:
                rec["addr"] = target
        else:
            body = _need_map(ctx, file, body, what, eline)
            addr_key = "to" if op == "push" else "in"
            allowed = {"push": ("to", "item"), "set": ("in", "key", "fields"),
                       "remove": ("in", "key")}[op]
            _check_keys(ctx, file, body, allowed, what)
            for k in allowed:
                if body.get(k) is None:
                    raise ctx.err(file, eline, f"{what} needs {k}")
            rec["addr"] = _address(ctx, file, body[addr_key], what, eline)
            if op == "push":
                rec["item"] = _expr_map(ctx, file, body["item"], f"{what}.item", eline)
            else:
                rec["key"] = _expr_text(ctx, file, body["key"], f"{what}.key", eline)
                if op == "set":
                    rec["fields"] = _expr_map(ctx, file, body["fields"], f"{what}.fields", eline)
                    if not rec["fields"]:
                        raise ctx.err(file, eline, f"{what}.fields must not be empty")
        rec["at"] = _moment(ctx, file, entry, MOMENT_LEAVE, what, eline)
        rec["when"] = (_expr_text(ctx, file, entry["when"], f"{what}.when", eline)
                       if entry.get("when") is not None else None)
        out.append(rec)
    return out


def _triggers(ctx: _Ctx, file: str, value: Any, line: int) -> List[Dict[str, Any]]:
    """``triggers:`` -> ``[{do, at}]``, plus ``push: {to, each, item}`` when
    ``do`` is ``push`` (and ``when``, an expression the engine evaluates as it
    records the trigger). A bare name means ``{do: <name>, at: enter}`` (v1)."""
    entries = _need_list(ctx, file, value, "triggers", line)
    lines = getattr(entries, "item_lines", None) or []
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for i, entry in enumerate(entries):
        eline = lines[i] if i < len(lines) else line
        if isinstance(entry, str):
            entry = {"do": entry}
        entry = _need_map(ctx, file, entry, "a triggers entry", eline)
        _bool_key(ctx, file, entry, "a triggers entry", eline)
        if "push" in entry:
            _check_keys(ctx, file, entry, ("push", "at", "when"), "a push trigger")
            body = _need_map(ctx, file, entry["push"], "trigger push", eline)
            _check_keys(ctx, file, body, ("to", "each", "item"), "trigger push")
            for k in ("to", "item"):
                if body.get(k) is None:
                    raise ctx.err(file, eline, f"trigger push needs {k}")
            rec: Dict[str, Any] = {
                "do": TRIGGER_PUSH,
                "at": _moment(ctx, file, entry, MOMENT_ENTER, "trigger push", eline),
                "push": {
                    "to": _address(ctx, file, body["to"], "trigger push.to", eline),
                    "each": (_expr_text(ctx, file, body["each"], "trigger push.each", eline)
                             if body.get("each") is not None else None),
                    "item": _expr_map(ctx, file, body["item"], "trigger push.item", eline),
                },
            }
            if entry.get("when") is not None:
                rec["when"] = _expr_text(ctx, file, entry["when"], "trigger when", eline)
            out.append(rec)
            continue
        _check_keys(ctx, file, entry, ("do", "at"), "a triggers entry")
        do = entry.get("do")
        if do not in TRIGGER_ACTIONS:
            raise ctx.err(file, eline, f"unknown trigger {do!r} (use {', '.join(TRIGGER_ACTIONS)}, "
                                       f"or a push: {{to, each, item}} entry)")
        at = _moment(ctx, file, entry, MOMENT_ENTER, f"trigger {do}", eline)
        if (do, at) in seen:
            raise ctx.err(file, eline, f"trigger {do!r} is declared twice for at: {at}")
        seen.add((do, at))
        out.append({"do": do, "at": at})
    return out


def _effects_and_triggers(ctx: _Ctx, file: str, m: Dict[str, Any], node: ir.Node, line: int) -> None:
    if m.get("do") is not None:
        node.props["do"] = _do_list(ctx, file, m["do"], _key_line(m, "do", line))
    if m.get("triggers") is not None:
        node.props["triggers"] = _triggers(ctx, file, m["triggers"], _key_line(m, "triggers", line))


# ---------------------------------------------------------------------------
# spec


def _spec(ctx: _Ctx, file: str, value: Any, line: int) -> Dict[str, Any]:
    if value is None:
        return {}
    spec = _need_map(ctx, file, value, "spec", line)
    _check_keys(ctx, file, spec, SPEC_KEYS, "spec")
    inv = spec.get("invariants")
    if inv is not None:
        inv = _need_map(ctx, file, inv, "spec.invariants", line)
        for name, body in inv.items():
            bline = _key_line(inv, name, line)
            body = _need_map(ctx, file, body, f"invariant {name!r}", bline)
            _check_keys(ctx, file, body, ("never_effect", "target"), f"invariant {name!r}")
            if not isinstance(body.get("never_effect"), str):
                raise ctx.err(file, bline, f"invariant {name!r} needs never_effect: <effect>")
    paths = spec.get("paths")
    if paths is not None:
        paths = _need_map(ctx, file, paths, "spec.paths", line)
        for name, body in paths.items():
            bline = _key_line(paths, name, line)
            body = _need_map(ctx, file, body, f"path {name!r}", bline)
            _check_keys(ctx, file, body, ("to", "must_pass"), f"path {name!r}")
            if not isinstance(body.get("to"), str):
                raise ctx.err(file, bline, f"path {name!r} needs to: <label|end>")
            if not _str_list(ctx, file, body.get("must_pass"), f"path {name!r} must_pass", bline):
                raise ctx.err(file, bline, f"path {name!r} needs a non-empty must_pass")
    ens = spec.get("ensures")
    if ens is not None:
        ens = _need_map(ctx, file, ens, "spec.ensures", line)
        for key, text in ens.items():
            kline = _key_line(ens, key, line)
            if "." not in str(key):
                raise ctx.err(file, kline, f"ensures key {key!r} must be <node>.<outcome>")
            _expr_text(ctx, file, text, f"ensures {key}", kline)
    mons = spec.get("monitors")
    if mons is not None:
        mons = _need_map(ctx, file, mons, "spec.monitors", line)
        for name, body in mons.items():
            bline = _key_line(mons, name, line)
            body = _need_map(ctx, file, body, f"monitor {name!r}", bline)
            _check_keys(ctx, file, body, MONITOR_KEYS, f"monitor {name!r}")
            if ("always" in body) == ("within" in body):
                raise ctx.err(file, bline, f"monitor {name!r} needs exactly one of always / within")
            if body.get("probe") is not None:
                probes = _need_map(ctx, file, body["probe"], f"monitor {name!r} probe", bline)
                for pkey, cmd in probes.items():
                    _command(ctx, file, _key_line(probes, pkey, bline), cmd,
                             f"monitor {name!r} probe {pkey}")
            if "always" in body:
                _expr_text(ctx, file, body["always"], f"monitor {name!r} always", bline)
            else:
                for k in ("when", "expect"):
                    if not isinstance(body.get(k), dict):
                        raise ctx.err(file, bline, f"monitor {name!r} with within needs {k}: {{event: ...}}")
                _number(ctx, file, body["within"], f"monitor {name!r} within", bline)
            ov = body.get("on_violation", "record")
            if ov not in ON_VIOLATION:
                raise ctx.err(file, bline, f"monitor {name!r} on_violation must be one of {', '.join(ON_VIOLATION)}")
    return _plain(spec)


# ---------------------------------------------------------------------------
# entry points


def _default_resolver(base: Optional[Path]) -> ResolveInclude:
    """Names through the v2 layers of the process cwd (``layers.resolver``).
    ``base`` is not consulted: a file's neighbours are not a layer (C3)."""
    from . import layers

    return layers.resolver(None)


def _recur(ctx: _Ctx, file: str, doc: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    """``recur: true`` or ``recur: {auto: true, min_gap: S}`` ->
    ``{"auto": bool, "min_gap": float}``; ``false``/absent -> ``None``. Only a
    workflow recurs. ``min_gap`` (default 60) is the least number of seconds
    between a round's end and the auto start of the next."""
    raw = doc.get("recur")
    line = _key_line(doc, "recur", 1)
    if raw is None or raw is False:
        return None
    if kind != ir.KIND_WORKFLOW:
        raise ctx.err(file, line, "only a workflow may declare recur (a procedure has no rounds)")
    if raw is True:
        return {"auto": False, "min_gap": ir.RECUR_MIN_GAP_DEFAULT}
    if isinstance(raw, dict):
        _check_keys(ctx, file, raw, ("auto", "min_gap"), "recur")
        auto = raw.get("auto", False)
        if not isinstance(auto, bool):
            raise ctx.err(file, line, "recur.auto must be true or false")
        gap = ir.RECUR_MIN_GAP_DEFAULT
        if raw.get("min_gap") is not None:
            gap = _number(ctx, file, raw["min_gap"], "recur.min_gap", line)
            if gap < 0:
                raise ctx.err(file, line, "recur.min_gap must be 0 or more seconds")
        return {"auto": auto, "min_gap": gap}
    raise ctx.err(file, line, "recur must be true or false, or a mapping {auto, min_gap}")


def compile_text(text: str, *, source: str = "",
                 resolve_include: Optional[ResolveInclude] = None) -> ir.Chart:
    """Compile one v2 document. ``source`` is its path (for ``src`` and errors)."""
    file = Path(source).name if source else "<string>"
    if resolve_include is None:
        resolve_include = _default_resolver(Path(source).parent if source else None)
    ctx = _Ctx(file=file, resolve_include=resolve_include)
    if source:
        ctx.included.add(str(Path(source).resolve()))
        ctx.including.append(str(Path(source).resolve()))
    doc = _check_header(ctx, _load_yaml(text, file), file)
    _absorb(ctx, doc, file)

    kind = doc.get("kind", ir.KIND_WORKFLOW)
    name = doc.get("name") or (Path(source).stem if source else "")
    if not isinstance(name, str) or not name:
        raise ctx.err(file, 1, "a definition needs a name")
    desc = doc.get("description") or ""
    if not isinstance(desc, str):
        raise ctx.err(file, _key_line(doc, "description"), "description must be a string")
    filter_roles = doc.get("filter_roles")
    if filter_roles is not None:
        fr = _need_map(ctx, file, filter_roles, "filter_roles", _key_line(doc, "filter_roles"))
        _check_keys(ctx, file, fr, ("type", "roles"), "filter_roles")
        if fr.get("type") not in ("whitelist", "blacklist"):
            raise ctx.err(file, _line(fr), "filter_roles.type must be whitelist or blacklist")
        _str_list(ctx, file, fr.get("roles"), "filter_roles.roles", _line(fr))
        filter_roles = _plain(fr)
    priority = doc.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        raise ctx.err(file, _key_line(doc, "priority"), "priority must be a number")
    data = doc.get("data")
    if data is not None:
        _need_map(ctx, file, data, "data", _key_line(doc, "data"))
        if DATA_INPUTS in data:
            raise ctx.err(file, _key_line(data, DATA_INPUTS, _key_line(doc, "data")),
                          f"data key {DATA_INPUTS!r} is reserved for the run's start inputs "
                          f"(declare them under inputs:, docs 2.6)")
    # 0: no limit (doc 2.1; check warns unbounded-visits)
    max_visits = _int(ctx, file, doc.get("max_visits", ir.DEFAULT_MAX_VISITS), "max_visits",
                      _key_line(doc, "max_visits", 1), 0)
    from . import inputs as _inputs

    try:
        input_schema = _inputs.schema(_plain(doc.get("inputs")))
    except _inputs.InputsError as exc:
        raise ctx.err(file, _key_line(doc, "inputs", 1), str(exc)) from None
    try:
        input_constraints = _inputs.constraints(_plain(doc.get("input_constraints")),
                                                input_schema)
    except _inputs.InputsError as exc:
        raise ctx.err(file, _key_line(doc, "input_constraints", 1), str(exc)) from None

    collections = _collection_decls(ctx, file, doc)
    recur = _recur(ctx, file, doc, kind)
    sides = doc.get("sides")
    if sides is not None or ctx.included_sides:
        sline = _key_line(doc, "sides", 1)
        own = _need_map(ctx, file, sides, "sides", sline) if sides is not None else {}
        sides = {name: entry[0] for name, entry in ctx.included_sides.items()}
        sides.update(own)
        for sname, sdef in sides.items():
            if not isinstance(sname, str) or not _store.SIDE_NAME_RE.match(sname):
                raise ctx.err(file, sline, f"sides: bad side name {sname!r} (a letter or '_', "
                                           f"then letters, digits, '_', '-')")
            _definition_name(ctx, sdef, file, _key_line(sides, sname, sline), f"side {sname!r}")
        sides = _plain(sides)

    if kind == ir.KIND_LIBRARY:
        # nothing to run: the chart is one ``end`` so validate/list still work
        root = _alloc(ctx, "body")
        _add(ctx, ir.Node(id=root, type=ir.T_CONTROL, parent=None, src=_where(file, 1),
                          props={"op": ir.CTL_END}))
    else:
        if "body" not in doc:
            raise ctx.err(file, 1, "a definition needs a body")
        tasks = _file_tasks(ctx, doc, file)
        sc = _Scope(file=file, tasks=tasks, in_proc=kind == ir.KIND_PROCEDURE,
                    procs=(name,) if kind == ir.KIND_PROCEDURE else ())
        root = _compile_tree(ctx, sc, doc["body"], None, "body", _key_line(doc, "body", 1))

    chart = ir.Chart(
        name=name,
        nodes=ctx.nodes,
        root=root,
        kind=kind,
        description=desc,
        meta={
            "default_role": doc.get("default_role"),
            "filter_roles": filter_roles,
            "default_child_cflow": doc.get("default_child_cflow"),
            "priority": priority,
            "max_visits": max_visits,
            # typed start inputs (cflow2/inputs.py); folded into the chart
            # at start by inputs.apply
            "inputs": input_schema,
            # cross-input asserts, evaluated by inputs.normalize
            **({"input_constraints": input_constraints} if input_constraints else {}),
            # normalized collection declarations (cflow2/collections.py)
            "collections": collections,
            **({"sides": sides} if sides is not None else {}),
            # question type declarations, own and included (cflow2/question.py)
            **({"question_types": {n: e[0] for n, e in ctx.question_types.items()}}
               if ctx.question_types else {}),
            # recur: {"auto": bool} when declared (docs 2.1, recur)
            **({"recur": recur} if recur is not None else {}),
        },
        params=_workflow_params(ctx, file, doc),
        returns=_returns(ctx, file, doc["returns"], _key_line(doc, "returns"))
        if doc.get("returns") is not None else {},
        data=_plain(data or {}),
        rules=_plain(ctx.rules),
        spec=_spec(ctx, file, doc.get("spec"), _key_line(doc, "spec")),
        source=str(source or ""),
        warnings=list(ctx.warnings),
    )
    _narrow_task_marks(ctx, chart)
    try:
        ir.validate(chart)
    except ir.IRError as exc:  # a frontend bug, but say where
        raise FrontendError(f"{file}: compiled chart is invalid: {exc}") from None
    return chart


def compile_file(path: Path, *, resolve_include: Optional[ResolveInclude] = None) -> ir.Chart:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FrontendError(f"{path}: cannot read: {exc}") from None
    return compile_text(text, source=str(path), resolve_include=resolve_include)
