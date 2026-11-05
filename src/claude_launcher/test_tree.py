"""The test tree, and the three run modes of the targeted gate.

``tools/changed_tests.py`` (T0 while working, T1 on the merge preview) runs in
one of three modes (claunch-ce872):

* ``partial``  -- every test of the modules the change selects. What the gate
  always did.
* ``sampling`` -- per module, its representative tests plus a seeded sample of
  the rest, at a rate set by the module's importance. When anything in that
  first pass fails, the gate runs a second pass over the whole of every module
  whose node failed, the nodes below it, and the nodes that depend on it.
* ``full``     -- ``pytest tests``, the slow tail included: the sweep's range.

Which mode runs is :func:`resolve_mode`: the command line, then
``CLAUNCH_TEST_MODE``, then ``full`` when ``CI`` is set, else ``sampling``.
So a local daemon session samples and a CI job runs everything without either
side being configured. T2 (the post-merge sweep) is not touched: it never
reads this module.

The tree
--------
A node names a set of test modules (glob patterns), an importance, its
representative tests, the nodes it depends on, and the node above it. The
declaration is ``tests/test_tree.yaml``::

    nodes:
      - name: cflow2
        modules: ["tests/test_cflow2_*.py"]
        importance: high
        smoke: ["tests/test_cflow2_engine.py::test_*"]
        depends: [beads]
        children:
          - name: cflow2-rail
            modules: ["tests/test_cflow2_rail*.py"]

A module belongs to the deepest declared node whose pattern matches it. A
module no declaration matches gets a derived node named by its file-name
prefix (``tests/test_mesh_wire.py`` -> ``mesh``), importance ``normal``, no
dependencies -- so the tree covers every test module whether or not anybody
wrote the file. A node without ``smoke`` takes the first test of each module,
in collection order, as that module's representative.

Importance sets the share of a module's non-representative tests the first
pass runs, against the base rate (``--sample-rate``, default
:data:`DEFAULT_RATE`): ``critical`` runs all of them, ``high`` twice the rate,
``normal`` the rate, ``low`` half of it. Every module runs at least one test.

The sample is a function of the seed and the node id only, so the same seed
over the same collection picks the same tests; the gate seeds with the tree
hash, which keeps one tree's verdict stable and moves the sample with every
edit.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SAMPLING = "sampling"
PARTIAL = "partial"
FULL = "full"
MODES = (SAMPLING, PARTIAL, FULL)

#: Environment variable that picks the mode when the command line does not.
MODE_ENV = "CLAUNCH_TEST_MODE"
#: Set (truthy) by CI services; selects ``full`` when nothing else is said.
CI_ENV = "CI"

#: Share of a ``normal`` module's non-representative tests the first pass runs.
DEFAULT_RATE = 0.25

IMPORTANCE = {"critical": None, "high": 2.0, "normal": 1.0, "low": 0.5}
DEFAULT_IMPORTANCE = "normal"

#: Where the declaration lives, relative to the repository root.
TREE_FILE = "tests/test_tree.yaml"

_FALSY = {"", "0", "false", "no", "off"}


def resolve_mode(cli: Optional[str], env: Mapping[str, str]) -> Tuple[str, str]:
    """``(mode, how)``: the mode this run uses and what chose it.

    ``how`` is one of ``cli``, ``env``, ``ci``, ``default``. A value of
    :data:`MODE_ENV` that names no mode is an error, not a fallback: a CI job
    that misspells ``full`` must not quietly sample.
    """
    if cli:
        if cli not in MODES:
            raise ValueError(f"unknown mode {cli!r} (one of {', '.join(MODES)})")
        return cli, "cli"
    named = (env.get(MODE_ENV) or "").strip().lower()
    if named:
        if named not in MODES:
            raise ValueError(
                f"{MODE_ENV}={env.get(MODE_ENV)!r} names no mode "
                f"(one of {', '.join(MODES)})"
            )
        return named, "env"
    if (env.get(CI_ENV) or "").strip().lower() not in _FALSY:
        return FULL, "ci"
    return SAMPLING, "default"


@dataclass(frozen=True)
class Node:
    name: str
    modules: Tuple[str, ...] = ()
    importance: str = DEFAULT_IMPORTANCE
    smoke: Tuple[str, ...] = ()
    depends: Tuple[str, ...] = ()
    parent: Optional[str] = None
    depth: int = 0
    derived: bool = False


@dataclass
class Tree:
    nodes: Dict[str, Node] = field(default_factory=dict)
    #: Declaration order, so the first of two equally deep matches wins.
    order: List[str] = field(default_factory=list)

    def add(self, node: Node) -> None:
        if node.name in self.nodes:
            raise ValueError(f"test tree: node {node.name!r} declared twice")
        if node.importance not in IMPORTANCE:
            raise ValueError(
                f"test tree: node {node.name!r} has importance "
                f"{node.importance!r} (one of {', '.join(IMPORTANCE)})"
            )
        self.nodes[node.name] = node
        self.order.append(node.name)

    def check(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends:
                if dep not in self.nodes:
                    raise ValueError(
                        f"test tree: node {node.name!r} depends on unknown "
                        f"node {dep!r}"
                    )

    def node_for(self, module: str) -> Node:
        """The deepest declared node matching ``module``, else a derived one."""
        rel = _posix(module)
        best: Optional[Node] = None
        for name in self.order:
            node = self.nodes[name]
            if node.derived:
                continue
            if any(fnmatch.fnmatchcase(rel, pat) for pat in node.modules):
                if best is None or node.depth > best.depth:
                    best = node
        if best is not None:
            return best
        name = derived_name(rel)
        node = self.nodes.get(name)
        if node is None:
            node = Node(name=name, derived=True)
            self.nodes[name] = node
            self.order.append(name)
        return node

    def children(self, name: str) -> List[str]:
        return [n for n in self.order if self.nodes[n].parent == name]

    def affected(self, names: Iterable[str]) -> List[str]:
        """``names``, every node below them, and every node depending on any.

        Transitive both ways: a dependent's dependents are affected too, and
        so are the descendants of a dependent.
        """
        seen: List[str] = []
        todo = list(names)
        while todo:
            name = todo.pop(0)
            if name in seen or name not in self.nodes:
                continue
            seen.append(name)
            todo.extend(self.children(name))
            todo.extend(
                n for n in self.order if name in self.nodes[n].depends
            )
        return seen


def derived_name(module: str) -> str:
    """``tests/test_mesh_wire.py`` -> ``mesh``; a module without a prefix keeps its stem."""
    stem = PurePosixPath(_posix(module)).stem
    if stem.startswith("test_"):
        stem = stem[len("test_"):]
    return stem.split("_", 1)[0] or stem


def _posix(path: str) -> str:
    return str(path).replace("\\", "/")


def parse_tree(doc: Optional[Mapping]) -> Tree:
    """A :class:`Tree` from the declaration's parsed YAML (None: no declaration)."""
    tree = Tree()
    if not doc:
        return tree
    if not isinstance(doc, Mapping) or not isinstance(doc.get("nodes", []), list):
        raise ValueError("test tree: the file must hold a mapping with a 'nodes' list")

    def walk(items: Sequence, parent: Optional[str], depth: int) -> None:
        for item in items:
            if not isinstance(item, Mapping) or not item.get("name"):
                raise ValueError(f"test tree: every node needs a name ({item!r})")
            tree.add(Node(
                name=str(item["name"]),
                modules=tuple(_posix(m) for m in item.get("modules") or ()),
                importance=str(item.get("importance") or DEFAULT_IMPORTANCE),
                smoke=tuple(_posix(s) for s in item.get("smoke") or ()),
                depends=tuple(str(d) for d in item.get("depends") or ()),
                parent=parent,
                depth=depth,
            ))
            walk(item.get("children") or (), str(item["name"]), depth + 1)

    walk(doc.get("nodes") or (), None, 0)
    tree.check()
    return tree


def load_tree(repo: Path) -> Tree:
    """The repository's declared tree; an empty one (all derived) without a file."""
    path = Path(repo) / TREE_FILE
    if not path.is_file():
        return Tree()
    import yaml  # the package already depends on it; imported here to keep this module light

    return parse_tree(yaml.safe_load(path.read_text(encoding="utf-8")))


def module_of(nodeid: str) -> str:
    return _posix(nodeid.split("::", 1)[0])


def _rank(seed: str, nodeid: str) -> str:
    return hashlib.sha1(f"{seed}\0{nodeid}".encode("utf-8")).hexdigest()


def module_rate(node: Node, rate: float) -> float:
    factor = IMPORTANCE[node.importance]
    return 1.0 if factor is None else min(1.0, max(0.0, rate * factor))


@dataclass
class Sample:
    """The first pass: which node ids run, and what they stand for."""

    picked: List[str]
    total: int
    #: module -> (node name, picked, collected)
    modules: Dict[str, Tuple[str, int, int]]

    @property
    def complete(self) -> bool:
        return len(self.picked) == self.total


def sample(
    nodeids: Sequence[str], tree: Tree, *, rate: float = DEFAULT_RATE, seed: str = "0"
) -> Sample:
    """Representatives plus a seeded, importance-weighted share of each module."""
    by_module: Dict[str, List[str]] = {}
    for nid in nodeids:
        by_module.setdefault(module_of(nid), []).append(nid)
    keep: set = set()
    report: Dict[str, Tuple[str, int, int]] = {}
    for module, ids in by_module.items():
        node = tree.node_for(module)
        smoke = [
            n for n in ids
            if any(fnmatch.fnmatchcase(n, pat) for pat in node.smoke)
        ] or ids[:1]
        rest = [n for n in ids if n not in smoke]
        k = math.ceil(len(rest) * module_rate(node, rate))
        chosen = sorted(rest, key=lambda n: _rank(seed, n))[:k]
        picked = set(smoke) | set(chosen)
        keep |= picked
        report[module] = (node.name, len(picked), len(ids))
    return Sample(
        picked=[n for n in nodeids if n in keep],
        total=len(nodeids),
        modules=report,
    )


def expansion(
    failed: Iterable[str], tree: Tree, modules: Iterable[str]
) -> Tuple[List[str], List[str]]:
    """``(nodes, modules)`` the second pass runs after ``failed`` node ids.

    ``modules`` is every test module the second pass may reach (the
    repository's, not only the first pass's), so a dependent node outside the
    change's selection is run too.
    """
    hit = []
    for nid in failed:
        name = tree.node_for(module_of(nid)).name
        if name not in hit:
            hit.append(name)
    nodes = tree.affected(hit)
    wanted = set(nodes)
    out = sorted({
        _posix(m) for m in modules if tree.node_for(m).name in wanted
    })
    # A failing module is always in its own expansion, even when the module
    # list given here does not hold it (a module outside tests/).
    for nid in failed:
        mod = module_of(nid)
        if mod not in out:
            out.append(mod)
    return nodes, sorted(out)


def repo_modules(repo: Path) -> List[str]:
    """Every ``tests/test_*.py`` of the checkout, repository-relative."""
    root = Path(repo) / "tests"
    if not root.is_dir():
        return []
    return sorted(
        p.relative_to(repo).as_posix() for p in root.rglob("test_*.py")
    )


def env_mode_note(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    return f"{MODE_ENV}={env.get(MODE_ENV, '')!r}, {CI_ENV}={env.get(CI_ENV, '')!r}"
