"""``tools/related_surfaces.py``: the files that mirror the ones a branch changed.

The gate (``changed_tests.py``) answers which tests a change reaches. This
tool answers the question next to it -- which *other sources* copy the same
contract by hand -- because the one live mismatch measured on 2026-09-11
(the web modal's spawn mode not sending ``workflow: "-"``, ``claunch-ozpf``)
passed every test that existed. The cases below pin the three sources
(co-change history, guard groups, tests naming the file), the touched /
to-check partition the review step reports, and the one evidence criterion
the issue set: a change to one spawn-contract file lists ``app.js``.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "related_surfaces.py"


def _load():
    spec = importlib.util.spec_from_file_location("related_surfaces", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _write(repo: Path, rel: str, text: str = "x = 1\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, msg: str, files: dict) -> None:
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _build_repo(repo: Path) -> None:
    """History shaped for the three rules.

    * ``a.py`` and ``b.py`` change together in three commits after the base
      (four with it: a partner), ``a.py`` and ``c.py`` once after it (two:
      noise), and one 40-file commit carries ``a.py`` with ``z.py`` (a mass
      edit, not coupling -- with it skipped, ``z.py`` only shares the base).
    * ``tests/test_a_guard.py`` names ``a.py`` by string and never imports it.
    * a canonical workflow and its project-layer copy share a name.
    * ``tools/gate.py`` is named by a project-layer ``verify:`` line.
    """
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    _commit(repo, "base", {
        "src/pkg/a.py": "x = 1\n",
        "src/pkg/b.py": "x = 1\n",
        "src/pkg/c.py": "x = 1\n",
        "src/pkg/g.py": "x = 1\n",
        "src/pkg/z.py": "x = 1\n",
        "tests/test_a_guard.py": "def test_it():\n    assert 'a.py'\n",
        "tests/test_other.py": "def test_it():\n    assert True\n",
        "src/claude_launcher/workflows/flow-one.yaml": "name: flow-one\n",
        ".claunch/workflows/flow-one.yaml": "name: flow-one\nverify: 'uv run --no-sync python tools/gate.py'\n",
        "tools/gate.py": "x = 1\n",
    })
    for i in range(3):
        _commit(repo, f"ab {i}", {"src/pkg/a.py": f"x = {i + 2}\n", "src/pkg/b.py": f"x = {i + 2}\n"})
    _commit(repo, "ac", {"src/pkg/a.py": "x = 9\n", "src/pkg/c.py": "x = 9\n"})
    mass = {f"src/pkg/m{i}.py": "x = 1\n" for i in range(38)}
    mass.update({"src/pkg/a.py": "x = 10\n", "src/pkg/z.py": "x = 10\n"})
    _commit(repo, "mass", mass)
    _git(repo, "branch", "-M", "master")
    _git(repo, "checkout", "-q", "-b", "feature")


@pytest.fixture
def repo(tmp_path, repo_template) -> Path:
    return repo_template("related-surfaces", _build_repo, tmp_path / "repo")


def _ct():
    spec = importlib.util.spec_from_file_location("changed_tests", ROOT / "tools" / "changed_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _surfaces(repo: Path, changed, **kw):
    return rs.surfaces(repo, list(changed), changed_tests=_ct(), groups=kw.pop("groups", ()), **kw)


def _by_path(found):
    return {s["path"]: s for s in found}


def test_a_file_that_changed_together_repeatedly_is_a_partner_and_rarely_is_not(repo):
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"]))
    assert "cochange:4" in found["src/pkg/b.py"]["reasons"]
    assert "src/pkg/c.py" not in found


def test_a_mass_commit_is_not_evidence_of_coupling(repo):
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"]))
    assert "src/pkg/z.py" not in found
    # lowering the ceiling past that commit's size lets it count
    commits = rs.history(repo, max_files=100)
    assert rs.cochanged(commits, "src/pkg/a.py", min_count=1)["src/pkg/z.py"] == 2


def test_the_threshold_is_a_parameter(repo):
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"], min_cochange=2))
    assert "cochange:2" in found["src/pkg/c.py"]["reasons"]


def test_a_guard_group_lists_the_other_members_regardless_of_history(repo):
    groups = (("grp", ("src/pkg/a.py", "src/pkg/g.py")),)
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"], groups=groups))
    assert found["src/pkg/g.py"]["reasons"] == ["guard:grp"]
    # a member that no longer exists is not a surface
    groups = (("grp", ("src/pkg/a.py", "src/pkg/gone.py")),)
    assert "src/pkg/gone.py" not in _by_path(_surfaces(repo, ["src/pkg/a.py"], groups=groups))


def test_a_canonical_workflow_and_its_project_layer_copy_mirror_each_other(repo):
    found = _by_path(_surfaces(repo, ["src/claude_launcher/workflows/flow-one.yaml"]))
    assert found[".claunch/workflows/flow-one.yaml"]["reasons"] == ["guard:workflow-layers"]
    found = _by_path(_surfaces(repo, [".claunch/workflows/flow-one.yaml"]))
    assert found["src/claude_launcher/workflows/flow-one.yaml"]["reasons"] == ["guard:workflow-layers"]


def test_a_gate_script_lists_the_workflow_that_arms_it(repo):
    found = _by_path(_surfaces(repo, ["tools/gate.py"]))
    assert found[".claunch/workflows/flow-one.yaml"]["reasons"] == ["guard:gate-arming"]


def test_a_test_that_names_the_file_is_a_surface_and_one_that_does_not_is_not(repo):
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"]))
    assert found["tests/test_a_guard.py"]["reasons"] == ["test"]
    assert "tests/test_other.py" not in found


def _seed_on_base(repo: Path, files: dict) -> None:
    """Put files in the base commit, so only what a case writes afterwards is
    the branch's change (a test module written by the round is selected by
    rule 1 whatever it contains)."""
    _git(repo, "checkout", "-q", "master")
    _commit(repo, "seed", files)
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "master")


#: A diff of the shape ``claunch-wt12o.1.2`` measured: a changed test module
#: that another test names, and a ``__main__.py`` that tests mention.
_TEST_MODULE_AND_DUNDER = {
    "tests/test_edited.py": "def test_it():\n    assert True\n",
    "tests/test_reads_edited.py": '"""Where ``test_edited`` drives the app, this drives the CLI."""\n',
    "tests/test_node_id.py": 'NODE = "tests/test_edited.py::test_it"\n',
    "src/pkg/__main__.py": "x = 1\n",
    "tests/test_says_main.py": 'if __name__ == "__main__":\n    pass\n',
}


def _edit_test_module_and_dunder(repo: Path) -> list:
    _seed_on_base(repo, _TEST_MODULE_AND_DUNDER)
    _write(repo, "tests/test_edited.py", "def test_it():\n    assert 1\n")
    _write(repo, "src/pkg/__main__.py", "x = 2\n")
    return ["src/pkg/__main__.py", "tests/test_edited.py"]


def test_a_test_naming_a_changed_test_module_is_to_check_because_the_gate_runs_only_the_module(repo):
    """Rule 1 selects a changed test module and stops: the tests that name it
    are not in the gate's selection, so they are not "the gate runs them"."""
    changed = _edit_test_module_and_dunder(repo)
    found = _surfaces(repo, changed)
    by = _by_path(found)
    assert by["tests/test_reads_edited.py"]["reasons"] == ["test:unselected"]
    assert by["tests/test_node_id.py"]["reasons"] == ["test:unselected"]
    assert not rs.is_naming_test(by["tests/test_node_id.py"])
    assert rs.summary_line(found) == (
        "related surfaces: 2 to check, 0 touched by this diff, 0 naming tests (the gate runs them)"
    )


def test_a_test_naming_a_longer_module_does_not_name_the_changed_one(repo):
    """``test_edited`` is a substring of ``test_edited_v2``: a test that names
    only the longer module is not a reader of the changed one and is not a
    row. On ``8b57829c``'s change to ``tests/test_mesh.py`` 12 of 15
    ``test:unselected`` rows were that (``claunch-6w2az``)."""
    _seed_on_base(repo, {"tests/test_names_v2.py": 'NODE = "tests/test_edited_v2.py::test_it"\n'})
    changed = _edit_test_module_and_dunder(repo)
    by = _by_path(_surfaces(repo, changed))
    assert "tests/test_names_v2.py" not in by
    assert by["tests/test_reads_edited.py"]["reasons"] == ["test:unselected"]
    assert by["tests/test_node_id.py"]["reasons"] == ["test:unselected"]


def test_a_dunder_file_has_no_naming_tests_because_its_stem_names_nothing(repo):
    """``__main__`` in a test is ``if __name__ == "__main__"``, not a reference
    to ``src/pkg/__main__.py`` -- the gate's rule 2c does not search for it,
    and neither does this tool."""
    changed = _edit_test_module_and_dunder(repo)
    assert "tests/test_says_main.py" not in _by_path(_surfaces(repo, changed))


def test_a_naming_test_the_gate_selects_by_another_rule_is_still_gate(repo):
    """``gate`` means the gate's selection contains the module, whatever rule
    put it there: here rule 2b selects the reader because it imports a
    changed module."""
    _seed_on_base(repo, {
        "tests/test_edited.py": "def test_it():\n    assert True\n",
        "tests/test_reads_edited.py": "from pkg import b\n# see test_edited\n",
    })
    _write(repo, "tests/test_edited.py", "def test_it():\n    assert 1\n")
    _write(repo, "src/pkg/b.py", "x = 50\n")
    found = _by_path(_surfaces(repo, ["src/pkg/b.py", "tests/test_edited.py"]))
    assert found["tests/test_reads_edited.py"]["reasons"] == ["test"]


@pytest.mark.parametrize(
    "changed",
    [
        ["src/pkg/a.py"],
        ["tools/gate.py"],
        [".claunch/workflows/flow-one.yaml"],
        ["tests/test_edited.py"],
        ["src/pkg/__main__.py"],
        ["src/pkg/__main__.py", "tests/test_edited.py", "src/pkg/a.py", "tools/gate.py"],
    ],
)
def test_every_gate_row_is_in_the_gates_selection(repo, changed):
    """The property ``claunch-wt12o.1.2`` asked for, across every kind of
    changed path the gate's rules tell apart (source, tools script, non-python,
    test module, dunder): no row says "the gate runs it" unless
    :func:`changed_tests.select` contains it."""
    _edit_test_module_and_dunder(repo)
    ct = _ct()
    found = rs.surfaces(repo, changed, changed_tests=ct, groups=())
    gate = {s["path"] for s in found if rs.is_naming_test(s)}
    assert gate <= set(ct.select(repo, changed))


def _list_selection(stdout: str) -> set:
    """The modules ``changed_tests.py --list`` prints under its "selected" line."""
    lines = stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if "test module(s) selected" in line)
    out = set()
    for line in lines[start + 1:]:
        if not line.startswith("  tests/"):
            break
        out.add(line.strip())
    return out


def test_the_scripts_gate_rows_are_a_subset_of_what_changed_tests_list_selects(repo):
    """The completion criterion of ``claunch-wt12o.1.2``, end to end: both
    scripts read the same branch change from git, and every ``gate`` row
    ``related_surfaces.py`` prints is a module ``changed_tests.py --list``
    selects."""
    _edit_test_module_and_dunder(repo)
    listed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "changed_tests.py"), "--repo", str(repo), "--list"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    selected = _list_selection(listed.stdout)
    assert "tests/test_edited.py" in selected
    out = _run("--repo", str(repo), cwd=repo)
    assert out.returncode == 0, out.stderr
    rows = [line.split() for line in out.stdout.splitlines() if line.startswith(("gate", "CHECK", "touched"))]
    gate = {row[1] for row in rows if row[0] == "gate"}
    assert gate <= selected, gate - selected
    check = {row[1] for row in rows if row[0] == "CHECK"}
    assert {"tests/test_reads_edited.py", "tests/test_node_id.py"} <= check
    assert "tests/test_says_main.py" not in {row[1] for row in rows}


def test_tests_edited_alongside_a_module_are_not_cochange_partners(repo):
    _commit(repo, "with test", {"src/pkg/b.py": "x = 20\n", "tests/test_other.py": "def test_it():\n    assert 1\n"})
    _commit(repo, "with test 2", {"src/pkg/b.py": "x = 21\n", "tests/test_other.py": "def test_it():\n    assert 2\n"})
    _commit(repo, "with test 3", {"src/pkg/b.py": "x = 22\n", "tests/test_other.py": "def test_it():\n    assert 3\n"})
    found = _by_path(_surfaces(repo, ["src/pkg/b.py"]))
    assert "tests/test_other.py" not in found


def test_a_surface_also_in_the_change_is_marked_touched_and_the_summary_counts_both(repo):
    found = _surfaces(repo, ["src/pkg/a.py", "src/pkg/b.py"])
    by = _by_path(found)
    assert by["src/pkg/b.py"]["touched"] is True
    assert by["src/pkg/a.py"]["touched"] is True   # b's partner is a
    assert by["tests/test_a_guard.py"]["touched"] is False
    assert rs.summary_line(found) == (
        "related surfaces: 0 to check, 2 touched by this diff, 1 naming tests (the gate runs them)"
    )


def test_the_reasons_are_ordered_guard_then_history_then_tests(repo):
    groups = (("grp", ("src/pkg/a.py", "src/pkg/b.py")),)
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"], groups=groups))
    assert found["src/pkg/b.py"]["reasons"] == ["guard:grp", "cochange:4"]


def test_two_changed_files_sharing_a_partner_yield_one_count_the_larger(repo):
    _commit(repo, "bc", {"src/pkg/b.py": "x = 40\n", "src/pkg/c.py": "x = 40\n"})
    _commit(repo, "bc 2", {"src/pkg/b.py": "x = 41\n", "src/pkg/c.py": "x = 41\n"})
    # a<->b is 4 (base + three), c<->b is 3 (base + two): one line, the larger
    found = _by_path(_surfaces(repo, ["src/pkg/a.py", "src/pkg/c.py"]))
    assert found["src/pkg/b.py"]["reasons"] == ["cochange:4"]
    assert found["src/pkg/b.py"]["for"] == ["src/pkg/a.py", "src/pkg/c.py"]


def test_the_board_is_never_a_surface(repo):
    for i in range(3):
        _commit(repo, f"board {i}", {"src/pkg/a.py": f"x = {30 + i}\n", ".beads/issues.jsonl": f"{i}\n"})
    assert ".beads/issues.jsonl" not in _by_path(_surfaces(repo, ["src/pkg/a.py"]))


# --------------------------------------------------------------------------- #
# the script as a script
# --------------------------------------------------------------------------- #


def _run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_the_script_reads_the_branch_change_and_prints_the_summary_last(repo):
    _write(repo, "src/pkg/a.py", "x = 99\n")
    out = _run("--repo", str(repo), cwd=repo)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[0].startswith("changed paths (1) vs master")
    assert lines[-1] == "related surfaces: 1 to check, 0 touched by this diff, 1 naming tests (the gate runs them)"
    assert any(line.startswith("CHECK") and "src/pkg/b.py" in line for line in lines)
    assert any(line.startswith("gate") and "tests/test_a_guard.py" in line for line in lines)


def test_json_output_carries_the_same_facts(repo):
    _write(repo, "src/pkg/a.py", "x = 99\n")
    out = _run("--repo", str(repo), "--json", cwd=repo)
    data = json.loads(out.stdout)
    assert data["changed"] == ["src/pkg/a.py"]
    assert {s["path"] for s in data["surfaces"]} == {"src/pkg/b.py", "tests/test_a_guard.py"}
    assert data["summary"] == "related surfaces: 1 to check, 0 touched by this diff, 1 naming tests (the gate runs them)"


def test_paths_can_be_given_instead_of_asking_git(repo):
    out = _run("--repo", str(repo), "--paths", "src/pkg/a.py", cwd=repo)
    assert out.returncode == 0
    assert "(given)" in out.stdout.splitlines()[0]


def test_outside_a_repository_it_cannot_tell(tmp_path):
    out = _run("--repo", str(tmp_path), cwd=tmp_path)
    assert out.returncode == rs.CANNOT_TELL
    assert out.stdout.startswith("cannot tell")


def test_it_loads_on_the_standard_library_alone():
    out = subprocess.run(
        [sys.executable, "-S", "-E", str(SCRIPT), "--help"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert out.returncode == 0 and "ModuleNotFoundError" not in out.stderr, out.stderr


# --------------------------------------------------------------------------- #
# this repository: the evidence criterion of claunch-6ccc
# --------------------------------------------------------------------------- #


def test_this_repository_lists_the_web_modal_for_a_spawn_contract_change():
    """The live mismatch of claunch-3uef ASSESSMENT 1.2 (``app.js`` ~5279:
    the spawn mode of the new-session modal not sending ``workflow: "-"``)
    is a mirror of the wizard and the CLI. A change to either has to put
    ``app.js`` on the list -- by the guard table, and independently by
    history.

    The window is this repository's whole history, not the tool's default
    (``DEFAULT_LIMIT``, 800 commits). The default is a reading budget for
    the review step; this assertion is not a budget but the evidence
    criterion of claunch-6ccc, and a fixed window makes it a claim about
    where the commits happen to sit rather than about whether the files
    are coupled. ``app.js`` and ``cli_sessions.py`` had met in exactly
    three counted commits inside 800; when #620 landed six commits,
    ``331b03ef`` (prompt-packs, 2026-10-18) fell out of the window, the
    count became two, and this test failed on master with nothing changed
    in either file. Every commit that adds to the repository pushes the
    oldest evidence out, so the assertion would fail again for the same
    non-reason -- while a real break in the coupling (the case this test
    exists for) still fails it, because the count would go to zero.
    """
    out = _run("--paths", "src/claude_launcher/wizard.py", "src/claude_launcher/cli_sessions.py",
               "--limit", "100000", "--json")
    assert out.returncode == 0, out.stderr
    by = {s["path"]: s for s in json.loads(out.stdout)["surfaces"]}
    app = by["src/claude_launcher/web/static/app.js"]
    assert "guard:spawn-contract" in app["reasons"]
    assert any(r.startswith("cochange:") for r in app["reasons"]), app
    assert set(app["for"]) == {"src/claude_launcher/wizard.py", "src/claude_launcher/cli_sessions.py"}
    assert app["touched"] is False


def test_every_guard_group_member_exists_in_this_checkout():
    for name, files in rs.SURFACE_GROUPS:
        for rel in files:
            assert (ROOT / rel).is_file(), f"{name}: {rel} is not a file"


# --------------------------------------------------------------------------- #
# the window a call reads: this repository's history is not a reading budget
# --------------------------------------------------------------------------- #

_SELF = Path(__file__).resolve()
_HERE = str(ROOT)

_EXPR = "\x00not-a-constant"


def _argv_of(nodes):
    """The literal arguments a call carries. Anything that is not a string
    constant becomes ``_EXPR`` -- present in the list so ``in`` tests still
    see the flag, but carrying no text to match on."""
    return [
        n.value
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        else _EXPR
        for n in nodes
    ]


def _reads_this_checkout(node):
    """Does this argument name the checkout the test is standing in?

    ``ROOT``, ``str(ROOT)`` and the literal path all resolve to it. A fixture
    name (``repo``), a ``tmp_path`` or anything else does not: the tool cannot
    fall back to that path on its own, so its history is a fixed thing the
    case built rather than a window that slides as this repository grows."""
    if isinstance(node, ast.Name):
        return node.id == "ROOT"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == _HERE
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "str":
        return bool(node.args) and _reads_this_checkout(node.args[0])
    return False


def _names_the_script(node):
    """Whether an argv element is the script, however it is spelled.

    The two spellings in this file are ``SCRIPT`` and ``str(SCRIPT)``. Testing
    the element's own name against ``SCRIPT`` only ever matched the first, so
    the bare ``subprocess.run`` branch below was dead code -- it matched no
    call at all, and a bare call reading this repository without ``--limit``
    passed the guard (found by three reviewers on claunch-o5il6). Walking the
    element instead catches both, and any future wrapper."""
    return any(isinstance(n, ast.Name) and n.id == "SCRIPT" for n in ast.walk(node))


def _repo_verdict(nodes, in_process=False):
    """Whether a call reads *this* repository's history.

    Three ways to end up there: no ``--repo`` at all (the tool falls back to
    ``ROOT``), ``--repo`` handed this checkout's own path, or an in-process
    ``rs.history(ROOT)``. Only the first is an ``in`` test on the flag list --
    the other two are why this is a function.

    ``--help``/``-h`` is the one argv that cannot reach ``history()``:
    ``parse_args`` raises ``SystemExit`` on it before the tool reads anything.
    The claim is pinned by :func:`test_help_exits_before_the_tool_reads_history`
    rather than asserted here, so it cannot rot into a silent exemption."""
    if in_process:
        return bool(nodes) and _reads_this_checkout(nodes[0])
    argv = _argv_of(nodes)
    if "--help" in argv or "-h" in argv:
        return False
    if "--repo" not in argv:
        return True
    at = argv.index("--repo")
    if at + 1 >= len(nodes):
        return True  # --repo with no value: argparse will refuse it, but do not exempt it
    return _reads_this_checkout(nodes[at + 1])


def _tool_invocations(source=None):
    """Every place a source invokes the tool, as ``(lineno, argv, reads_here)``.

    Three shapes: the ``_run(...)`` helper (its definition prepends
    ``sys.executable, str(SCRIPT)``, so a call site's own arguments are the
    tool's argv), a bare ``subprocess.run([...])`` naming the script anywhere
    in its argument list, and the in-process ``rs.history(<repo>)``. Parsed
    from source rather than grepped so a call split across lines is still one
    call."""
    tree = ast.parse(source if source is not None else _SELF.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)

        if name == "_run":
            out.append((node.lineno, _argv_of(node.args), _repo_verdict(node.args)))
            continue

        if name == "history" and getattr(getattr(func, "value", None), "id", None) == "rs":
            out.append((node.lineno, [], _repo_verdict(node.args[:1], in_process=True)))
            continue

        if name != "run" or not node.args or not isinstance(node.args[0], ast.List):
            continue
        items = node.args[0].elts
        at = next((i for i, el in enumerate(items) if _names_the_script(el)), None)
        if at is None:
            continue
        tail = items[at + 1 :]
        if any(isinstance(el, ast.Starred) for el in tail):
            # The helper's own definition forwards ``*args``; its call sites
            # above are the invocations, and this body has no argv of its own.
            continue
        out.append((node.lineno, _argv_of(tail), _repo_verdict(tail)))
    return out


def test_every_call_into_this_repository_names_its_window():
    """A call that reads *this* repository's history must say how far back.

    ``DEFAULT_LIMIT`` (800) is the review step's reading budget: a fixed count
    of commits back from HEAD. For the review step that is the right shape --
    it bounds what a reader pays. For an assertion about this repository it is
    not: the window slides with every landing, so the test measures where the
    commits happen to sit rather than whether the files are coupled.
    ``app.js`` and ``cli_sessions.py`` had met in exactly three counted commits
    inside 800; #620 landed six commits, ``331b03ef`` fell from 797 to 803, and
    the count dropped to two with neither file changed (claunch-o5il6).

    A call with ``--repo`` reads a repository it was handed -- a temporary one
    built by the fixture -- and its history cannot slide past anything that
    matters, so it is exempt. ``--paths`` does **not** exempt: it narrows which
    files the question is about, but the history is still *this* repository's
    log read back ``--limit`` commits.

    The exemption is the *path*, not the flag: ``--repo str(ROOT)``,
    ``--repo ROOT`` and the bare literal name this checkout, so they read this
    repository as surely as no ``--repo`` does, and ``rs.history(ROOT)`` skips
    the CLI altogether. All three were outside an earlier version of this
    guard, which matched the flag's presence rather than its value (reviewers
    s490-rev, s404-rev, s648-rev on claunch-o5il6)."""
    calls = _tool_invocations()
    assert calls, "no invocation of the tool was found -- the parser lost them"

    this_repository = []
    for lineno, argv, reads_here in calls:
        if not reads_here:
            continue
        this_repository.append(lineno)
        assert "--limit" in argv, (
            f"line {lineno}: this call reads this repository's history without "
            f"--limit, so it rides the sliding DEFAULT_LIMIT ({rs.DEFAULT_LIMIT}). "
            f"Pass a window that fits the assertion, as "
            f"test_this_repository_lists_the_web_modal_for_a_spawn_contract_change does."
        )

    # The guard is only worth its weight if something actually reads this
    # repository through it; an empty list would make every line above vacuous.
    assert this_repository, (
        "no call reads this repository any more -- if that is intended, delete "
        "this guard rather than leaving it passing on nothing"
    )


def test_a_call_handed_its_own_repository_may_keep_the_default():
    """The exemption is a real distinction, not a hole: the temporary-repository
    calls above are exempt because the tool falls back to ``ROOT`` only when it
    is given nothing. Assert that here, or the guard could be satisfied by
    exempting everything."""
    calls = _tool_invocations()
    exempt = [n for n, _argv, reads_here in calls if not reads_here]
    assert exempt, "the exemption matches nothing -- the guard above is untested"

    # And the tool's own default really is the sliding window this test names,
    # so the guard cannot be satisfied by the tool quietly changing shape.
    assert rs.DEFAULT_LIMIT == 800

    sig = inspect.signature(rs.history)
    assert sig.parameters["limit"].default == rs.DEFAULT_LIMIT


def test_help_exits_before_the_tool_reads_history():
    """``--help`` is the one argv the guard exempts without a ``--limit``.

    The exemption is a claim about ``argparse``: ``parse_args`` raises
    ``SystemExit`` on ``--help`` before ``main`` reaches ``history()``, so no
    window is ever read. Assert it against the tool itself, because an
    exemption that only *looks* true is the same hole this guard exists to
    close. What proves the point is the absence of the rendered answer: the
    usage text itself mentions "changed paths" as a flag description, so the
    marker has to be the summary line the tool prints after reading history."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert out.returncode == 0
    assert "usage: related_surfaces" in out.stdout
    # No rendering: the tool's own facts never printed.
    assert "related surfaces:" not in out.stdout
    assert out.stdout.lstrip().startswith("usage:")


def test_the_parser_sees_both_spellings_of_the_script():
    """The bare ``subprocess.run`` branch must stay alive.

    An earlier version matched a list element's *name* against ``SCRIPT``, but
    this file writes ``str(SCRIPT)``, so the branch matched nothing and a bare
    call reading this repository without ``--limit`` passed the guard
    (claunch-o5il6, found independently by s1262-rev, s404-rev and s648-rev).
    Assert the shape directly on a sample rather than trusting the live file to
    happen to contain one: a guard whose branch is dead looks exactly like a
    guard that finds nothing wrong."""
    sample = """
def _run(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd or ROOT))

def test_a():
    out = subprocess.run([sys.executable, str(SCRIPT), "--json"], cwd=str(ROOT))

def test_b():
    out = subprocess.run([sys.executable, SCRIPT, "--limit", "5"], cwd=str(ROOT))

def test_c():
    out = _run("--repo", str(tmp_path), cwd=tmp_path)

def test_d():
    rs.history(ROOT)

def test_e():
    rs.history(repo, max_files=100)
"""
    calls = _tool_invocations(sample)
    by_line = {lineno: (argv, reads_here) for lineno, argv, reads_here in calls}

    # _run's own body forwards *args, so it is not itself an invocation; its
    # three call-site shapes below are what the guard reads.
    assert len(calls) == 5, [(n, a, r) for n, a, r in calls]

    bare_str = by_line[6]
    assert bare_str == (["--json"], True), bare_str
    bare_name = by_line[9]
    assert bare_name == (["--limit", "5"], True), bare_name
    via_run = by_line[12]
    assert via_run[1] is False, via_run
    in_process_root = by_line[15]
    assert in_process_root == ([], True), in_process_root
    in_process_fixture = by_line[18]
    assert in_process_fixture == ([], False), in_process_fixture


def test_the_parser_treats_this_checkouts_own_path_as_this_repository():
    """``--repo`` names the path it was handed, so three spellings that all
    resolve to this checkout are *not* exemptions.

    The s490-rev finding on claunch-o5il6: the guard exempted any call that
    carried ``--repo``, including one pointed back at this repository."""
    sample = """
def test_a():
    out = _run("--repo", str(ROOT), "--json")

def test_b():
    out = _run("--repo", ROOT)

def test_c():
    out = _run("--repo", "@LITERAL@")

def test_d():
    out = _run("--repo", "@ELSEWHERE@")

def test_e():
    out = _run("--repo")
""".replace("@LITERAL@", _HERE).replace("@ELSEWHERE@", _HERE + "-elsewhere")
    by_line = {n: (a, r) for n, a, r in _tool_invocations(sample)}

    assert by_line[3][1] is True, by_line[3]        # --repo str(ROOT)
    assert by_line[6][1] is True, by_line[6]        # --repo ROOT
    assert by_line[9][1] is True, by_line[9]        # the literal path
    assert by_line[12][1] is False, by_line[12]     # another checkout is exempt
    assert by_line[15][1] is True, by_line[15]      # --repo with no value is not exempt


def test_the_review_step_asks_for_the_sentence_and_names_the_tool():
    sys.path.insert(0, str(ROOT / "src"))
    from claude_launcher.cflow import model

    for path in (
        ROOT / "src" / "claude_launcher" / "workflows" / "improv-worker.yaml",
        ROOT / ".claunch" / "workflows" / "improv-worker.yaml",
    ):
        wf = model.load(path)
        review = wf.steps["review"]
        assert "tools/related_surfaces.py --base auto" in review.instructions
        assert "관련 표면 N개 중 M개 점검" in review.instructions
        assert "관련 표면 N개 중 M개 점검" in review.done_when


def test_the_review_step_counts_a_naming_test_the_gate_does_not_select():
    """The step's gloss on the summary line follows the tool: a naming test
    outside the gate's selection is ``test:unselected`` and counted in N, so
    the step no longer says every naming test is left out (claunch-8tl8i).

    The two lines it quotes are the ones :func:`rs.render` prints, placeholders
    and spacing included, so a reader who greps the output for them finds
    them; one that is also in the diff is ``touched`` (claunch-gvfob)."""
    sys.path.insert(0, str(ROOT / "src"))
    from claude_launcher.cflow import model

    found = [
        {"path": "<경로>", "reasons": [reason], "for": ["<파일>"], "touched": False}
        for reason in ("test", "test:unselected")
    ]
    printed = rs.render([], found, "").splitlines()
    gate_line = next(line for line in printed if line.startswith("gate"))
    check_line = next(line for line in printed if line.startswith("CHECK"))
    for path in (
        ROOT / "src" / "claude_launcher" / "workflows" / "improv-worker.yaml",
        ROOT / ".claunch" / "workflows" / "improv-worker.yaml",
    ):
        review = model.load(path).steps["review"]
        assert f"`{gate_line}`" in review.instructions
        assert f"`{check_line}`" in review.instructions
        assert "`touched`" in review.instructions
        assert "tools/changed_tests.py --list" in review.instructions
        assert "세지 않고 기록만 한다" not in review.instructions
