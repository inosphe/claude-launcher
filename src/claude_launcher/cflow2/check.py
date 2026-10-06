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
- ``delegated-ask``     a task with ``ask`` inside a subtree delegated to a
                        non-inline executor
- ``delegated-user-gate`` a ``choose`` with ``chooser: user`` inside such a
                        subtree (with ``delegated-ask``: no gate that waits
                        for a person may sit inside a delegation -- the
                        delegate would answer it, or skip it)
- ``effects-undeclared`` (warning) a task without ``effects`` in a chart
                        that has a ``never_effect`` invariant or a rule
                        ``effect``: the never-effect check cannot see it

Collections and triggers (docs/cflow-v2.md 2.7, 2.8; :func:`_collection_checks`):

- ``unknown-collection``  a ``do:`` entry, a ``wait`` ``until``/``consume``,
                        an expression (``items``/``count``/``fresh``/``exists``
                        with a literal address) or a prompt ``${col.<name>}``
                        names a ``self`` collection the chart does not declare;
                        also a ``lifecycle.reset_at`` entry that is not a node
- ``side-runs-unavailable`` a ``main:`` or ``side:<run>:`` address (side runs
                        are phase 2)
- ``parent-outside-trigger`` a ``parent:`` address anywhere but a trigger
- ``trigger-target``    a trigger ``push`` whose ``to`` is not ``parent:``
- ``facts-outside-trigger`` ``$facts`` or ``$each`` in an expression that is
                        not a trigger ``push`` (a foreach ``as: each`` binding
                        in scope excepted)
- ``collection-read-in-delegation`` a collection read (``items``/``count``/
                        ``fresh``/``exists``, a ``wait`` ``consume``, a prompt
                        ``${col.<name>}``) on a node strictly inside a delegated
                        subtree: the nested run's chart has no collections
- ``trigger-collection-read`` a collection function (``items``/``count``/
                        ``fresh``/``exists``) in a trigger ``push`` expression
                        (the daemon evaluates it with only ``$facts`` and ``$each``)
- ``effect-in-delegation`` a ``do:`` on a node strictly inside a subtree
                        delegated to a non-inline executor (the delegation
                        root itself may carry it)
- ``trigger-in-delegation`` a ``push`` or ``enqueue-landing`` trigger there
- ``collection-op-kind`` ``set``/``remove`` on a log, ``consume`` on a queue
- ``collection-field``  a ``push`` item or ``set`` fields naming a field the
                        declaration lacks, or a ``push`` missing a required one
- ``undeclared-status`` a ``set`` of ``status`` to a literal value the
                        declaration does not list (or a collection without
                        ``status``)
- ``do-access``         a ``do:`` operation whose ``access`` excludes
                        ``driver`` (node effects run as the driver)
- ``unpushable-collection`` (warning) an expression reads a collection
                        whose ``access.push`` is empty

Effects may carry a target: ``git.merge:master`` or ``git.merge(master)``.
An effect without a target may hit any target.

The control-flow graph (:func:`cfg`) follows the runtime's outcome
propagation (``runtime._complete``): an atomic node's possible outcomes are
its ``returns`` keys, its ask's outcomes, ``fail`` and ``timeout`` (a probing
wait adds ``undecidable``). ``ok`` continues normally; any other outcome
travels up to the nearest ``on`` handler for it on the node or an
ancestor, or the ``catch`` of an enclosing ``try`` (``*`` catches all), with
every ``finally`` on the way run first; a retry tries again; a par settles
by its mode. An outcome nothing handles ends the run: a path to ``END`` only
when the chart's ``returns`` declares it. Control signals (``end``,
``return``, ``break``, ``continue``) also run every ``finally`` they leave.
A cond with no true guard and no ``else`` finishes ``fail``. ``par`` with
``all``/``allSettled`` runs every branch (modelled one after another in
declaration order); ``race``/``any``/``quorum`` branches are alternatives.
A ``group`` member runs zero or more times before its ``do`` tree.
``while``/``until``/``foreach`` may run zero times; ``retry`` runs at least
once, and a ``continue`` inside it always starts another attempt (a loop
limit held for a person is not a path to ``END``).
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import collections as _collections
from . import expr as _expr
from . import ir

#: Expression roots the daemon sets only while it evaluates a trigger
#: ``push`` (docs/cflow-v2.md 2.8).
TRIGGER_ROOTS = ("facts", "each")

#: A ``wait`` with ``until`` whose expression reads a run that is gone
#: (docs/cflow-v2.md 2.7 「wait until」).
OUTCOME_ABSENT = "absent"

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
#
# Vertices are strings: ``START``/``END``; ``in:<id>``, ``done:<id>``,
# ``out:<id>`` around every node (``done`` = the node finished, before its
# ``on`` handlers; ``out`` = left to its parent); and the atomic node ids
# themselves. A ``finally`` subtree that some exit runs before continuing
# elsewhere (a control signal, or a non-ok outcome on its way up) is built
# again as a *copy* under a prefix ``f<k>|`` whose end leads only to that
# continuation -- otherwise every normal pass through the finally could
# follow the signal (a path that does not exist). An atomic vertex in a copy
# stands for the same node: ``_base`` strips the prefix.


class _Graph:
    def __init__(self) -> None:
        self.edges: Dict[str, Set[str]] = {}

    def add(self, a: str, b: str) -> None:
        self.edges.setdefault(a, set()).add(b)
        self.edges.setdefault(b, set())

    def touch(self, a: str) -> None:
        self.edges.setdefault(a, set())


def _in(nid: str) -> str:
    return "in:" + nid


def _done(nid: str) -> str:
    return "done:" + nid


def _out(nid: str) -> str:
    return "out:" + nid


def _base(vertex: str) -> str:
    return vertex.rsplit("|", 1)[-1]


def _possible_outcomes(node: ir.Node) -> Set[str]:
    """Every outcome a node may finish with by itself."""
    outs = {ir.OUTCOME_FAIL, ir.OUTCOME_TIMEOUT}
    if node.type == ir.T_TASK:
        outs |= set((node.get("returns") or {ir.OUTCOME_OK: {}}).keys())
        ask = node.get("ask") or {}
        if ask:
            if ask.get("options"):
                outs |= {o.get("outcome") or o.get("name") for o in ask["options"]}
            else:
                outs.add(ask.get("on_decline") or "declined")
    else:
        outs.add(ir.OUTCOME_OK)
        if node.type == ir.T_WAIT and node.get("probe"):
            outs.add(ir.OUTCOME_UNDECIDABLE)
        if node.type == ir.T_WAIT and node.get("until"):
            outs.add(OUTCOME_ABSENT)
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


def _ensured(chart: ir.Chart) -> Set[str]:
    """Node ids/labels some ``spec.ensures`` key names."""
    return {ir.ensures_target(k) for k in ((chart.spec or {}).get("ensures") or {})}


class _Space:
    """Where vertex names come from: the chart itself, or a copy of one
    ``finally`` subtree (``members``) under ``prefix`` inside ``parent``."""

    def __init__(self, prefix: str = "", members: Optional[Set[str]] = None,
                 parent: Optional["_Space"] = None) -> None:
        self.prefix = prefix
        self.members = members
        self.parent = parent

    def v(self, vertex: str) -> str:
        """The name of a base vertex (``in:x``, an atomic id ...) here."""
        if vertex in (START, END):
            return vertex
        nid = vertex.split(":", 1)[1] if ":" in vertex else vertex
        if self.members is None or nid in self.members:
            return self.prefix + vertex
        return self.parent.v(vertex) if self.parent else vertex


class _Builder:
    def __init__(self, chart: ir.Chart) -> None:
        self.chart = chart
        self.g = _Graph()
        self.copies: Dict[Tuple[str, str, Tuple[str, ...]], str] = {}
        self.ensured = _ensured(chart)
        # group id -> non-ok outcomes its members can settle it with
        self.group_fail: Dict[Tuple[str, str], Set[str]] = {}

    # -- whole chart / one copy ---------------------------------------------
    def build(self) -> _Graph:
        top = _Space()
        for node in self.chart.walk():
            self.node(top, node)
        self.g.add(START, _in(self.chart.root))
        self.g.add(_out(self.chart.root), END)
        return self.g

    def copy_finally(self, sp: _Space, try_id: str, conts: List[str]) -> str:
        """Entry vertex of a copy of ``try_id``'s finally whose end goes to
        ``conts`` (vertices already named in ``sp``)."""
        fin = self.chart.node(try_id).get("finally_child")
        key = (sp.prefix, try_id, tuple(sorted(conts)))
        if key in self.copies:
            return self.copies[key]
        prefix = f"f{len(self.copies)}|"
        inner = _Space(prefix, {n.id for n in self.chart.walk(fin)}, sp)
        entry = inner.v(_in(fin))
        self.copies[key] = entry
        for node in self.chart.walk(fin):
            self.node(inner, node)
        for c in conts:
            self.g.add(inner.v(_out(fin)), c)
        if not conts:
            self.g.touch(inner.v(_out(fin)))
        return entry

    # -- a non-ok outcome travelling up (runtime ``_complete``) --------------
    def propagate(self, sp: _Space, nid: str, outcome: str, skip_on: bool = True) -> List[str]:
        """Vertices a node finishing ``outcome`` (non-ok) leads to: the
        nearest ``on`` handler for it, a ``catch`` (``*`` included), the
        settle rule of a parallel region, a retry's next attempt -- running
        any ``finally`` on the way first. ``[]`` when the run ends failed;
        ``[END]`` when the chart's ``returns`` declares the outcome."""
        chart = self.chart
        cur, skip = nid, skip_on
        while True:
            node = chart.node(cur)
            if not skip and outcome in node.on:
                return [sp.v(_in(node.on[outcome]))]
            reg = _region(chart, cur)
            if reg is None:
                return [END] if outcome in (chart.returns or {}) else []
            pid, key = reg
            parent = chart.node(pid)
            skip = False
            if key == "on":
                cur, skip = pid, True
                continue
            t = parent.type
            if t == ir.T_TRY:
                if key == "child":
                    catch = parent.get("catch") or {}
                    handler = catch.get(outcome) or catch.get("*")
                    if handler:
                        return [sp.v(_in(handler))]
                if key in ("child", "catch") and parent.get("finally_child"):
                    conts = self.propagate(sp, pid, outcome, skip_on=False)
                    return [self.copy_finally(sp, pid, conts)]
                cur = pid
                continue
            if t == ir.T_LOOP:
                if key == "child" and parent.get("mode") == ir.LOOP_RETRY:
                    return [sp.v(_loop_again(parent))] + self.propagate(sp, pid, outcome, False)
                cur = pid
                continue
            if t == ir.T_PAR:
                settle = str(parent.get("settle") or ir.SETTLE_ALL).split(":", 1)[0]
                if settle == ir.SETTLE_ALL_SETTLED:
                    kids = list((parent.get("branches") or {}).values())
                    i = kids.index(cur)
                    return [sp.v(_in(kids[i + 1]) if i + 1 < len(kids) else _done(pid))]
                if settle in (ir.SETTLE_ANY, ir.SETTLE_QUORUM):
                    return [sp.v(_done(pid))] + self.propagate(sp, pid, ir.OUTCOME_FAIL, False)
                cur = pid
                continue
            if t == ir.T_GROUP and key == "member":
                settle = str(parent.get("settle") or ir.SETTLE_ALL_SETTLED).split(":", 1)[0]
                if settle != ir.SETTLE_ALL_SETTLED:
                    out = outcome if settle in (ir.SETTLE_ALL, ir.SETTLE_RACE) else ir.OUTCOME_FAIL
                    self.group_fail.setdefault((sp.prefix, pid), set()).add(out)
                    for target in self.propagate(sp, pid, out, False):
                        self.g.add(sp.v(_out(parent.get("child"))), target)
                # the group settles after its do tree; members keep coming
                return [sp.v(_in(cur)), sp.v(_in(parent.get("child")))]
            cur = pid

    # -- control signals (runtime ``_propagate``) ----------------------------
    def signal(self, sp: _Space, ctrl: str, stop: Optional[str], targets: List[str]) -> List[str]:
        """``targets`` reached from ``ctrl`` through every ``try`` with a
        ``finally`` whose body or catch holds it, below ``stop`` (the loop or
        scope the signal ends at; ``None`` = the run)."""
        chart = self.chart
        fins: List[str] = []
        cur = ctrl
        while True:
            reg = _region(chart, cur)
            if reg is None:
                break
            pid, key = reg
            if pid == stop:
                break
            parent = chart.node(pid)
            if parent.type == ir.T_TRY and key in ("child", "catch") and parent.get("finally_child"):
                fins.append(pid)
            cur = pid
        conts = list(targets)
        for tid in reversed(fins):
            conts = [self.copy_finally(sp, tid, conts)]
        return conts

    # -- one node --------------------------------------------------------------
    def node(self, sp: _Space, node: ir.Node) -> None:
        chart, add, v = self.chart, self.g.add, sp.v
        nid, t, p = node.id, node.type, node.props
        add(v(_done(nid)), v(_out(nid)))
        for handler in node.on.values():
            add(v(_done(nid)), v(_in(handler)))
            add(v(_out(handler)), v(_out(nid)))
        if t not in ir.ATOMIC_TYPES and (nid in self.ensured or node.label in self.ensured):
            for target in self.propagate(sp, nid, ir.OUTCOME_FAIL, skip_on=False):
                add(v(_done(nid)), target)

        if t in ir.ATOMIC_TYPES:
            add(v(_in(nid)), v(nid))
            add(v(nid), v(_done(nid)))
            for outcome in sorted(_possible_outcomes(node) - {ir.OUTCOME_OK}):
                if outcome in node.on:
                    continue  # done -> the handler, above
                for target in self.propagate(sp, nid, outcome):
                    add(v(nid), target)
                self.g.touch(v(nid))
        elif t == ir.T_SEQ:
            kids = list(p.get("children") or [])
            if not kids:
                add(v(_in(nid)), v(_done(nid)))
            else:
                add(v(_in(nid)), v(_in(kids[0])))
                for a, b in zip(kids, kids[1:]):
                    add(v(_out(a)), v(_in(b)))
                add(v(_out(kids[-1])), v(_done(nid)))
        elif t == ir.T_COND:
            has_else = False
            for br in p.get("branches") or []:
                add(v(_in(nid)), v(_in(br["child"])))
                add(v(_out(br["child"])), v(_done(nid)))
                has_else = has_else or br.get("guard") is None
            if not has_else:
                # no guard held and no else: the cond finishes ``fail``
                for target in self.propagate(sp, nid, ir.OUTCOME_FAIL, skip_on=False):
                    add(v(_in(nid)), target)
                self.g.touch(v(_in(nid)))
        elif t == ir.T_CHOOSE:
            for opt in p.get("options") or []:
                add(v(_in(nid)), v(_in(opt["child"])))
                add(v(_out(opt["child"])), v(_done(nid)))
        elif t == ir.T_LOOP:
            child = p["child"]
            between = p.get("between")
            add(v(_in(nid)), v(_in(child)))
            if p.get("mode") != ir.LOOP_RETRY:
                add(v(_in(nid)), v(_done(nid)))
            add(v(_out(child)), v(_done(nid)))
            add(v(_out(child)), v(_loop_again(node)))
            if between:
                add(v(_out(between)), v(_in(child)))
        elif t == ir.T_PAR:
            kids = list((p.get("branches") or {}).values())
            settle = str(p.get("settle") or ir.SETTLE_ALL)
            if settle in (ir.SETTLE_ALL, ir.SETTLE_ALL_SETTLED):
                add(v(_in(nid)), v(_in(kids[0])))
                for a, b in zip(kids, kids[1:]):
                    add(v(_out(a)), v(_in(b)))
                add(v(_out(kids[-1])), v(_done(nid)))
            else:
                for k in kids:
                    add(v(_in(nid)), v(_in(k)))
                    add(v(_out(k)), v(_done(nid)))
        elif t == ir.T_GROUP:
            child, member = p["child"], p.get("member")
            add(v(_in(nid)), v(_in(child)))
            if member:
                add(v(_in(nid)), v(_in(member)))
                add(v(_out(member)), v(_in(member)))
                add(v(_out(member)), v(_in(child)))
            add(v(_out(child)), v(_done(nid)))
        elif t == ir.T_TIMEOUT:
            child, other = p["child"], p.get("else_child")
            add(v(_in(nid)), v(_in(child)))
            add(v(_out(child)), v(_done(nid)))
            if other:
                fire = [v(_in(other))]
                add(v(_out(other)), v(_done(nid)))
            else:
                fire = self.propagate(sp, nid, ir.OUTCOME_TIMEOUT, skip_on=False)
            for a in _atomics_under(chart, child):
                for target in fire:
                    add(v(a), target)
        elif t == ir.T_TRY:
            child, fin = p["child"], p.get("finally_child")
            after = v(_in(fin)) if fin else v(_done(nid))
            add(v(_in(nid)), v(_in(child)))
            add(v(_out(child)), after)
            for handler in (p.get("catch") or {}).values():
                add(v(_out(handler)), after)
            if fin:
                add(v(_out(fin)), v(_done(nid)))
        elif t == ir.T_SCOPE:
            add(v(_in(nid)), v(_in(p["child"])))
            add(v(_out(p["child"])), v(_done(nid)))
        elif t == ir.T_CONTROL:
            self.control(sp, node)

    def control(self, sp: _Space, node: ir.Node) -> None:
        chart, v = self.chart, sp.v
        op = node.get("op")
        nid = node.id
        entry = v(_in(nid))
        self.g.touch(entry)
        targets: List[str] = []
        stop: Optional[str] = None
        if op == ir.CTL_END:
            targets = [END]
        elif op in (ir.CTL_CONTINUE, ir.CTL_BREAK):
            target = node.get("target")
            if not (target and target in chart.nodes):
                return
            loop = chart.node(target)
            stop = target
            if op == ir.CTL_BREAK:
                targets = [v(_done(target))]
            else:
                targets = [v(_loop_again(loop))]
                # the guard (or the item list) may end the loop; a retry's
                # next attempt is unconditional up to its max
                if loop.get("mode") != ir.LOOP_RETRY:
                    targets.append(v(_done(target)))
        elif op == ir.CTL_RAISE:
            # a raise finishes the control node with that outcome
            for target in self.propagate(sp, nid, str(node.get("outcome")), skip_on=False):
                self.g.add(entry, target)
            return
        elif op == ir.CTL_RETURN:
            outcome = str(node.get("outcome") or ir.OUTCOME_OK)
            scope = next((a for a in chart.ancestors(nid) if chart.node(a).type == ir.T_SCOPE), None)
            if scope is not None:
                stop = scope
                if outcome == ir.OUTCOME_OK:
                    targets = [v(_done(scope))]
                else:  # the scope finishes with that outcome: it travels on
                    targets = self.propagate(sp, scope, outcome, skip_on=False)
            else:
                ok = outcome == ir.OUTCOME_OK or outcome in (chart.returns or {})
                targets = [END] if ok else []
        for target in self.signal(sp, nid, stop, targets):
            self.g.add(entry, target)


def _build(chart: ir.Chart) -> _Graph:
    return _Builder(chart).build()


def _loop_again(loop: ir.Node) -> str:
    between = loop.get("between")
    return _in(between) if between else _in(loop.get("child"))


def _atomics_under(chart: ir.Chart, nid: str) -> List[str]:
    return [n.id for n in chart.walk(nid) if n.type in ir.ATOMIC_TYPES]


def _is_atomic(chart: ir.Chart, vertex: str) -> bool:
    base = _base(vertex)
    return base in chart.nodes and chart.nodes[base].type in ir.ATOMIC_TYPES and ":" not in base


def cfg(chart: ir.Chart) -> Dict[str, List[str]]:
    """Successor map over atomic node ids (``task``, ``wait``) plus the
    synthetic ``START`` and ``END``: ``succ[a]`` lists the atomic nodes (or
    ``END``) that can run next after ``a`` completes (whatever its outcome).
    A finally copy's nodes are reported under their own ids."""
    g = _build(chart)
    out: Dict[str, Set[str]] = {START: set()}
    for n in chart.walk():
        if n.type in ir.ATOMIC_TYPES:
            out.setdefault(n.id, set())
    starts = [START] + [vx for vx in g.edges if _is_atomic(chart, vx)]
    for a in starts:
        found: Set[str] = set()
        seen: Set[str] = set()
        stack = list(g.edges.get(a, ()))
        while stack:
            vx = stack.pop()
            if vx in seen:
                continue
            seen.add(vx)
            if vx == END or _is_atomic(chart, vx):
                found.add(_base(vx))
                continue
            stack.extend(g.edges.get(vx, ()))
        out.setdefault(_base(a), set()).update(found)
    result = {k: sorted(vs) for k, vs in out.items()}
    result[END] = []
    return result


def _path(chart: ir.Chart, g: _Graph, goals: Set[str], banned: Set[str]) -> Optional[List[str]]:
    """Shortest START -> goal path over the full graph avoiding atomic
    vertices whose node is in ``banned``; the atomic nodes on it (and
    ``START``/``END``), or ``None``."""
    prev: Dict[str, Optional[str]] = {START: None}
    q = deque([START])
    while q:
        vx = q.popleft()
        if vx != START and (vx == END and END in goals or
                            _is_atomic(chart, vx) and _base(vx) in goals):
            path = []
            cur: Optional[str] = vx
            while cur is not None:
                if cur in (START, END) or _is_atomic(chart, cur):
                    path.append(_base(cur))
                cur = prev[cur]
            return list(reversed(path))
        for w in sorted(g.edges.get(vx, ())):
            if w in prev:
                continue
            if _is_atomic(chart, w) and _base(w) in banned:
                continue
            prev[w] = vx
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


def _delegated_by(chart: ir.Chart, node: ir.Node) -> List[str]:
    """Non-inline executors of ``node`` or any ancestor (a delegated subtree
    runs whole in its executor, whatever its inner nodes say)."""
    out: List[str] = []
    for nid in [node.id] + chart.ancestors(node.id):
        for e in chart.node(nid).executor:
            if e != ir.EXEC_INLINE and e not in out:
                out.append(e)
    return out


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
            remote = _delegated_by(chart, t)
            if remote:
                out.append(Finding(ERROR, "delegated-ask",
                                   f"task {t.id} asks for approval but may run under executor "
                                   f"{', '.join(remote)}; approvals cannot run inside a delegate", t.id))
    for n in nodes:
        if n.type == ir.T_CHOOSE and n.get("chooser") == ir.CHOOSER_USER:
            remote = _delegated_by(chart, n)
            if remote:
                out.append(Finding(ERROR, "delegated-user-gate",
                                   f"choose {n.id} waits for the user but may run under executor "
                                   f"{', '.join(remote)}; a person's choice cannot run inside a "
                                   f"delegate -- move it out of the delegated subtree", n.id))
    if denials:
        for t in tasks:
            if not t.get("effects_declared", True):
                out.append(Finding(WARNING, "effects-undeclared",
                                   f"task {t.id} declares no effects; this chart forbids effects "
                                   f"({', '.join(sorted({src for src, _, _ in denials}))}) and the "
                                   f"check reads an undeclared task as doing nothing -- declare "
                                   f"effects (or effects: [] for none)", t.id))

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

    out.extend(_collection_checks(chart, nodes))

    graph = _build(chart)
    if _path(chart, graph, {END}, set()) is None:
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
            witness = _path(chart, graph, goals - must, must)
            if witness is not None:
                out.append(Finding(ERROR, "must-pass",
                                   f"path {name}: {' -> '.join(witness)} reaches {to} without passing "
                                   f"{label}", witness[-1] if witness[-1] != END else None))
    return out


_COL_PROMPT_RE = re.compile(r"\$\{col\.([A-Za-z_][A-Za-z0-9_-]*)")


def _delegated_above(chart: ir.Chart, node: ir.Node) -> List[str]:
    """Non-inline executors of the node's ancestors: the node sits strictly
    inside a delegated subtree (its own executor does not count -- the
    delegation root's effects run in the delegating run)."""
    out: List[str] = []
    for nid in chart.ancestors(node.id):
        for e in chart.node(nid).executor:
            if e != ir.EXEC_INLINE and e not in out:
                out.append(e)
    return out


def _literal(text: Optional[str]) -> Tuple[bool, object]:
    """``(True, value)`` when an expression is a plain literal."""
    if not isinstance(text, str):
        return False, None
    try:
        ast = _expr.parse(text).ast
    except _expr.ExprError:
        return False, None
    if ast and ast[0] == "lit":
        return True, ast[1]
    return False, None


def _node_exprs(chart: ir.Chart, node: ir.Node) -> List[Tuple[str, str, bool]]:
    """``(what, expression text, inside a trigger)`` for every expression the
    node holds."""
    out: List[Tuple[str, str, bool]] = []

    def add(what: str, text: object, trig: bool = False) -> None:
        if isinstance(text, str) and text:
            out.append((what, text, trig))

    if node.type == ir.T_COND:
        for i, br in enumerate(node.get("branches") or []):
            add(f"cond branch {i}", (br or {}).get("guard"))
    elif node.type == ir.T_CHOOSE:
        for opt in node.get("options") or []:
            add(f"option {opt.get('name')} when", (opt or {}).get("guard"))
    elif node.type == ir.T_LOOP:
        add("loop guard", node.get("guard"))
        add("foreach in", node.get("items"))
    elif node.type == ir.T_SCOPE:
        for k, v in (node.get("with_") or {}).items():
            if isinstance(v, dict):
                add(f"with {k}", v.get("expr"))
    elif node.type == ir.T_CONTROL:
        for k, v in (node.get("value") or {}).items():
            if isinstance(v, dict):
                add(f"return value {k}", v.get("expr"))
    elif node.type == ir.T_WAIT:
        add("wait until", node.get("until"))
    for i, op in enumerate(node.get("do") or []):
        what = f"do[{i}] {op.get('op')}"
        add(f"{what} when", op.get("when"))
        add(f"{what} key", op.get("key"))
        for f, t in (op.get("item") or {}).items():
            add(f"{what} item.{f}", t)
        for f, t in (op.get("fields") or {}).items():
            add(f"{what} fields.{f}", t)
    for i, trig in enumerate(node.get("triggers") or []):
        push = trig.get("push") or {}
        add(f"trigger[{i}] push each", push.get("each"), True)
        for f, t in (push.get("item") or {}).items():
            add(f"trigger[{i}] push item.{f}", t, True)
    return out


def _collection_checks(chart: ir.Chart, nodes: List[ir.Node]) -> List[Finding]:
    out: List[Finding] = []
    cols: Dict[str, dict] = (chart.meta or {}).get("collections") or {}
    read: Set[str] = set()

    def resolve(addr: object, where: str, nid: Optional[str], *,
                reading: bool = False) -> Optional[dict]:
        """The self collection an address names (None when it is not a
        self address or does not resolve; findings are added)."""
        try:
            target, name = _collections.parse_address(addr)
        except _collections.CollectionError as exc:
            out.append(Finding(ERROR, "unknown-collection", f"{where}: {exc}", nid))
            return None
        if target == "main" or target.startswith("side:"):
            out.append(Finding(ERROR, "side-runs-unavailable",
                               f"{where}: address {addr!r} names a side run; side runs are not "
                               f"available yet (docs/cflow-v2.md 2.9)", nid))
            return None
        if target == "parent":
            out.append(Finding(ERROR, "parent-outside-trigger",
                               f"{where}: address {addr!r}: parent: is only for triggers", nid))
            return None
        spec = cols.get(name)
        if spec is None:
            known = ", ".join(sorted(cols)) or "none declared"
            out.append(Finding(ERROR, "unknown-collection",
                               f"{where}: no collection {name!r} (known: {known})", nid))
            return None
        if reading:
            read.add(name)
        return spec

    def field_check(spec: dict, fields: Dict[str, str], where: str, nid: str, push: bool) -> None:
        for f in fields:
            if f == "status" and not push:
                continue
            if f not in spec["item"]:
                out.append(Finding(ERROR, "collection-field",
                                   f"{where}: {spec['name']} has no field {f!r} "
                                   f"(fields: {', '.join(spec['item'])})", nid))
            elif not push and f == spec.get("key"):
                out.append(Finding(ERROR, "collection-field",
                                   f"{where}: the key field {f!r} cannot be set", nid))
        if push:
            missing = [f for f, d in spec["item"].items() if not d["optional"] and f not in fields]
            if missing:
                out.append(Finding(ERROR, "collection-field",
                                   f"{where}: push to {spec['name']} is missing required "
                                   f"field(s) {', '.join(missing)}", nid))

    for n in nodes:
        for i, op in enumerate(n.get("do") or []):
            kind = op.get("op")
            where = f"node {n.id} do[{i}] {kind}"
            remote = _delegated_above(chart, n)
            if remote:
                out.append(Finding(ERROR, "effect-in-delegation",
                                   f"{where}: the node is inside a subtree delegated to "
                                   f"{', '.join(remote)}; put collection effects on the "
                                   f"delegation root", n.id))
            spec = resolve(op.get("addr"), where, n.id)
            if spec is None:
                continue
            if kind in ("set", "remove") and spec["kind"] != "queue":
                out.append(Finding(ERROR, "collection-op-kind",
                                   f"{where}: {spec['name']} is a log; {kind} is only for a queue",
                                   n.id))
                continue
            if kind == "consume":
                if spec["kind"] != "log":
                    out.append(Finding(ERROR, "collection-op-kind",
                                       f"{where}: {spec['name']} is a queue; consume is only for "
                                       f"a log", n.id))
                continue
            status = None
            if kind == "push":
                field_check(spec, op.get("item") or {}, where, n.id, True)
                if "status" in (op.get("item") or {}):
                    out.append(Finding(ERROR, "collection-field",
                                       f"{where}: push cannot set status (it starts at the "
                                       f"declared initial value)", n.id))
            elif kind == "set":
                fields = op.get("fields") or {}
                field_check(spec, fields, where, n.id, False)
                if "status" in fields:
                    st = spec.get("status")
                    is_lit, value = _literal(fields["status"])
                    if st is None:
                        out.append(Finding(ERROR, "undeclared-status",
                                           f"{where}: {spec['name']} declares no status", n.id))
                    elif is_lit and value not in st["values"]:
                        out.append(Finding(ERROR, "undeclared-status",
                                           f"{where}: status {value!r} is not one of "
                                           f"{', '.join(st['values'])}", n.id))
                    elif is_lit:
                        status = value
            allowed = _collections.access_ok(spec, kind, "driver", status)
            if kind == "set" and status is None and "status" in (op.get("fields") or {}) \
                    and spec.get("status"):
                # a computed status: denied only when no value is open to the driver
                allowed = allowed or any(_collections.access_ok(spec, "set", "driver", v)
                                         for v in spec["status"]["values"])
            if not allowed:
                rule = (f"set.{status}" if status is not None
                        and f"set.{status}" in spec["access"] else kind)
                out.append(Finding(ERROR, "do-access",
                                   f"{where}: access.{rule} of {spec['name']} excludes driver "
                                   f"(node effects run as the driver): "
                                   f"{spec['access'].get(rule) or 'nobody'}", n.id))

        remote = _delegated_above(chart, n)
        for i, trig in enumerate(n.get("triggers") or []):
            do = trig.get("do")
            where = f"node {n.id} trigger[{i}] {do}"
            if do in ("push", "enqueue-landing") and remote:
                out.append(Finding(ERROR, "trigger-in-delegation",
                                   f"{where}: the node is inside a subtree delegated to "
                                   f"{', '.join(remote)}; {do} triggers cannot run there", n.id))
            if do == "push":
                to = (trig.get("push") or {}).get("to")
                try:
                    target, _ = _collections.parse_address(to)
                except _collections.CollectionError as exc:
                    out.append(Finding(ERROR, "trigger-target", f"{where}: {exc}", n.id))
                    continue
                if target != "parent":
                    out.append(Finding(ERROR, "trigger-target",
                                       f"{where}: a trigger push goes to another run's collection; "
                                       f"to must be parent:<name>, got {to!r}", n.id))

        def read_in_delegation(what: str) -> None:
            # a nested delegated run's chart carries no collections, so a
            # read there is an expression error at run time
            out.append(Finding(ERROR, "collection-read-in-delegation",
                               f"node {n.id} {what}: the node is inside a subtree delegated to "
                               f"{', '.join(remote)}, whose run has no collections; read them on "
                               f"the delegation root or outside the subtree", n.id))

        if n.type == ir.T_WAIT:
            for addr in n.get("consume") or []:
                if remote:
                    read_in_delegation(f"wait consume {addr}")
                spec = resolve(addr, f"wait {n.id} consume", n.id)
                if spec is not None and spec["kind"] != "log":
                    out.append(Finding(ERROR, "collection-op-kind",
                                       f"wait {n.id} consume: {spec['name']} is a queue; consume "
                                       f"is only for a log", n.id))

        bound = {chart.node(a).get("as_name") for a in chart.ancestors(n.id)
                 if chart.node(a).type == ir.T_LOOP}
        for what, text, trig in _node_exprs(chart, n):
            try:
                calls = _expr.collection_calls(text)
                roots = _expr.references(text)
            except _expr.ExprError:
                continue
            if not trig:
                for root in sorted((roots & set(TRIGGER_ROOTS)) - bound):
                    out.append(Finding(ERROR, "facts-outside-trigger",
                                       f"node {n.id} {what}: ${root} is only set in a trigger "
                                       f"push expression", n.id))
            for fn, addr in calls:
                if trig:
                    # the daemon evaluates a trigger push with only $facts
                    # and $each (docs/cflow-v2.md 2.8); a collection read
                    # there fails at run time and the trigger is skipped
                    out.append(Finding(ERROR, "trigger-collection-read",
                                       f"node {n.id} {what}: {fn}() reads a collection; a "
                                       f"trigger push expression reads only $facts and $each",
                                       n.id))
                else:
                    if remote:
                        read_in_delegation(f"{what}: {fn}()")
                    if addr is not None:
                        resolve(addr, f"node {n.id} {what}", n.id, reading=True)

        if n.type == ir.T_TASK:
            for name in _COL_PROMPT_RE.findall(n.get("prompt") or ""):
                if remote:
                    read_in_delegation(f"prompt ${{col.{name}}}")
                if name not in cols:
                    out.append(Finding(ERROR, "unknown-collection",
                                       f"task {n.id} prompt: ${{col.{name}}} names no declared "
                                       f"collection", n.id))
                else:
                    read.add(name)

    spec_exprs: List[Tuple[str, str]] = []
    for key, text in ((chart.spec or {}).get("ensures") or {}).items():
        spec_exprs.append((f"ensures {key}", text))
    for name, mon in ((chart.spec or {}).get("monitors") or {}).items():
        if isinstance(mon, dict) and isinstance(mon.get("always"), str):
            spec_exprs.append((f"monitor {name}", mon["always"]))
    for what, text in spec_exprs:
        try:
            calls = _expr.collection_calls(text)
            roots = _expr.references(text)
        except _expr.ExprError:
            continue
        for root in sorted(roots & set(TRIGGER_ROOTS)):
            out.append(Finding(ERROR, "facts-outside-trigger",
                               f"{what}: ${root} is only set in a trigger push expression"))
        for _fn, addr in calls:
            if addr is not None:
                resolve(addr, what, None, reading=True)

    for name, spec in cols.items():
        for entry in (spec.get("lifecycle") or {}).get("reset_at") or []:
            if entry != "finish" and not any(m.id == entry or m.label == entry for m in nodes):
                out.append(Finding(ERROR, "unknown-collection",
                                   f"collection {name} lifecycle.reset_at: no node {entry!r} "
                                   f"(use a node id or label, or finish)"))
        if name in read and not (spec.get("access") or {}).get("push"):
            out.append(Finding(WARNING, "unpushable-collection",
                               f"collection {name} is read but its access.push is empty: "
                               f"nothing can ever add a record"))
    return out


def errors(chart: ir.Chart) -> List[Finding]:
    return [f for f in check(chart) if f.level == ERROR]


__all__ = ("Finding", "check", "errors", "cfg", "split_effect", "START", "END")
