"""Which other files should be looked at alongside the ones this branch changed?

``tools/changed_tests.py`` answers "which *tests* does this change reach".
This file answers the question that sits next to it and that no test can:
"which *other sources* mirror the thing I just changed". This repository
carries contracts that exist as hand-kept copies -- the spawn / new-session
payload is written by five pieces of code across seven files (CLI flags,
two wizards, the web modal in two modes, the MCP tool, two API routes), and
the sentinel ``"-"`` for "no workflow" is spelled in four of them. A round
that fixes one copy and not the others lands green: the selected tests pin
each copy on its own, and the one live mismatch measured on 2026-09-11
(the web modal's spawn mode not sending ``workflow: "-"`` for an empty row,
``app.js`` ~5279, ``claunch-ozpf``) passed every test that existed. Three
earlier misses of the same shape took two or three commits each to converge
(``issue_text``, reasoning effort, form grouping -- ``claunch-3uef``
ASSESSMENT 1.2).

So: derive the surfaces from the repository and print them, so the review
step can *say* which ones were checked instead of relying on the author to
remember that a mirror exists. Three sources, unioned, each named on the
line it produces so a reader knows why a file is there:

``cochange:N``
    files that changed **in the same commit** as the changed file at least
    N times in the last ``--limit`` non-merge commits (default 800, the span
    the assessment measured). Commits touching more than ``--max-files``
    paths are skipped: a repo-wide rename is not evidence that two files are
    coupled. Measured on this checkout, ``cli_sessions.py`` co-changes with
    ``daemon/api.py`` 26 times, ``app.js`` 22, ``wizard.py`` 15 -- the mirror
    set, read out of history rather than remembered.
``guard:<group>``
    :data:`SURFACE_GROUPS` -- the mirrors that history cannot prove because
    they were *supposed* to change together and did not (that is the defect),
    plus the two pairs that are derived rather than listed: a canonical
    workflow and its project-layer copy, and a gate script and the workflow
    file that arms it.
``test``
    test modules whose source names the changed file, by the gate's own
    function for it (:func:`changed_tests.named_by`, rules 2c and 3a: a test
    that pins a file by string is a reader of it, and a contract change has
    to reach it; a dunder file's stem names nothing, so it has none).
``test:unselected``
    the same, for a module the gate's selection of this change
    (:func:`changed_tests.select`) does not contain. The case that produces
    it is a changed test module: rule 1 runs the module itself and not the
    tests that name it.

Each surface is then marked **touched** (also in this branch's change),
**gate** (a naming test the gate's selection contains, so it is listed for
the record and not counted) or **check** (a source mirror, or a naming test
the gate does not select -- nobody will run it for you). The last line is
the sentence the review step reports:

    related surfaces: N to check, M touched by this diff, T naming tests (the gate runs them)

The exit code is not a verdict: 0 means the list was produced, 2 that it
could not be (no repository). A surface left unchecked is a sentence in the
report ("N of M checked, the rest because ..."), not a red gate -- forcing
an edit to every mirror would make rounds touch files they have no reason to
touch, and the step's ``done_when`` is what stands behind the sentence.

    uv run --no-sync python tools/related_surfaces.py --base auto
    uv run --no-sync python tools/related_surfaces.py --paths src/claude_launcher/wizard.py
    uv run --no-sync python tools/related_surfaces.py --base auto --json

Standard library only, this checkout's ``src`` first: the gate rules of
``tests/test_gates_run_this_checkout.py`` apply to every ``tools/`` script
a workflow step names, and this one is named by ``improv-worker``'s review
step.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# A gate runs the tree it is checking (see tools/changed_tests.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CANNOT_TELL = 2

#: How far back co-change history is read, in non-merge commits. The
#: assessment that motivated this tool measured 800 and found 92 commits
#: touching three or more of the seven spawn-contract files.
DEFAULT_LIMIT = 800

#: A commit that touches more paths than this says nothing about coupling
#: (a rename across the tree, a formatter run, a squash of a whole branch).
DEFAULT_MAX_FILES = 30

#: Co-changes below this are noise: two files that met once in a commit are
#: not mirrors of each other. Three is the count at which the assessment's
#: "changed together in the same commit" cases all appear and the one-off
#: neighbours (a test file edited in passing) mostly do not.
DEFAULT_MIN_COCHANGE = 3

#: The mirrors history cannot be trusted to show, because the defect this
#: tool exists for is precisely a mirror that *did not* change together.
#: Each group is (name, files); a changed file in a group lists the rest.
#:
#: Keep the table to contracts that are actually copied by hand. The spawn
#: contract is the measured one (``claunch-3uef`` ASSESSMENT 1.2: eleven
#: surfaces, five payload writers, eight hand-mirrored duplications); the
#: cflow skills are the two prose descriptions of one parser, and a parser
#: change that reaches neither is the shape ``test_the_authoring_skill_only_
#: shows_yaml_the_parser_accepts`` was written after.
SURFACE_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "spawn-contract",
        (
            "src/claude_launcher/cli_sessions.py",
            "src/claude_launcher/wizard.py",
            "src/claude_launcher/web/static/app.js",
            "src/claude_launcher/mesh_mcp.py",
            "src/claude_launcher/daemon/api.py",
            "src/claude_launcher/daemon/onboard.py",
            "src/claude_launcher/daemon/beads.py",
            "src/claude_launcher/spawn.py",
        ),
    ),
    (
        "cflow-skills",
        (
            "src/claude_launcher/cflow/model.py",
            "src/claude_launcher/cflow/authoring.py",
            "src/claude_launcher/cflow/install.py",
        ),
    ),
)

#: The canonical workflow directory and the project layer that mirrors it
#: by file name (AGENTS.md: the project layer is canonical + grafted gate
#: fields, regenerated by tools/sync_project_layer.py).
WORKFLOW_LAYERS = ("src/claude_launcher/workflows/", ".claunch/workflows/")

#: Paths under these first components are not code and never surfaces
#: (the board is written by every session on the checkout).
NON_CODE_ENTRIES = frozenset({".beads"})

_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_changed_tests():
    """``tools/changed_tests.py`` by path -- for the base, the changed set,
    the naming rule and the selection, so this tool and the gate read the
    same change and agree on what the gate runs."""
    spec = importlib.util.spec_from_file_location(
        "changed_tests", Path(__file__).resolve().parent / "changed_tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise LookupError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _is_code(rel: str) -> bool:
    return bool(rel) and rel.split("/", 1)[0] not in NON_CODE_ENTRIES


def history(repo: Path, limit: int = DEFAULT_LIMIT, max_files: int = DEFAULT_MAX_FILES) -> List[frozenset]:
    """The file sets of the last ``limit`` non-merge commits, largest ones dropped."""
    out = _git(repo, "log", "--no-merges", f"-n{limit}", "--format=%H", "--name-only")
    commits: List[set] = []
    cur: Optional[set] = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if _HASH_RE.match(line):
            cur = set()
            commits.append(cur)
        elif cur is not None and _is_code(line):
            cur.add(line)
    return [frozenset(c) for c in commits if c and len(c) <= max_files]


def cochanged(commits: Iterable[frozenset], rel: str, min_count: int = DEFAULT_MIN_COCHANGE) -> Dict[str, int]:
    """``{other file: times it changed in the same commit as rel}``, at or above ``min_count``."""
    counter: Counter = Counter()
    for files in commits:
        if rel in files:
            for other in files:
                # Tests are the gate's axis (changed_tests.py selects them by
                # rule, not by history); listing every test edited alongside
                # a module would bury the source mirrors this tool is for.
                if other != rel and not other.startswith("tests/"):
                    counter[other] += 1
    return {f: n for f, n in counter.items() if n >= min_count}


def guarded(rel: str, groups=SURFACE_GROUPS) -> Dict[str, str]:
    """``{other file: group name}`` for every explicit or derived mirror of ``rel``."""
    out: Dict[str, str] = {}
    for name, files in groups:
        if rel in files:
            for other in files:
                if other != rel:
                    out[other] = name
    canon, layer = WORKFLOW_LAYERS
    if rel.startswith(canon) and rel.endswith((".yaml", ".yml")):
        out[layer + rel[len(canon):]] = "workflow-layers"
    elif rel.startswith(layer) and rel.endswith((".yaml", ".yml")):
        out[canon + rel[len(layer):]] = "workflow-layers"
    return out


def gate_arming_workflows(repo: Path, rel: str) -> List[str]:
    """Workflow files under the project layer whose gate fields name ``rel``.

    A ``tools/`` script and the ``verify:``/``check:``/``probe:`` line that
    runs it are one contract: renaming a flag in the script without touching
    the line that passes it is the mirror miss for gate scripts.
    """
    if not rel.startswith("tools/"):
        return []
    out = []
    for path in sorted((repo / WORKFLOW_LAYERS[1]).glob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rel in text:
            out.append(WORKFLOW_LAYERS[1] + path.name)
    return out


def surfaces(
    repo: Path,
    changed: List[str],
    *,
    commits: Optional[List[frozenset]] = None,
    min_cochange: int = DEFAULT_MIN_COCHANGE,
    groups=SURFACE_GROUPS,
    changed_tests=None,
) -> List[dict]:
    """Every related surface of ``changed``, one dict per file:
    ``{"path", "reasons": [..], "touched": bool, "for": [changed files]}``."""
    if commits is None:
        commits = history(repo)
    changed_set = set(changed)
    selected = set()
    if changed_tests is not None:
        selected = set(changed_tests.select(repo, [rel for rel in changed if _is_code(rel)]))
    found: Dict[str, dict] = {}

    def add(path: str, reason: str, source: str) -> None:
        if not path or path == source:
            return
        if not (repo / path).exists():
            return  # a co-change partner that has since been deleted is not a surface
        entry = found.setdefault(path, {"path": path, "reasons": [], "for": []})
        kind, _, value = reason.partition(":")
        if kind == "cochange":
            # one count per surface: the strongest partner among the changed
            # files, not one line per changed file
            existing = [r for r in entry["reasons"] if r.startswith("cochange:")]
            if existing and int(existing[0].split(":")[1]) >= int(value):
                reason = None
            elif existing:
                entry["reasons"].remove(existing[0])
        if reason is not None and reason not in entry["reasons"]:
            entry["reasons"].append(reason)
        if source not in entry["for"]:
            entry["for"].append(source)

    for rel in changed:
        if not _is_code(rel):
            continue
        for other, n in sorted(cochanged(commits, rel, min_cochange).items()):
            add(other, f"cochange:{n}", rel)
        for other, group in sorted(guarded(rel, groups).items()):
            add(other, f"guard:{group}", rel)
        for wf in gate_arming_workflows(repo, rel):
            add(wf, "guard:gate-arming", rel)
        if changed_tests is not None:
            for test in changed_tests.named_by(repo, rel):
                add(test, "test" if test in selected else "test:unselected", rel)

    out = []
    for path in sorted(found):
        entry = found[path]
        entry["touched"] = path in changed_set
        entry["reasons"].sort(key=_reason_rank)
        out.append(entry)
    return out


def _reason_rank(reason: str) -> tuple:
    kind = reason.split(":", 1)[0]
    return ({"guard": 0, "cochange": 1, "test": 2}.get(kind, 3), reason)


def is_naming_test(surface: dict) -> bool:
    """A test module that names the changed file and that the gate's
    selection contains -- listed, but not counted among the surfaces to
    check, because the gate runs it. One the selection does not contain is
    ``test:unselected`` and is counted."""
    return surface["reasons"] == ["test"]


def summary_line(found: List[dict]) -> str:
    sources = [s for s in found if not is_naming_test(s)]
    to_check = [s for s in sources if not s["touched"]]
    touched = [s for s in sources if s["touched"]]
    tests = [s for s in found if is_naming_test(s)]
    return (
        f"related surfaces: {len(to_check)} to check, {len(touched)} touched by this diff, "
        f"{len(tests)} naming tests (the gate runs them)"
    )


def render(changed: List[str], found: List[dict], base_note: str) -> str:
    lines = [f"changed paths ({len(changed)}) {base_note}".rstrip()]
    lines.extend(f"  {p}" for p in changed)
    lines.append("")
    if not found:
        lines.append("related surfaces: none derived (no co-change partner, guard group, or naming test)")
    for s in found:
        mark = "touched" if s["touched"] else ("gate   " if is_naming_test(s) else "CHECK  ")
        lines.append(f"{mark}  {s['path']}  [{', '.join(s['reasons'])}]  for {', '.join(s['for'])}")
    lines.append("")
    lines.append(summary_line(found))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="related_surfaces",
        description=(
            "list the files that mirror the ones this branch changed "
            "(co-change history, explicit guard groups, tests naming the file)"
        ),
    )
    ap.add_argument("--repo", default=".", help="checkout to read (default: cwd)")
    ap.add_argument("--base", default="master", help="ref to diff against; 'auto' reads the upstream")
    ap.add_argument("--paths", nargs="*", help="changed paths to use instead of asking git")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="non-merge commits of history to read")
    ap.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="skip commits touching more paths than this")
    ap.add_argument("--min-cochange", type=int, default=DEFAULT_MIN_COCHANGE, help="co-changes needed to count as a partner")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    repo = Path(args.repo).resolve()
    try:
        top = Path(_git(repo, "rev-parse", "--show-toplevel"))
    except (LookupError, OSError):
        print(f"cannot tell: {repo} is not inside a git repository")
        return CANNOT_TELL
    repo = top

    ct = _load_changed_tests()
    if args.paths:
        changed = sorted({p.replace("\\", "/") for p in args.paths})
        base_note = "(given)"
    else:
        base, how = ct.resolve_base(repo, args.base)
        try:
            changed = ct.changed_paths(repo, base)
        except LookupError as exc:
            print(f"cannot tell: {exc}")
            return CANNOT_TELL
        base_note = f"vs {base} ({how})"

    commits = history(repo, args.limit, args.max_files)
    found = surfaces(repo, changed, commits=commits, min_cochange=args.min_cochange, changed_tests=ct)

    if args.json:
        print(json.dumps({"changed": changed, "base": base_note, "surfaces": found, "summary": summary_line(found)}, ensure_ascii=False, indent=2))
    else:
        print(render(changed, found, base_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
