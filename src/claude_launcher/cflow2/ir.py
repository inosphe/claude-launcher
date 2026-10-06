"""The cflow v2 intermediate representation: a hierarchical statechart.

The surface syntax (``frontend``) compiles into this; the runtime, the static
checker, the dashboard and the prompt renderer read only this. It is plain
data -- every value round-trips through JSON -- so a run snapshots it once
(``chart.json``) and is never re-interpreted against an edited definition.

Spec: ``docs/cflow-v2.md`` section 3.

Node types and their statechart reading:

=========  ==========================================  ======================
type       statechart                                  fields (beyond common)
=========  ==========================================  ======================
task       atomic state                                title, prompt, text_id,
                                                       done_when, verify, ask,
                                                       needs, returns, effects,
                                                       paths, budget, rules,
                                                       strict_budget
seq        compound state, children in order           children, pass_prev
cond       compound state with guarded transitions     branches[{guard, child}]
choose     external choice                             prompt, chooser,
                                                       require_reason,
                                                       options[{name,
                                                       description, guard,
                                                       child}]
loop       compound state with a self transition       mode, guard, max, child,
                                                       between, every, items,
                                                       as_name, parallel, carry
par        parallel regions                            branches{name: child},
                                                       settle
timeout    compound state with a timed transition      child, seconds, else_child
try        compound state with outcome transitions     child, catch{outcome:
                                                       child}, finally_child
wait       atomic state waiting on the outside         probe, poll, seconds,
                                                       describe
group      dynamic parallel region                     name, child, member,
                                                       limit, settle
control    a transition out of the enclosing scope     op, target, outcome,
                                                       value
scope      an inlined ``call``                         proc, child, with_
=========  ==========================================  ======================

Common to every node: ``id``, ``type``, ``parent``, ``src`` (``file:line``),
``label`` (the author's name, if any), ``executor`` (a delegation point when
non-empty), ``needs``, ``budget``, ``bind``, ``on`` (outcome -> node id of a
``control`` node or a subtree).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

#: Bumped when a stored ``chart.json`` would no longer read the same.
IR_VERSION = 1

#: The surface format this package compiles (the ``cflow:`` key's value).
FORMAT_VERSION = 2

KIND_WORKFLOW = "workflow"
KIND_PROCEDURE = "procedure"
#: Included only: fragments, rules, procedures and type declarations, no body.
KIND_LIBRARY = "library"
KINDS = (KIND_WORKFLOW, KIND_PROCEDURE, KIND_LIBRARY)

T_TASK = "task"
T_SEQ = "seq"
T_COND = "cond"
T_CHOOSE = "choose"
T_LOOP = "loop"
T_PAR = "par"
T_TIMEOUT = "timeout"
T_TRY = "try"
T_WAIT = "wait"
T_GROUP = "group"
T_CONTROL = "control"
T_SCOPE = "scope"
NODE_TYPES = (
    T_TASK, T_SEQ, T_COND, T_CHOOSE, T_LOOP, T_PAR, T_TIMEOUT, T_TRY,
    T_WAIT, T_GROUP, T_CONTROL, T_SCOPE,
)
#: Node types an agent (or the outside) completes; everything else is
#: completed by the engine from its children.
ATOMIC_TYPES = (T_TASK, T_WAIT)

LOOP_WHILE = "while"
LOOP_UNTIL = "until"
LOOP_FOREACH = "foreach"
LOOP_RETRY = "retry"
LOOP_MODES = (LOOP_WHILE, LOOP_UNTIL, LOOP_FOREACH, LOOP_RETRY)

SETTLE_ALL = "all"                # every branch ok; first non-ok fails fast
SETTLE_ALL_SETTLED = "allSettled" # wait for every branch, whatever outcome
SETTLE_RACE = "race"              # first branch to finish decides
SETTLE_ANY = "any"                # first ok decides; all non-ok -> fail
SETTLE_QUORUM = "quorum"          # spelled quorum:K -- K ok decide
SETTLE_MODES = (SETTLE_ALL, SETTLE_ALL_SETTLED, SETTLE_RACE, SETTLE_ANY, SETTLE_QUORUM)

CTL_CONTINUE = "continue"
CTL_BREAK = "break"
CTL_RAISE = "raise"
CTL_RETURN = "return"
CTL_END = "end"
CONTROL_OPS = (CTL_CONTINUE, CTL_BREAK, CTL_RAISE, CTL_RETURN, CTL_END)

EXEC_INLINE = "inline"
EXEC_SUBAGENT = "subagent"
EXEC_HEADLESS = "headless"
EXEC_SESSION = "session"
EXECUTORS = (EXEC_INLINE, EXEC_SUBAGENT, EXEC_HEADLESS, EXEC_SESSION)

CHOOSER_AGENT = "agent"
CHOOSER_USER = "user"
CHOOSERS = (CHOOSER_AGENT, CHOOSER_USER)

#: Default of the top-level ``max_visits`` (``chart.meta['max_visits']``): how
#: many times one node may be entered over a whole run before the run holds
#: for a person (reason ``visit_limit``), whatever loop got it there.
DEFAULT_MAX_VISITS = 200

#: Seconds between a recurring run's end and the auto start of its next round
#: when ``recur.min_gap`` is not given (docs 2.1, recur).
RECUR_MIN_GAP_DEFAULT = 60.0

#: A task's ``restart`` command runs one of these scripts and nothing else
#: (docs 2.2, restart): the script path, optionally behind an interpreter and
#: its flags. The daemon runs it with its own rights, so an agent-written
#: definition must not name an arbitrary command.
RESTART_SCRIPTS = ("tools/restart_live.sh", "tools/restart_live.ps1")
#: Interpreters a restart command may name, and the only flags each may take
#: (a flag mapped to True takes one value). No ``-c``/``-Command``: those run
#: text that is not the script.
RESTART_INTERPRETERS: Dict[str, Dict[str, bool]] = {
    "bash": {}, "sh": {},
    **{name: {"-noprofile": False, "-noninteractive": False, "-nologo": False,
              "-executionpolicy": True, "-file": False}
       for name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe")},
}
_RESTART_SHELL_CHARS = set(";&|`$<>()\n\r")


def restart_refusal(command: str) -> Optional[str]:
    """``None`` when ``command`` runs an allowed restart script; otherwise
    the reason it is refused. Allowed: ``[<interpreter> [flag [value]]...]
    <script> [args]`` -- the script one of :data:`RESTART_SCRIPTS` (``./``
    prefix allowed), the flags those :data:`RESTART_INTERPRETERS` lists for
    the interpreter, and no shell operator anywhere."""
    text = str(command or "").strip()
    if not text:
        return "is empty"
    if any(c in _RESTART_SHELL_CHARS for c in text):
        return "may not contain shell operators (; & | ` $ < > ( ))"
    words = text.split()
    i = 0
    flags = RESTART_INTERPRETERS.get(words[0].lower())
    if flags is not None:
        i = 1
        while i < len(words) and words[i].startswith("-"):
            flag = words[i].lower()
            if flag not in flags:
                return f"may not pass {words[i]} to {words[0]} (allowed: {', '.join(flags) or 'none'})"
            i += 2 if flags[flag] else 1
    if i < len(words) and _restart_script(words[i]) is not None:
        return None
    return ("must run one of " + ", ".join(RESTART_SCRIPTS)
            + " (optionally behind an interpreter); got " + repr(text))


def _restart_script(word: str) -> Optional[str]:
    w = word[2:] if word.startswith("./") else word
    w = w.replace("\\", "/")
    return w if w in RESTART_SCRIPTS else None


#: The outcome a node reports when nothing more specific is declared.
OUTCOME_OK = "ok"
#: The outcome the engine assigns when something failed (a delegate that
#: returned nothing usable, an ``ensures`` that did not hold, a timeout).
OUTCOME_FAIL = "fail"
OUTCOME_TIMEOUT = "timeout"
#: A ``wait`` whose probe could not decide (exit code other than 0/1)
#: ``undecided_max`` times in a row.
OUTCOME_UNDECIDABLE = "undecidable"

#: Effect vocabulary. Free-form strings are allowed (``a.b``), these are the
#: ones the checker and the fallback planner know about.
EFFECT_FS_READ = "fs.read"
EFFECT_FS_WRITE = "fs.write"
EFFECT_GIT_COMMIT = "git.commit"
EFFECT_GIT_MERGE = "git.merge"
EFFECT_GIT_PUSH = "git.push"
EFFECT_BOARD_WRITE = "board.write"
EFFECT_TEST_RUN = "test.run"
EFFECT_SESSION_SPAWN = "session.spawn"
#: Effects that do not change anything outside the delegate -- a node whose
#: effects are all in this set is a read-only delegation (doc 6.4).
READ_ONLY_EFFECTS = frozenset({EFFECT_FS_READ, EFFECT_TEST_RUN})


class IRError(Exception):
    """A chart that does not hold together (raised by :func:`validate`)."""


@dataclass
class Node:
    id: str
    type: str
    parent: Optional[str] = None
    src: str = ""
    label: str = ""
    executor: List[str] = field(default_factory=list)
    needs: List[str] = field(default_factory=list)
    budget: Optional[Dict[str, Any]] = None
    bind: Optional[str] = None
    #: outcome -> node id (a ``control`` node or any subtree root)
    on: Dict[str, str] = field(default_factory=dict)
    #: type-specific fields, kept in one mapping so the dataclass does not
    #: grow a field per node type; :data:`FIELDS` names what each type holds.
    props: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.props.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "type": self.type}
        if self.parent is not None:
            out["parent"] = self.parent
        for key in ("src", "label"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.executor:
            out["executor"] = list(self.executor)
        if self.needs:
            out["needs"] = list(self.needs)
        if self.budget:
            out["budget"] = dict(self.budget)
        if self.bind:
            out["bind"] = self.bind
        if self.on:
            out["on"] = dict(self.on)
        out.update(self.props)
        return out

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Node":
        common = {"id", "type", "parent", "src", "label", "executor", "needs",
                  "budget", "bind", "on"}
        return cls(
            id=str(raw["id"]),
            type=str(raw["type"]),
            parent=raw.get("parent"),
            src=str(raw.get("src") or ""),
            label=str(raw.get("label") or ""),
            executor=list(raw.get("executor") or []),
            needs=list(raw.get("needs") or []),
            budget=dict(raw["budget"]) if raw.get("budget") else None,
            bind=raw.get("bind"),
            on=dict(raw.get("on") or {}),
            props={k: v for k, v in raw.items() if k not in common},
        )


#: Type -> the keys of ``props`` that hold child node ids (single id, list of
#: ids, or mapping of name -> id). The walkers below read only this table.
CHILD_KEYS: Dict[str, Tuple[str, ...]] = {
    T_TASK: (),
    T_SEQ: ("children",),
    T_COND: ("branches",),
    T_CHOOSE: ("options",),
    T_LOOP: ("child", "between"),
    T_PAR: ("branches",),
    T_TIMEOUT: ("child", "else_child"),
    T_TRY: ("child", "catch", "finally_child"),
    T_WAIT: (),
    T_GROUP: ("child", "member"),
    T_CONTROL: (),
    T_SCOPE: ("child",),
}


@dataclass
class Chart:
    name: str
    nodes: Dict[str, Node]
    root: str
    kind: str = KIND_WORKFLOW
    description: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    returns: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)
    rules: Dict[str, Any] = field(default_factory=dict)
    spec: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    warnings: List[str] = field(default_factory=list)
    ir_version: int = IR_VERSION

    # -- access ----------------------------------------------------------
    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise IRError(f"{self.name}: no node {node_id!r}") from None

    def children(self, node_id: str) -> List[str]:
        """Direct children of a node, in declaration order (``on`` targets
        included, last)."""
        node = self.node(node_id)
        out: List[str] = []
        for key in CHILD_KEYS.get(node.type, ()):
            out.extend(_ids_in(node.props.get(key)))
        out.extend(v for v in node.on.values())
        return out

    def walk(self, start: Optional[str] = None) -> Iterator[Node]:
        """Every node reachable from ``start`` (default: root), pre-order."""
        seen = set()
        stack = [start or self.root]
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            node = self.node(nid)
            yield node
            stack.extend(reversed(self.children(nid)))

    def tasks(self) -> List[Node]:
        return [n for n in self.walk() if n.type == T_TASK]

    def ancestors(self, node_id: str) -> List[str]:
        out: List[str] = []
        cur = self.node(node_id).parent
        while cur is not None:
            out.append(cur)
            cur = self.node(cur).parent
        return out

    def by_label(self, label: str) -> Optional[Node]:
        for node in self.nodes.values():
            if node.label == label or node.id == label:
                return node
        return None

    def delegation_points(self) -> List[Node]:
        return [n for n in self.walk() if n.executor and n.executor != [EXEC_INLINE]]

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "ir_version": self.ir_version,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "meta": dict(self.meta),
            "params": self.params,
            "returns": self.returns,
            "data": self.data,
            "rules": self.rules,
            "spec": self.spec,
            "source": self.source,
            "warnings": list(self.warnings),
            "root": self.root,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1, sort_keys=False)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Chart":
        version = int(raw.get("ir_version") or 0)
        if version != IR_VERSION:
            raise IRError(
                f"chart {raw.get('name')!r} has ir_version {version}; this "
                f"build reads {IR_VERSION}"
            )
        return cls(
            name=str(raw["name"]),
            kind=str(raw.get("kind") or KIND_WORKFLOW),
            description=str(raw.get("description") or ""),
            meta=dict(raw.get("meta") or {}),
            params=dict(raw.get("params") or {}),
            returns=dict(raw.get("returns") or {}),
            data=dict(raw.get("data") or {}),
            rules=dict(raw.get("rules") or {}),
            spec=dict(raw.get("spec") or {}),
            source=str(raw.get("source") or ""),
            warnings=list(raw.get("warnings") or []),
            root=str(raw["root"]),
            nodes={nid: Node.from_dict(n) for nid, n in (raw.get("nodes") or {}).items()},
        )

    @classmethod
    def from_json(cls, text: str) -> "Chart":
        return cls.from_dict(json.loads(text))


def _ids_in(value: Any) -> List[str]:
    """Child ids held by one props value (see :data:`CHILD_KEYS`)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and "child" in item:
                out.extend(_ids_in(item["child"]))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_ids_in(item if not isinstance(item, dict) else item.get("child")))
        return out
    return []


def ensures_target(key: str) -> str:
    """The node part of a ``spec.ensures`` key ``<node>.<outcome>`` (a node id
    may itself hold dots, the outcome does not)."""
    return str(key).rsplit(".", 1)[0]


def subtree_nodes(chart: Chart, node_id: str) -> List[Node]:
    """The nodes a delegation of ``node_id`` runs: the node and everything
    under it, without the node's own ``on`` handlers (those run after the
    delegation returned, in the delegating run)."""
    root = chart.node(node_id)
    skip = set(root.on.values())
    out: List[Node] = [root]
    stack = [c for c in reversed(chart.children(node_id)) if c not in skip]
    seen = {node_id}
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = chart.node(nid)
        out.append(node)
        stack.extend(reversed(chart.children(nid)))
    return out


def subtree_is_flat(chart: Chart, node_id: str) -> bool:
    """Whether the subtree under ``node_id`` is a plain list of tasks.

    Flat: every node is a ``task`` or a ``seq``; no node below the root
    carries ``on`` or ``bind``; and no ``spec.ensures`` key targets a node
    below the root. The root's own ``on``/``bind``/``ensures`` do not count:
    the delegating run applies them when the delegation returns (the
    runtime's completion of the delegation node). A flat subtree can be
    handed to an executor as one brief whose tasks run in order; anything
    else needs the engine to drive it (doc section 7).
    """
    nodes = subtree_nodes(chart, node_id)
    inside = set()
    for node in nodes:
        if node.type not in (T_TASK, T_SEQ):
            return False
        if node.id != node_id:
            if node.on or node.bind:
                return False
            inside.add(node.id)
            if node.label:
                inside.add(node.label)
    for key in ((chart.spec or {}).get("ensures") or {}):
        if ensures_target(key) in inside:
            return False
    return True


def text_id(*parts: str) -> str:
    """Content-only id of a task's text: the template, never rendered values."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]


def validate(chart: Chart) -> None:
    """Structural checks every chart must pass, whoever produced it.

    The frontend runs this on what it compiles; the runtime runs it on a
    snapshot it loads. Semantic checks (spec, effects, reachability of an
    end) live in ``check``; these are the ones without which a walker would
    misbehave.
    """
    if chart.kind not in KINDS:
        raise IRError(f"{chart.name}: unknown kind {chart.kind!r}")
    if chart.root not in chart.nodes:
        raise IRError(f"{chart.name}: root {chart.root!r} is not a node")
    for nid, node in chart.nodes.items():
        if node.id != nid:
            raise IRError(f"{chart.name}: node keyed {nid!r} says id {node.id!r}")
        if node.type not in NODE_TYPES:
            raise IRError(f"{chart.name}: node {nid!r} has unknown type {node.type!r}")
        for ex in node.executor:
            if ex not in EXECUTORS:
                raise IRError(f"{chart.name}: node {nid!r} names unknown executor {ex!r}")
        for child in chart.children(nid):
            if child not in chart.nodes:
                raise IRError(f"{chart.name}: node {nid!r} points at missing {child!r}")
            if chart.nodes[child].parent != nid:
                raise IRError(
                    f"{chart.name}: node {child!r} is a child of {nid!r} but "
                    f"says parent {chart.nodes[child].parent!r}"
                )
        if node.type == T_LOOP:
            mode = node.get("mode")
            if mode not in LOOP_MODES:
                raise IRError(f"{chart.name}: loop {nid!r} has unknown mode {mode!r}")
            if not isinstance(node.get("max"), int) or node.get("max") < 1:
                raise IRError(f"{chart.name}: loop {nid!r} needs a positive integer max")
        if node.type in (T_PAR, T_GROUP):
            settle = str(node.get("settle") or "")
            base = settle.split(":", 1)[0]
            if base not in SETTLE_MODES:
                raise IRError(f"{chart.name}: {node.type} {nid!r} has unknown settle {settle!r}")
        if node.type == T_CONTROL and node.get("op") not in CONTROL_OPS:
            raise IRError(f"{chart.name}: control {nid!r} has unknown op {node.get('op')!r}")
    reached = {n.id for n in chart.walk()}
    stray = sorted(set(chart.nodes) - reached)
    if stray:
        raise IRError(f"{chart.name}: nodes not reachable from root: {', '.join(stray)}")
