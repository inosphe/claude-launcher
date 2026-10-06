"""Static checks over a compiled v2 chart (``docs/cflow-v2.md`` section 8).

Every check reads only the IR. Codes:

- ``unbounded-loop``    a loop without a positive integer ``max``
- ``never-effect``      a task declares an effect a ``spec.invariants`` entry
                        or a rule's ``effect: {deny, target}`` forbids
- ``must-pass``         a path from the start to ``spec.paths.*.to`` skips a
                        ``must_pass`` node (the message carries the path)
- ``no-end``            no path reaches completion
- ``par-write-overlap`` (warning) parallel branches that write and whose
                        ``paths`` overlap or are undeclared
- ``unknown-capability`` a ``needs`` entry that ``caps.CAPABILITIES`` lacks
- ``unknown-rule``      a task names a rule the chart does not define
- ``delegated-ask``     a task with ``ask`` under a non-inline executor

Effects may carry a target: ``git.merge:master`` or ``git.merge(master)``.
An effect without a target may hit any target.

The control-flow graph (:func:`cfg`) models normal completion only: a node's
declared outcomes (plus the engine outcomes ``fail``/``timeout``) lead to the
``on`` handlers and the ``catch`` of enclosing ``try`` nodes, any other
outcome is taken to continue normally. An unhandled failure aborting the run
is not a path to ``END``. ``par`` with ``all``/``allSettled`` runs every
branch (modelled one after another in declaration order); ``race``/``any``/
``quorum`` branches are alternatives. A ``group`` member runs zero or more
times before its ``do`` tree. ``while``/``until``/``foreach`` may run zero
times; ``retry`` runs at least once, and a ``continue`` inside it always
starts another attempt (the loop limit is not a path to ``END``).
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import ir

START = "START"
END = "END"

ERROR = "error"
WARNING = "warning"


@dataclass
class Finding:
    level: str
    code: str
    message: str
    node: Optional[str] = None

    def __str__(self) -> str:
        where = f" [{self.node}]" if self.node else ""
        return f"{self.level}: {self.code}{where}: {self.message}"


# ---------------------------------------------------------------------------
# control-flow graph


class _Graph:
    def __init__(self) -> None:
        self.edges: Dict[str, Set[str]] = {}

    def add(self, a: str, b: str) -> None:
        self.edges.setdefault(a, set()).add(b)
        self.edges.setdefault(b, set())


def _in(nid: str) -> str:
    return "in:" + nid


def _done(nid: str) -> str:
    return "done:" + nid


def _out(nid: str) -> str:
    return "out:" + nid


def _possible_outcomes(node: ir.Node) -> Set[str]:
    outs = {ir.OUTCOME_FAIL, ir.OUTCOME_TIMEOUT}
    if node.type == ir.T_TASK:
        outs |= set((node.get("returns") or {ir.OUTCOME_OK: {}}).keys())
    else:
        outs.add(ir.OUTCOME_OK)
    return outs


def _region(chart: ir.Chart, nid: str) -> Optional[Tuple[str, str]]:
    """(parent id, props key) that holds ``nid`` in its parent."""
    node = chart.node(nid)
    if node.parent is None:
        return None
    parent = chart.node(node.parent)
    for key in ir.CHILD_KEYS.get(parent.type, ()):
        if nid in ir._ids_in(parent.props.get(key)):
            return parent.id, key
    return parent.id, "on"


def _build(chart: ir.Chart) -> _Graph:
    g = _Graph()
    for node in chart.walk():
        _build_node(chart, g, node)
    g.add(START, _in(chart.root))
    g.add(_out(chart.root), END)
    return g


def _build_node(chart: ir.Chart, g: _Graph, node: ir.Node) -> None:
    nid = node.id
    t = node.type
    p = node.props
    g.add(_done(nid), _out(nid))
    for handler in node.on.values():
        g.add(_done(nid), _in(handler))
        g.add(_out(handler), _out(nid))

    if t in ir.ATOMIC_TYPES:
        g.add(_in(nid), nid)
        g.add(nid, _done(nid))
        _atomic_exceptional(chart, g, node)
    elif t == ir.T_SEQ:
        kids = list(p.get("children") or [])
        if not kids:
            g.add(_in(nid), _done(nid))
        else:
            g.add(_in(nid), _in(kids[0]))
            for a, b in zip(kids, kids[1:]):
                g.add(_out(a), _in(b))
            g.add(_out(kids[-1]), _done(nid))
    elif t == ir.T_COND:
        has_else = False
        for br in p.get("branches") or []:
            g.add(_in(nid), _in(br["child"]))
            g.add(_out(br["child"]), _done(nid))
            has_else = has_else or br.get("guard") is None
        if not has_else:
            g.add(_in(nid), _done(nid))
    elif t == ir.T_CHOOSE:
        for opt in p.get("options") or []:
            g.add(_in(nid), _in(opt["child"]))
            g.add(_out(opt["child"]), _done(nid))
    elif t == ir.T_LOOP:
        child = p["child"]
        between = p.get("between")
        g.add(_in(nid), _in(child))
        if p.get("mode") != ir.LOOP_RETRY:
            g.add(_in(nid), _done(nid))
        g.add(_out(child), _done(nid))
        g.add(_out(child), _loop_again(node))
        if between:
            g.add(_out(between), _in(child))
    elif t == ir.T_PAR:
        kids = list((p.get("branches") or {}).values())
        settle = str(p.get("settle") or ir.SETTLE_ALL)
        if settle in (ir.SETTLE_ALL, ir.SETTLE_ALL_SETTLED):
            g.add(_in(nid), _in(kids[0]))
            for a, b in zip(kids, kids[1:]):
                g.add(_out(a), _in(b))
            g.add(_out(kids[-1]), _done(nid))
        else:
            for k in kids:
                g.add(_in(nid), _in(k))
                g.add(_out(k), _done(nid))
    elif t == ir.T_GROUP:
        child, member = p["child"], p.get("member")
        g.add(_in(nid), _in(child))
        if member:
            g.add(_in(nid), _in(member))
            g.add(_out(member), _in(member))
            g.add(_out(member), _in(child))
        g.add(_out(child), _done(nid))
    elif t == ir.T_TIMEOUT:
        child, other = p["child"], p.get("else_child")
        g.add(_in(nid), _in(child))
        g.add(_out(child), _done(nid))
        fire = _in(other) if other else _done(nid)
        for a in _atomics_under(chart, child):
            g.add(a, fire)
        if other:
            g.add(_out(other), _done(nid))
    elif t == ir.T_TRY:
        child, fin = p["child"], p.get("finally_child")
        after = _in(fin) if fin else _done(nid)
        g.add(_in(nid), _in(child))
        g.add(_out(child), after)
        for handler in (p.get("catch") or {}).values():
            g.add(_out(handler), after)
        if fin:
            g.add(_out(fin), _done(nid))
    elif t == ir.T_SCOPE:
        g.add(_in(nid), _in(p["child"]))
        g.add(_out(p["child"]), _done(nid))
    elif t == ir.T_CONTROL:
        _control(chart, g, node)


def _loop_again(loop: ir.Node) -> str:
    between = loop.get("between")
    return _in(between) if between else _in(loop.get("child"))


def _atomics_under(chart: ir.Chart, nid: str) -> List[str]:
    return [n.id for n in chart.walk(nid) if n.type in ir.ATOMIC_TYPES]


def _catching_try(chart: ir.Chart, nid: str, outcome: str) -> Optional[ir.Node]:
    """Nearest try whose ``child`` region holds ``nid`` and catches ``outcome``."""
    cur = nid
    while True:
        reg = _region(chart, cur)
        if reg is None:
            return None
        pid, key = reg
        parent = chart.node(pid)
        if parent.type == ir.T_TRY and key == "child" and outcome in (parent.get("catch") or {}):
            return parent
        cur = pid


def _atomic_exceptional(chart: ir.Chart, g: _Graph, node: ir.Node) -> None:
    for outcome in _possible_outcomes(node):
        if outcome in node.on:
            continue
        tnode = _catching_try(chart, node.id, outcome)
        if tnode is not None:
            g.add(node.id, _in(tnode.get("catch")[outcome]))


def _control(chart: ir.Chart, g: _Graph, node: ir.Node) -> None:
    op = node.get("op")
    nid = node.id
    if op == ir.CTL_END:
        g.add(_in(nid), END)
    elif op in (ir.CTL_CONTINUE, ir.CTL_BREAK):
        target = node.get("target")
        if target and target in chart.nodes:
            loop = chart.node(target)
            if op == ir.CTL_BREAK:
                g.add(_in(nid), _done(target))
            else:
                g.add(_in(nid), _loop_again(loop))
                # the guard (or the item list) may end the loop; a retry's
                # next attempt is unconditional up to its max
                if loop.get("mode") != ir.LOOP_RETRY:
                    g.add(_in(nid), _done(target))
    elif op == ir.CTL_RAISE:
        tnode = _catching_try(chart, nid, node.get("outcome"))
        if tnode is not None:
            g.add(_in(nid), _in(tnode.get("catch")[node.get("outcome")]))
        else:
            g.edges.setdefault(_in(nid), set())
    elif op == ir.CTL_RETURN:
        for anc in chart.ancestors(nid):
            if chart.node(anc).type == ir.T_SCOPE:
                g.add(_in(nid), _done(anc))
                break
        else:
            g.add(_in(nid), END)


def cfg(chart: ir.Chart) -> Dict[str, List[str]]:
    """Successor map over atomic node ids (``task``, ``wait``) plus the
    synthetic ``START`` and ``END``: ``succ[a]`` lists the atomic nodes (or
    ``END``) that can run next after ``a`` completes."""
    g = _build(chart)
    keep = [START] + [n.id for n in chart.walk() if n.type in ir.ATOMIC_TYPES]
    out: Dict[str, List[str]] = {}
    for a in keep:
        found: Set[str] = set()
        seen: Set[str] = set()
        stack = list(g.edges.get(a, ()))
        while stack:
            v = stack.pop()
            if v in seen:
                continue
            seen.add(v)
            if v == END or v in chart.nodes:
                found.add(v)
                continue
            stack.extend(g.edges.get(v, ()))
        out[a] = sorted(found)
    out[END] = []
    return out


def _path(succ: Dict[str, List[str]], goals: Set[str], banned: Set[str]) -> Optional[List[str]]:
    """Shortest START -> goal path avoiding ``banned``; ``None`` if none."""
    prev: Dict[str, Optional[str]] = {START: None}
    q = deque([START])
    while q:
        v = q.popleft()
        if v in goals:
            path = []
            cur: Optional[str] = v
            while cur is not None:
                path.append(cur)
                cur = prev[cur]
            return list(reversed(path))
        for w in succ.get(v, ()):
            if w in banned or w in prev:
                continue
            prev[w] = v
            q.append(w)
    return None


# ---------------------------------------------------------------------------
# checks


def _resolve_label(chart: ir.Chart, label: str) -> Set[str]:
    """Atomic node ids standing for ``label`` (a task used twice is two ids;
    a compound node stands for the atomic nodes under it)."""
    out: Set[str] = set()
    for node in chart.walk():
        if node.id == label or node.label == label:
            if node.type in ir.ATOMIC_TYPES:
                out.add(node.id)
            else:
                out.update(_atomics_under(chart, node.id))
    return out


_TARGET_RE = re.compile(r"^([^:()]+)(?::(.+)|\((.*)\))?$")


def split_effect(effect: str) -> Tuple[str, Optional[str]]:
    """``git.merge:master`` / ``git.merge(master)`` -> (``git.merge``, ``master``)."""
    m = _TARGET_RE.match(effect.strip())
    if not m:
        return effect.strip(), None
    target = m.group(2) if m.group(2) is not None else m.group(3)
    return m.group(1).strip(), (target.strip() if target else None)


def _effect_hits(effect: str, deny: str, target: Optional[str]) -> bool:
    name, etarget = split_effect(effect)
    if name != deny:
        return False
    return target is None or etarget is None or etarget == target


def _write_effects(node: ir.Node) -> List[str]:
    return [e for e in node.get("effects") or [] if split_effect(e)[0] not in ir.READ_ONLY_EFFECTS]


def _glob_prefix(pattern: str) -> str:
    m = re.search(r"[*?\[]", pattern)
    return pattern[: m.start()] if m else pattern


def _globs_overlap(a: str, b: str) -> bool:
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    pa, pb = _glob_prefix(a), _glob_prefix(b)
    if pa == a and pb == b:
        return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
    return pa.startswith(pb) or pb.startswith(pa)


def _branch_writes(chart: ir.Chart, root: str) -> Tuple[bool, Optional[List[str]]]:
    """(writes anything, declared paths or ``None`` if a writer declares none)."""
    writes = False
    paths: Optional[List[str]] = []
    for n in chart.walk(root):
        if n.type != ir.T_TASK or not _write_effects(n):
            continue
        writes = True
        declared = list(n.get("paths") or [])
        if not declared:
            paths = None
        elif paths is not None:
            paths.extend(declared)
    return writes, paths


def _effective_executor(chart: ir.Chart, node: ir.Node) -> List[str]:
    if node.executor:
        return node.executor
    for anc in chart.ancestors(node.id):
        ex = chart.node(anc).executor
        if ex:
            return ex
    return []


def _known_caps() -> Optional[Set[str]]:
    try:
        from . import caps
    except Exception:  # noqa: BLE001 - the module is optional here
        return None
    table = getattr(caps, "CAPABILITIES", None)
    return set(table) if isinstance(table, dict) else None


def check(chart: ir.Chart) -> List[Finding]:
    out: List[Finding] = []
    nodes = list(chart.walk())
    tasks = [n for n in nodes if n.type == ir.T_TASK]

    for n in nodes:
        if n.type == ir.T_LOOP:
            mx = n.get("max")
            if isinstance(mx, bool) or not isinstance(mx, int) or mx < 1:
                out.append(Finding(ERROR, "unbounded-loop", f"loop {n.id} has no positive max", n.id))

    denials: List[Tuple[str, str, Optional[str]]] = []
    for name, inv in ((chart.spec or {}).get("invariants") or {}).items():
        if inv.get("never_effect"):
            denials.append((f"invariant {name}", inv["never_effect"], inv.get("target")))
    for rid, rule in (chart.rules or {}).items():
        eff = (rule or {}).get("effect") or {}
        if eff.get("deny"):
            denials.append((f"rule {rid}", eff["deny"], eff.get("target")))
    for source, deny, target in denials:
        for t in tasks:
            for e in t.get("effects") or []:
                if _effect_hits(e, deny, target):
                    tgt = f" on {target}" if target else ""
                    out.append(Finding(ERROR, "never-effect",
                                       f"task {t.id} declares effect {e!r}; {source} forbids {deny}{tgt}", t.id))

    known_caps = _known_caps()
    for n in nodes:
        if known_caps is not None:
            for cap in n.needs:
                if cap not in known_caps:
                    out.append(Finding(ERROR, "unknown-capability",
                                       f"node {n.id} needs unknown capability {cap!r}", n.id))
    for t in tasks:
        for rid in t.get("rules") or []:
            if rid not in (chart.rules or {}):
                out.append(Finding(ERROR, "unknown-rule", f"task {t.id} names unknown rule {rid!r}", t.id))
        if t.get("ask"):
            ex = _effective_executor(chart, t)
            remote = [e for e in ex if e != ir.EXEC_INLINE]
            if remote:
                out.append(Finding(ERROR, "delegated-ask",
                                   f"task {t.id} asks for approval but may run under executor "
                                   f"{', '.join(remote)}; approvals cannot run inside a delegate", t.id))

    for n in nodes:
        if n.type == ir.T_PAR:
            regions = list((n.get("branches") or {}).items())
        elif n.type == ir.T_GROUP:
            regions = [("do", n.get("child"))]
            if n.get("member"):
                # members run side by side with each other: count it twice
                regions += [("member", n.get("member")), ("member#2", n.get("member"))]
        else:
            continue
        writers = []
        for name, root in regions:
            writes, paths = _branch_writes(chart, root)
            if writes:
                writers.append((name, paths))
        if len(writers) < 2:
            continue
        undeclared = [name for name, paths in writers if paths is None]
        if undeclared:
            out.append(Finding(WARNING, "par-write-overlap",
                               f"{n.type} {n.id}: branches {', '.join(sorted(set(undeclared)))} write "
                               f"without declared paths; the runtime will isolate or serialize them", n.id))
            continue
        for i in range(len(writers)):
            for j in range(i + 1, len(writers)):
                (na, pa), (nb, pb) = writers[i], writers[j]
                hit = next(((a, b) for a in pa for b in pb if _globs_overlap(a, b)), None)
                if hit:
                    out.append(Finding(WARNING, "par-write-overlap",
                                       f"{n.type} {n.id}: branches {na} and {nb} write overlapping "
                                       f"paths {hit[0]!r} / {hit[1]!r}; the runtime will isolate or "
                                       f"serialize them", n.id))

    succ = cfg(chart)
    if _path(succ, {END}, set()) is None:
        out.append(Finding(ERROR, "no-end", "no path from the start reaches completion", chart.root))

    for name, spec in ((chart.spec or {}).get("paths") or {}).items():
        to = spec.get("to")
        goals = {END} if to == "end" else _resolve_label(chart, str(to))
        if not goals:
            out.append(Finding(ERROR, "must-pass", f"path {name}: unknown target {to!r}"))
            continue
        for label in spec.get("must_pass") or []:
            must = _resolve_label(chart, str(label))
            if not must:
                out.append(Finding(ERROR, "must-pass", f"path {name}: unknown node {label!r}"))
                continue
            witness = _path(succ, goals - must, must)
            if witness is not None:
                out.append(Finding(ERROR, "must-pass",
                                   f"path {name}: {' -> '.join(witness)} reaches {to} without passing "
                                   f"{label}", witness[-1] if witness[-1] != END else None))
    return out


def errors(chart: ir.Chart) -> List[Finding]:
    return [f for f in check(chart) if f.level == ERROR]


__all__ = ("Finding", "check", "errors", "cfg", "split_effect", "START", "END")
