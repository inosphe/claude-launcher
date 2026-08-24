"""Semantic-dependency check for a batch merge: who deleted what the other calls.

``git merge-tree`` reporting no conflict is a statement about **text**, not
about meaning. Two branches can touch different lines of the same file — or
different files entirely — and still be incompatible: one deletes a helper,
the other adds calls to it. Git merges that silently and the failure surfaces
at run time as a ``NameError``, far from the review that approved it.

That is not hypothetical here. Two branches in this repository did exactly it:
one removed ``_wait_idle`` from ``tests/test_session_queued_api.py`` while the
other added three calls to it in the same file. No text conflict; the merge
would have been clean and broken. It was caught by a person reading a diff,
which is the part that does not scale — **the danger of this failure is that a
clean merge leaves nothing for a reviewer to look at.**

So the check is mechanical. For each side, relative to the merge base:

* **deleted definitions** — symbols whose defining line the side removed and
  did not put back (a move or a rename shows up as both, and is not a loss)
* **added calls** — symbols the side newly calls, minus the ones it defines
  itself (a side that brings its own helper depends on nobody)

The risk set is the intersection, taken **both ways**: what A deleted and B
calls, and what B deleted and A calls. Empty means this pair is clear of the
one failure a clean merge hides.

The intersection is on **(symbol, file)**, not on the symbol alone, because a
top-level name belongs to its module: a helper of the same name in another
test file resolves nothing. The first version of this check asked the whole
tree instead and cleared the very accident it was written for — ``_wait_idle``
is defined in a second test module, and tree-wide that reads as "still
defined". A checker that clears the case it exists for is worse than none.

A candidate is then **confirmed** against the deleter's own copy of that file:
a second definition surviving there means the merged file still has one, and
the call resolves. Only what survives that check is reported, because a
checker that cries wolf is one people learn to skip — and this one exists
precisely for the moments when nobody wants to look.

What this does not do: it does not follow imports across modules, resolve
attributes (``obj.method()``), understand conditional definitions, or
type-check. Deleting a name in one module that another module imports is a
real break this will not see — the answer here is deliberately narrow so that
what it *does* report is worth acting on. It answers one question — *does one
side delete a top-level name the other side started calling in the same file*
— and answers it cheaply enough to run on every pair in a batch. The preview
sweep (merge, then run the suite) remains the authority; this runs before it,
costs seconds, and names the pair, the file and the symbol.

Its own output is **ASCII only**, for the reason :mod:`wizard` gives: this
prints into a Windows console as often as a Unix terminal, and one character
outside the code page does not garble a line, it raises and takes the whole
report down. That happened on the first run of this module.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Files worth reading. Extensions rather than languages: the extractors below
#: are regex-shaped, and a language we cannot extract from is worse than one
#: we skip — a half-parsed file produces confident nonsense.
SUFFIXES = (".py", ".js", ".mjs")

#: A definition, per language family. Each pattern captures the symbol name.
#: Anchored at the start (with optional indentation) so a name mentioned in a
#: string or a comment is not mistaken for a definition.
_DEF_PATTERNS = (
    # Python: def foo / async def foo / class Foo
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*[:\(]"),
    # JS: function foo / async function foo
    re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    # JS/py module-level binding: const foo = / let foo = / FOO = (
    re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
)

#: A call site. Deliberately narrow: NAME( with the name not preceded by a dot
#: (``obj.method()`` is an attribute, and we do not resolve those) and not
#: itself a definition (handled by the caller).
_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")

#: Names that match the call shape but are never a project symbol. Keywords
#: first (``if (`` is not a call), then the builtins a diff is full of.
_NOISE = frozenset(
    """
    if elif while for switch catch return yield await async assert with
    del lambda raise except finally import from print
    function class def const let var new typeof instanceof delete void in of
    str int float bool list dict set tuple bytes len range enumerate zip
    open sorted reversed min max sum abs any all isinstance issubclass getattr
    setattr hasattr repr type super property staticmethod classmethod
    String Number Boolean Object Array JSON Math Date Promise Set Map WeakMap
    Error TypeError ValueError RuntimeError KeyError IndexError Exception
    parseInt parseFloat encodeURIComponent decodeURIComponent setTimeout
    setInterval clearTimeout clearInterval fetch require console alert confirm
    """.split()
)


@dataclass
class Side:
    """One branch of the pair, as the diff against the merge base describes it."""

    ref: str
    #: Definitions this side removed and did not restore.
    deleted_defs: Dict[str, List[str]] = field(default_factory=dict)
    #: Symbols this side newly calls, other than ones it defines itself.
    added_calls: Dict[str, List[str]] = field(default_factory=dict)
    #: Every definition this side's diff adds — a move is a delete plus this.
    added_defs: Set[str] = field(default_factory=set)


@dataclass
class Finding:
    """One symbol a clean merge would leave called but undefined."""

    symbol: str
    deleted_by: str
    deleted_in: List[str]
    called_by: str
    called_in: List[str]

    def describe(self) -> str:
        return (
            f"{self.symbol}: deleted by {self.deleted_by} "
            f"({', '.join(self.deleted_in)}) but called by {self.called_by} "
            f"({', '.join(self.called_in)})"
        )


class MergeCheckError(Exception):
    """Raised when git cannot answer — an unknown ref, no common ancestor."""


def _git(args: Sequence[str], cwd: Optional[str] = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise MergeCheckError(
            f"git {' '.join(args)} failed: {(proc.stderr or '').strip()}"
        )
    return proc.stdout


def merge_base(a: str, b: str, cwd: Optional[str] = None) -> str:
    """The commit both refs descend from — what each diff is taken against.

    Diffing the two refs against *each other* would read one side's additions
    as the other side's deletions, which is exactly the confusion this check
    exists to avoid.
    """
    out = _git(["merge-base", a, b], cwd=cwd).strip()
    if not out:
        raise MergeCheckError(f"{a} and {b} have no common ancestor")
    return out


def _wanted(path: str) -> bool:
    return path.endswith(SUFFIXES)


def _defs_in(line: str) -> Set[str]:
    found = set()
    for pattern in _DEF_PATTERNS:
        m = pattern.match(line)
        if m:
            found.add(m.group(1))
    return found


def _calls_in(line: str) -> Set[str]:
    # A definition line also matches the call shape (`def foo(`), so the name
    # being DEFINED is removed -- but not the rest of the line. A one-line JS
    # function carries its body: `function other() { return fmt(2); }` defines
    # `other` and calls `fmt`, and discarding the whole line loses the call.
    return {
        n for n in _CALL_RE.findall(line)
        if n not in _NOISE and n not in _defs_in(line)
    }


def read_side(ref: str, base: str, cwd: Optional[str] = None) -> Side:
    """What ``ref`` deleted and what it started calling, since ``base``.

    Parsed from the unified diff rather than from the two trees, because the
    question is about the *change*: a symbol the side never touched is not its
    business, and comparing whole files would drown the answer in the other
    side's work.
    """
    side = Side(ref=ref)
    diff = _git(["diff", "--unified=0", f"{base}...{ref}"], cwd=cwd)
    path = ""
    removed_defs: Dict[str, Set[str]] = {}
    added_calls: Dict[str, Set[str]] = {}
    added_defs: Dict[str, Set[str]] = {}
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not path or not _wanted(path):
            continue
        if line.startswith("-") and not line.startswith("---"):
            for name in _defs_in(line[1:]):
                removed_defs.setdefault(name, set()).add(path)
        elif line.startswith("+") and not line.startswith("+++"):
            body = line[1:]
            for name in _defs_in(body):
                added_defs.setdefault(name, set()).add(path)
                side.added_defs.add(name)
            for name in _calls_in(body):
                added_calls.setdefault(name, set()).add(path)

    # A definition that comes back is a move or a rewording, not a loss -- but
    # only where it comes back. Moving a helper from one module to another is
    # a loss for every caller left behind in the first one, so this pairs the
    # name with the FILE, like everything else here. Read tree-wide it hides
    # exactly the accident the check exists for.
    side.deleted_defs = {
        n: sorted(paths - added_defs.get(n, set()))
        for n, paths in removed_defs.items()
        if paths - added_defs.get(n, set())
    }
    # Likewise: a side that adds both the call and the callee in one file owes
    # nothing to the other branch for that file.
    side.added_calls = {
        n: sorted(paths - added_defs.get(n, set()))
        for n, paths in added_calls.items()
        if paths - added_defs.get(n, set())
    }
    return side


def _defines_in_file(
    ref: str, path: str, symbol: str, cwd: Optional[str] = None
) -> bool:
    """Whether ``ref``'s copy of ``path`` still defines ``symbol``.

    The confirmation step, and it is **per file on purpose**. A top-level name
    in Python belongs to its module, and the dashboard's ``app.js`` is one
    script: a helper of the same name in another file resolves nothing. The
    first version of this check asked the whole tree and cleared the very
    accident it was written for — ``_wait_idle`` exists in a second test
    module, and tree-wide that reads as "still defined".

    Asked of the **deleter**, because that is what the merge keeps: the side
    that removed the definition contributes the file without it, and the other
    side is only adding calls. A second definition surviving in the deleter's
    own copy is the one case where the removal costs nothing.
    """
    try:
        body = _git(["show", f"{ref}:{path}"], cwd=cwd)
    except MergeCheckError:
        # The file does not exist at that ref — deleted outright, or renamed.
        # Either way it defines nothing, which is the answer the caller wants.
        return False
    return any(symbol in _defs_in(line) for line in body.splitlines())


def check_pair(
    a: str, b: str, *, cwd: Optional[str] = None, base: Optional[str] = None
) -> List[Finding]:
    """The symbols a clean merge of ``a`` and ``b`` would leave undefined.

    Both directions, because the accident has no preferred side: whoever
    lands first makes the other one the deleter.
    """
    base = base or merge_base(a, b, cwd=cwd)
    side_a = read_side(a, base, cwd=cwd)
    side_b = read_side(b, base, cwd=cwd)

    findings: List[Finding] = []
    for deleter, caller in ((side_a, side_b), (side_b, side_a)):
        for symbol, deleted_in in sorted(deleter.deleted_defs.items()):
            called_in = caller.added_calls.get(symbol)
            if not called_in:
                continue
            # The intersection is on (symbol, FILE), not on the symbol alone:
            # a name is only "the same name" inside one module. The pair that
            # matters is a file this side stripped the definition from and the
            # other side added calls to.
            hits = sorted(set(deleted_in) & set(called_in))
            if not hits:
                continue
            # ...and only where the deleter's own copy has no second
            # definition left to carry the merged file.
            hits = [p for p in hits if not _defines_in_file(deleter.ref, p, symbol, cwd=cwd)]
            if not hits:
                continue
            findings.append(
                Finding(
                    symbol=symbol,
                    deleted_by=deleter.ref,
                    deleted_in=hits,
                    called_by=caller.ref,
                    called_in=hits,
                )
            )
    return findings


def check_batch(
    refs: Sequence[str], *, cwd: Optional[str] = None
) -> List[Tuple[str, str, List[Finding]]]:
    """Every pair in a batch, in order. The batch is the unit that lands."""
    out = []
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            out.append((a, b, check_pair(a, b, cwd=cwd)))
    return out


def report(results: Iterable[Tuple[str, str, List[Finding]]]) -> Tuple[str, int]:
    """Render the results, and the exit code a gate should use.

    Pairs that are clear are printed too. A checker that prints nothing when
    it passes is indistinguishable from one that did not run, and this is
    meant to be pasted into a review as evidence.
    """
    lines: List[str] = []
    total = 0
    for a, b, findings in results:
        if findings:
            total += len(findings)
            lines.append(f"RISK  {a} <-> {b}")
            for f in findings:
                lines.append(f"        {f.describe()}")
        else:
            lines.append(f"ok    {a} <-> {b}")
    if total:
        lines.append("")
        lines.append(
            f"{total} symbol(s) would be called but undefined after a clean "
            "merge. A text-conflict check cannot see these."
        )
    else:
        lines.append("")
        lines.append("no symbol is deleted by one side and called by another.")
    return "\n".join(lines), (1 if total else 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="claunch-mergecheck",
        description=(
            "Find symbols one branch deletes and another starts calling: "
            "the breakage a clean text merge hides."
        ),
    )
    parser.add_argument("refs", nargs="+", help="two or more branches/commits")
    parser.add_argument("-C", dest="cwd", default=None, help="repository path")
    args = parser.parse_args(argv)
    if len(args.refs) < 2:
        parser.error("give at least two refs: the check is about a pair")
    try:
        results = check_batch(args.refs, cwd=args.cwd)
    except MergeCheckError as exc:
        print(str(exc))
        return 2
    text, code = report(results)
    print(text)
    return code


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
