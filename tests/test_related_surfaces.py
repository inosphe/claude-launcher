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

#: The tool's file name. A call from *another* test module cannot say
#: ``SCRIPT`` -- that name is this file's -- so the guard recognises the tool
#: by this string wherever it appears in an argv (claunch-o5il6.3).
_TOOL_FILE = "related_surfaces.py"

#: ``subprocess`` entry points that take an argv list first. All three read
#: history the same way once the script is in the list; collecting only
#: ``run`` left ``check_output`` and ``Popen`` invisible (claunch-o5il6.2).
_SHELL_ENTRIES = ("run", "check_output", "Popen")

#: Calls that produce the same directory as ``resolve()`` for our purposes.
#: ``absolute()`` differs in symlink handling and not in which directory the
#: chain below it names, so refusing it was a spelling gap, not a distinction.
_RESOLVERS = ("resolve", "absolute")


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

_LIMIT = "--limit"


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


def _names_a_window(argv):
    """Whether an argv carries ``--limit``, in either spelling argparse takes.

    ``--limit=100000`` is one token and reads the same window as
    ``--limit 100000``; matching only the bare flag would have read the first
    as no window at all."""
    return any(a == _LIMIT or a.startswith(_LIMIT + "=") for a in argv)


def _path_text(node):
    """The path an argument names, as text; ``None`` when it names none.

    ``ROOT`` becomes this checkout's absolute path, and the wrappers this file
    puts around it (``str(ROOT)``, ``Path(ROOT)``) are unwrapped. Two idioms
    that also land here are recognised: a string constant keeps whatever it
    spells -- ``"."`` is relative, so reading it needs the call's ``cwd`` (see
    :func:`_resolves_here`) -- and ``Path(__file__).resolve().parents[N]``,
    which is how this file already writes ``_SELF`` and how a sibling module
    would reach the checkout from a test directory (s597-rev, claunch-o5il6).
    Anything else returns ``None``: a fixture name such as ``repo`` or
    ``tmp_path`` has no location the source states."""
    if isinstance(node, ast.Name):
        return _HERE if node.id == "ROOT" else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("str", "Path") and node.args:
        return _path_text(node.args[0])
    return _this_files_ancestor(node)


def _literal_index(node):
    """The non-negative integer a subscript carries, or ``None``.

    ``parents[-1]`` and a computed index are left alone: guessing which
    directory they name would be a claim the source does not make."""
    sl = getattr(node, "slice", node)
    if isinstance(sl, ast.Index):  # Python < 3.9
        sl = sl.value
    if isinstance(sl, ast.Constant) and isinstance(sl.value, int) and sl.value >= 0:
        return sl.value
    return None


def _this_files_ancestor(node):
    """A path walked from ``__file__``, or ``None``.

    ``__file__`` is this module, so ``parents[0]`` is this file's directory and
    each hop after that is one level up. That idiom is how this file already
    writes ``_SELF``, and how a sibling test would walk to the checkout from a
    test directory -- s597-rev found it unrecognised (claunch-o5il6).

    Three spellings reach the same directory and all three are read now
    (claunch-o5il6.2): ``parents[N]``, a chain of ``.parent`` attributes, and
    either with ``.resolve()``/``.absolute()``/no resolver called on the
    ``Path(__file__)`` first. ``pathlib.Path(...)`` counts as ``Path(...)``.
    Refusing them was a gap in what the guard could see, not a distinction it
    was drawing: each names this checkout exactly as ``parents[1]`` does.

    Only chains rooted at ``__file__`` are read. Some *other* object's
    ``parents[N]`` says nothing about this checkout, so treating it as one
    would be a false positive in the over-blocking direction."""
    if isinstance(node, ast.Subscript) and getattr(node.value, "attr", None) == "parents":
        hop = _literal_index(node)
        if hop is None or _file_base(node.value.value) is None:
            return None
        base = _SELF.parent
        for _ in range(hop):
            base = base.parent
        return str(base)

    # ``Path(__file__).resolve().parent.parent`` -- one hop per attribute.
    hops = 0
    chain = node
    while isinstance(chain, ast.Attribute) and chain.attr == "parent":
        hops += 1
        chain = chain.value
    if hops and _file_base(chain) is not None:
        base = _SELF
        for _ in range(hops):
            base = base.parent
        return str(base)
    return None


def _is_path_ctor(func):
    """``Path`` or ``pathlib.Path`` -- the two spellings a test writes."""
    if getattr(func, "id", None) == "Path":
        return True
    return (
        getattr(func, "attr", None) == "Path"
        and isinstance(getattr(func, "value", None), ast.Name)
        and func.value.id == "pathlib"
    )


def _file_base(node):
    """``Path(__file__)``, with or without a resolver call, or ``None``.

    Both constructor spellings and both resolvers are read: a module that
    imports ``pathlib`` rather than the name writes ``pathlib.Path``, and
    ``absolute()`` names the same directory ``resolve()`` does for this
    purpose. Reading only ``Path(__file__).resolve()`` left both spellings
    invisible (claunch-o5il6.2)."""
    chain = node
    if (
        isinstance(chain, ast.Call)
        and not chain.args
        and getattr(chain.func, "attr", None) in _RESOLVERS
    ):
        chain = chain.func.value
    if not (isinstance(chain, ast.Call) and chain.args and _is_path_ctor(chain.func)):
        return None
    first = chain.args[0]
    if isinstance(first, ast.Name) and first.id == "__file__":
        return chain
    return None


def _resolves_here(text, base):
    """Whether path ``text`` lies inside the checkout this file lives in.

    A subdirectory counts: ``git -C <subdir>`` reads the repository that
    contains it, so ``--repo src`` still walks this repository's log.

    ``base`` is what a relative path is relative to. ``None`` means the base
    could not be read from the source -- and then this answers ``False``
    rather than guessing, because a relative path with no readable base is the
    file's fixture convention (``cwd=repo``) and not a claim about this
    checkout. An *absent* base never reaches here: :func:`_cwd_of` fills it."""
    if text is None:
        return False
    path = Path(text)
    if not path.is_absolute():
        if base is None:
            return False
        path = Path(base) / path
    try:
        resolved = path.resolve()
    except OSError:
        return False
    here = Path(_HERE)
    return resolved == here or here in resolved.parents


def _within_this_checkout(node, base):
    """Does this argument name a directory inside the checkout under test?"""
    return _resolves_here(_path_text(node), base)


def _run_cwd_default():
    """The directory ``_run`` runs in when its caller omits ``cwd``.

    Read from ``_run``'s own definition rather than restated here: the guard's
    verdict for a bare ``_run(...)`` depends on that default, and a copy of it
    in this function would go stale silently the day ``_run`` changed. Before
    this, ``_cwd_of`` returned the literal ``_HERE`` and the two facts lived
    in two places with nothing holding them together (claunch-o5il6.1).

    ``_run`` writes ``cwd=str(cwd or ROOT)``. Three shapes are read: that
    ``or`` default, a plain ``ROOT``/``_HERE`` name, and the ``str(...)``
    wrapper around either. Anything else answers ``None`` -- an unreadable
    default leaves relative paths exempt rather than guessed at."""
    for node in ast.walk(ast.parse(_SELF.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef) or node.name != "_run":
            continue
        for kw in ast.walk(node):
            if not (isinstance(kw, ast.keyword) and kw.arg == "cwd"):
                continue
            value = kw.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "str" and value.args:
                value = value.args[0]
            if isinstance(value, ast.BoolOp):
                # ``cwd or ROOT``: the right-hand side is the default.
                value = value.values[-1]
            if isinstance(value, ast.Name) and value.id in ("ROOT", "_HERE"):
                return _HERE
        return None
    return None


def _cwd_of(call, default=None):
    """The directory a ``subprocess`` call runs in when it names no ``cwd``.

    ``default`` is what the call inherits. For the ``_run`` helper that is
    whatever ``_run`` itself defaults to (:func:`_run_cwd_default`); for a bare
    ``subprocess.run`` it is the pytest process's directory, which this file
    cannot place, so ``None``. Keeping the two apart is the point: they are
    different directories, and collapsing them into one literal was how the
    guard's answer for ``_run("--repo", ".")`` came to depend on a fact stated
    somewhere else with nothing pinning it (claunch-o5il6.1).

    A ``cwd`` the source cannot place (``cwd=repo``) comes back as ``None``,
    which leaves a relative path exempt rather than misread."""
    for kw in call.keywords:
        if kw.arg == "cwd":
            return _path_text(kw.value)
    return default


def _names_the_script(node, from_this_file=True):
    """Whether an argv element is the tool, however it is spelled.

    The two spellings in this file are ``SCRIPT`` and ``str(SCRIPT)``. Testing
    the element's own name against ``SCRIPT`` only ever matched the first, so
    the bare ``subprocess.run`` branch below was dead code -- it matched no
    call at all, and a bare call reading this repository without ``--limit``
    passed the guard (found by three reviewers on claunch-o5il6). Walking the
    element instead catches both, and any future wrapper.

    ``SCRIPT`` is *this* module's name for the tool, so a call from another
    test file cannot write it. There the tool is named by its file, so any
    string spelling ``related_surfaces.py`` counts (claunch-o5il6.3)."""
    if any(isinstance(n, ast.Name) and n.id == "SCRIPT" for n in ast.walk(node)):
        return True
    if from_this_file:
        return False
    return any(
        isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and Path(n.value).name == _TOOL_FILE
        for n in ast.walk(node)
    )


def _repo_verdict(nodes, base=None, in_process=False):
    """Whether a call reads *this* repository's history.

    Three ways to end up there: no ``--repo`` at all (the tool's default is
    ``"."``, resolved against the directory the call runs in), ``--repo``
    handed a path inside this checkout, or an in-process ``rs.history(<repo>)``
    / ``rs.surfaces(<repo>, ...)``. Only the first is an ``in`` test on the
    flag list -- the other two are why this is a function.

    ``base`` is the directory a relative path resolves against: the call's
    ``cwd`` for a subprocess, ``None`` for an in-process call. See
    :func:`_resolves_here` for what an unreadable base means.

    ``--help``/``-h`` is the one argv that cannot reach ``history()``:
    ``parse_args`` raises ``SystemExit`` on it before the tool reads anything.
    The claim is pinned by :func:`test_help_exits_before_the_tool_reads_history`
    rather than asserted here, so it cannot rot into a silent exemption."""
    if in_process:
        return bool(nodes) and _within_this_checkout(nodes[0], base)
    argv = _argv_of(nodes)
    if "--help" in argv or "-h" in argv:
        return False
    if "--repo" not in argv:
        # The tool's own default: --repo "." against this call's directory.
        return _resolves_here(".", base)
    at = argv.index("--repo")
    if at + 1 >= len(nodes):
        return True  # --repo with no value: argparse will refuse it, but do not exempt it
    return _within_this_checkout(nodes[at + 1], base)


def _history_window(call):
    """The window an in-process ``rs.history(...)`` call names, as argv.

    ``limit`` reaches the function two ways -- the second positional argument
    and the ``limit`` keyword -- and both are a window. Reading only argv
    spellings made a correctly windowed call look like a bare one, so it failed
    the guard for the right reason in the wrong direction (s490-rev, s648-rev
    and s404-rev all reported this on claunch-o5il6)."""
    if len(call.args) > 1:
        return [_LIMIT]
    if any(kw.arg == "limit" for kw in call.keywords):
        return [_LIMIT]
    return []


def _surfaces_window(call):
    """The window an in-process ``rs.surfaces(...)`` call names, as argv.

    ``surfaces`` takes no ``limit``: when ``commits`` is given it reads the
    history it was handed, and when it is not it calls ``history(repo)`` at
    ``DEFAULT_LIMIT`` -- the sliding window itself. So the window is *present*
    exactly when ``commits`` is passed with a value. The expression handed to
    ``commits`` is an invocation in its own right and is collected separately,
    so ``commits=rs.history(repo, limit=50)`` is checked twice and passes both
    times."""
    for kw in call.keywords:
        if kw.arg != "commits":
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            return []  # commits=None is the tool's own default path
        return [_LIMIT]
    return []


def _tool_invocations(source=None):
    """Every place a source invokes the tool, as ``(lineno, argv, reads_here)``.

    Four shapes: the ``_run(...)`` helper (its definition prepends
    ``sys.executable, str(SCRIPT)``, so a call site's own arguments are the
    tool's argv), a bare ``subprocess.run``/``check_output``/``Popen`` naming
    the script anywhere in its argument list, and the in-process
    ``rs.history(<repo>)`` and ``rs.surfaces(<repo>, ...)``. Parsed from source
    rather than grepped so a call split across lines is still one call.

    ``source`` defaults to this module; :func:`_every_test_source` passes each
    other test file in turn, so a sibling that calls the tool without a window
    is seen too (claunch-o5il6.3).

    ``argv`` is what the caller wrote, plus a synthesised ``--limit`` where the
    in-process functions take their window another way. It is not a faithful
    argv: it is the shape the guard's ``"--limit" in argv`` test reads."""
    own = source is None
    tree = ast.parse(source if not own else _SELF.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)

        if name == "_run":
            out.append(
                (node.lineno, _argv_of(node.args),
                 _repo_verdict(node.args, _cwd_of(node, _run_cwd_default())))
            )
            continue

        if getattr(getattr(func, "value", None), "id", None) == "rs":
            if name == "history":
                out.append(
                    (node.lineno, _history_window(node), _repo_verdict(node.args[:1], in_process=True))
                )
                continue
            if name == "surfaces":
                out.append(
                    (node.lineno, _surfaces_window(node), _repo_verdict(node.args[:1], in_process=True))
                )
                continue

        if name not in _SHELL_ENTRIES or not node.args or not isinstance(node.args[0], ast.List):
            continue
        items = node.args[0].elts
        at = next(
            (i for i, el in enumerate(items) if _names_the_script(el, from_this_file=own)),
            None,
        )
        if at is None:
            continue
        tail = items[at + 1 :]
        if any(isinstance(el, ast.Starred) for el in tail):
            # The helper's own definition forwards ``*args``; its call sites
            # above are the invocations, and this body has no argv of its own.
            continue
        base = _cwd_of(node)
        out.append((node.lineno, _argv_of(tail), _repo_verdict(tail, base)))
    return out


def _every_test_source():
    """``(path, source)`` for every test module that names the tool.

    The guard used to parse only its own file, so a call from another test
    module was outside it entirely (claunch-o5il6.3). Each module is read as
    text and parsed here rather than imported: importing would run it, and a
    test module has fixtures this one does not want to trigger. The narrowing
    to modules that mention the tool keeps the parse to a handful of files,
    and a module that cannot be parsed is skipped by name -- never silently,
    the caller asserts the list is non-empty.

    This is deliberately *only* the files that name the tool. Widening to
    every test file would parse a thousand modules to find the same few
    mentions, and it would put a syntax error anywhere in the suite on this
    guard's exit path."""
    found = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _TOOL_FILE not in text and "related_surfaces" not in text:
            continue
        found.append(path)
    return found


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
    s490-rev, s404-rev, s648-rev on claunch-o5il6).

    Every test module that names the tool is read, not just this one: a
    sibling calling ``related_surfaces.py`` with ``check_output`` and no
    ``--limit`` reads this repository's sliding window exactly as this file
    would (claunch-o5il6.3). ``(line, module)`` pairs are carried into the
    failure message, because a line number alone is ambiguous once more than
    one file is parsed."""
    sources = _every_test_source()
    assert sources, (
        "no test module names the tool -- if the tool is gone, delete this "
        "guard rather than leaving it passing on nothing"
    )

    calls = []
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = _tool_invocations(text)
        except SyntaxError as exc:  # pragma: no cover -- the suite would fail first
            raise AssertionError(f"{path.relative_to(ROOT)} could not be parsed: {exc}") from exc
        calls.extend((path.relative_to(ROOT), lineno, argv, reads_here) for lineno, argv, reads_here in parsed)

    assert calls, "no invocation of the tool was found -- the parser lost them"

    this_repository = []
    for where, lineno, argv, reads_here in calls:
        if not reads_here:
            continue
        this_repository.append((where, lineno))
        assert _names_a_window(argv), (
            f"{where}:{lineno}: this call reads this repository's history without "
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


def test_the_guards_default_cwd_is_read_from_run_and_not_restated():
    """``_cwd_of``'s default for ``_run`` is ``_run``'s own ``cwd`` default.

    The hole claunch-o5il6.1: ``_cwd_of`` returned the literal ``_HERE`` while
    ``_run`` wrote ``cwd=str(cwd or ROOT)``. The two agreed by hand, so the
    guard's verdict for ``_run("--repo", ".")`` depended on a fact written in
    another function with nothing holding the pair together -- change
    ``_run``'s default and the guard keeps answering "this repository".

    So this asserts the derived value, and then asserts it *moves* when the
    default moves. Both halves matter: the first would pass on a hardcoded
    ``_HERE`` too, and only the second tells a derived default from a copied
    one. The mutation is done on a sample source rather than on this file, so
    the assertion cannot be satisfied by editing what it is testing."""
    assert _run_cwd_default() == _HERE

    # ``_run`` really does default ``cwd`` to ``ROOT`` today. If someone
    # changes it to ``cwd=cwd`` (inherit pytest's directory), this fails here
    # rather than silently making ``_run``'s calls read elsewhere.
    run_source = next(
        n for n in ast.walk(ast.parse(_SELF.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    run_cwd = next(kw.value for kw in ast.walk(run_source) if isinstance(kw, ast.keyword) and kw.arg == "cwd")
    assert isinstance(run_cwd, ast.Call) and getattr(run_cwd.func, "id", None) == "str", ast.dump(run_cwd)
    boolop = run_cwd.args[0]
    assert isinstance(boolop, ast.BoolOp), ast.dump(boolop)
    assert getattr(boolop.values[-1], "id", None) == "ROOT", ast.dump(boolop)

    # A caller that names no ``cwd`` inherits the helper's default...
    call = ast.parse('_run("--repo", ".")').body[0].value
    assert _cwd_of(call, _run_cwd_default()) == _HERE

    # ...and a bare ``subprocess.run`` inherits the pytest process's directory,
    # which this file cannot place. Treating the two as one value is exactly
    # what the hole was, so they must answer differently.
    bare = ast.parse('subprocess.run([sys.executable, str(SCRIPT)])').body[0].value
    assert _cwd_of(bare) is None
    assert _resolves_here(".", _cwd_of(bare)) is False


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


def test_a_relative_repo_is_read_against_the_directory_the_call_runs_in():
    """``--repo "."`` is this repository or a temporary one depending on ``cwd``.

    The tool defaults ``--repo`` to ``"."`` and resolves it against the working
    directory, and ``_run`` defaults ``cwd`` to ``ROOT``. So ``_run("--repo",
    ".")`` reads *this* repository while looking like a relative path the guard
    would exempt -- a hole s490-rev found on claunch-o5il6, and the mirror of
    its opposite: ``_run(cwd=tmp_path)`` with no ``--repo`` reads the temporary
    repository, so flagging it as this checkout's would be over-blocking. Both
    directions are asserted here because only the pair pins the ``cwd`` axis."""
    sample = """
def _run(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(cwd or ROOT))

def test_a():
    _run("--repo", ".")

def test_b():
    _run("--repo", ".", cwd=tmp_path)

def test_c():
    _run(cwd=tmp_path)

def test_d():
    _run("--repo", "src")

def test_e():
    _run("--limit", "100000")
"""
    by_line = {n: (a, r) for n, a, r in _tool_invocations(sample)}

    dotted = by_line[6]
    assert dotted[1] is True, dotted     # _run defaults cwd to ROOT, so "." is here
    assert dotted[0] == ["--repo", "."], dotted

    elsewhere = by_line[9]
    assert elsewhere[1] is False, elsewhere      # "." against a temporary cwd

    no_repo = by_line[12]
    assert no_repo[1] is False, no_repo          # no --repo, but cwd is elsewhere

    subdirectory = by_line[15]
    assert subdirectory[1] is True, subdirectory  # git -C src reads the repo above it

    windowed = by_line[18]
    assert windowed[1] is True and _names_a_window(windowed[0]), windowed


def test_a_window_counts_however_the_flag_is_spelled():
    """``--limit=100000`` is one token and names the same window.

    Matching only the bare flag would read it as no window and fail a call that
    did everything right."""
    assert _names_a_window(["--limit", "100000"])
    assert _names_a_window(["--limit=100000"])
    assert not _names_a_window(["--json"])
    assert not _names_a_window(["--limits", "5"])
    assert not _names_a_window([])


def test_an_in_process_call_may_name_its_window_as_an_argument():
    """``rs.history`` takes ``limit`` as a positional or keyword argument.

    Both are a window, but neither appears in the ``argv`` the guard reads, so
    a call that named its window correctly was rejected as if it had named none
    -- the over-blocking direction. s490-rev, s648-rev and s404-rev all reported
    it on claunch-o5il6, and it is the one case where this guard was wrong
    about a *correct* call."""
    sample = """
def test_a():
    rs.history(ROOT, limit=50)

def test_b():
    rs.history(ROOT, 50)

def test_c():
    rs.history(ROOT)

def test_d():
    rs.history(ROOT, max_files=100)
"""
    by_line = {n: (a, r) for n, a, r in _tool_invocations(sample)}

    keyword = by_line[3]
    assert keyword[1] is True and _names_a_window(keyword[0]), keyword
    positional = by_line[6]
    assert positional[1] is True and _names_a_window(positional[0]), positional
    bare = by_line[9]
    assert bare[1] is True and not _names_a_window(bare[0]), bare
    # max_files is not limit: narrowing which commits count is not naming a
    # window to read, so this stays a call that rides DEFAULT_LIMIT.
    other_keyword = by_line[12]
    assert other_keyword[1] is True and not _names_a_window(other_keyword[0]), other_keyword


def test_surfaces_reads_this_repositorys_history_unless_it_is_handed_one():
    """``rs.surfaces`` is a second way into ``history`` and was not collected.

    With no ``commits`` it calls ``history(repo)`` at ``DEFAULT_LIMIT`` -- the
    sliding window this guard exists for -- so a bare ``rs.surfaces(ROOT, [...])``
    is exactly the call that must fail. ``commits=`` is what names a window
    instead, and the expression handed to it is an invocation in its own right,
    so ``commits=rs.history(ROOT, limit=50)`` is collected twice: once as the
    history call that names the window, once as the surfaces call handed it.
    s490-rev found this on claunch-o5il6 and asked for either a guard or an
    explicit statement that the shape is out of scope.

    Both of those calls are asserted below, on separate lines. An earlier
    version wrote the ``commits`` argument inline, putting the outer and inner
    calls on one line, and the ``by_line`` lookup then kept only the inner one
    -- so the outer verdict was never checked while this docstring said it was
    (s1262-rev, claunch-o5il6)."""
    sample = """
def test_a():
    rs.surfaces(ROOT, ["a.py"])

def test_b():
    rs.surfaces(ROOT, ["a.py"], commits=None)

def test_c():
    window = rs.history(ROOT, limit=50)
    rs.surfaces(ROOT, ["a.py"], commits=window)

def test_d():
    rs.surfaces(repo, ["a.py"])
"""
    calls = _tool_invocations(sample)
    by_line = {n: (a, r) for n, a, r in calls}

    # One call per line, deliberately: this test used to put the outer
    # ``rs.surfaces`` and the ``commits=rs.history(...)`` it hands in on the
    # same line, and ``by_line`` kept only the second. The verdict for the
    # outer call was then never asserted -- ``_surfaces_window`` could be made
    # to return ``[]`` and this test still passed, while its docstring claimed
    # both were checked (s1262-rev, claunch-o5il6). A dict keyed by line is
    # only as good as the one-call-per-line assumption behind it.
    assert len(calls) == len(by_line), (
        f"two invocations share a line and one would go unchecked: {calls}"
    )

    bare = by_line[3]
    assert bare[1] is True and not _names_a_window(bare[0]), bare

    # commits=None is the tool's own default path: it falls through to history().
    explicit_none = by_line[6]
    assert explicit_none[1] is True and not _names_a_window(explicit_none[0]), explicit_none

    # The windowed case is now two lines, and both are asserted: the history
    # call that names the window, and the surfaces call handed that window.
    inner = by_line[9]
    assert inner[1] is True and _names_a_window(inner[0]), inner
    windowed = by_line[10]
    assert windowed[1] is True and _names_a_window(windowed[0]), windowed

    elsewhere = by_line[13]
    assert elsewhere[1] is False, elsewhere


def test_a_path_walked_from_this_file_names_this_checkout():
    """``Path(__file__).resolve().parents[1]`` from this file *is* this checkout.

    This module already writes ``_SELF = Path(__file__).resolve()``, so the
    idiom is right here to be copied -- and a sibling test walking up from
    ``tests/`` reaches the same directory. Not recognising it made the guard
    miss a call that reads this repository (s597-rev, claunch-o5il6).

    The flip side is asserted too, because recognising *any* ``parents[N]``
    would be the over-blocking direction: some other object's ancestry says
    nothing about this checkout, and a negative or computed index is not a
    claim the source makes."""
    here = _path_text(ast.parse("Path(__file__).resolve().parents[1]").body[0].value)
    assert here == _HERE, here

    # Without .resolve() the chain is the same directory: Path(__file__) needs
    # no resolution to have a parent.
    no_resolve = _path_text(ast.parse("Path(__file__).parents[1]").body[0].value)
    assert no_resolve == _HERE, no_resolve

    # parents[0] is this file's own directory -- still inside the checkout.
    own_dir = _path_text(ast.parse("Path(__file__).resolve().parents[0]").body[0].value)
    assert own_dir == str(_SELF.parent), own_dir
    assert _resolves_here(own_dir, _HERE) is True

    for unknown in (
        "Path(other).resolve().parents[1]",     # not this file
        "Path(__file__).resolve().parents[-1]",  # negative: which dir is a guess
        "Path(__file__).resolve().parents[n]",   # computed: same
        "Path(__file__).resolve().parents[1].parent",  # a further wrapper
    ):
        assert _path_text(ast.parse(unknown).body[0].value) is None, unknown

    sample = """
def test_a():
    _run("--repo", str(Path(__file__).resolve().parents[1]))

def test_b():
    rs.history(Path(__file__).resolve().parents[1])
"""
    calls = _tool_invocations(sample)
    assert [r for _n, _a, r in calls] == [True, True], calls


def test_the_other_spellings_of_walking_up_from_this_file_are_the_same_checkout():
    """Four more ways a test writes "the directory above me", all unrecognised
    before (claunch-o5il6.2).

    ``Path(__file__).resolve().parent.parent``, ``pathlib.Path(...).parents[1]``
    and ``Path(__file__).absolute().parents[1]`` each name this checkout
    exactly as ``parents[1]`` does. Falling through to ``None`` meant a call
    using them was parked as "cannot place this path" and exempted -- the
    under-blocking direction, the one where the guard's failure mode is a
    repository read nobody named a window for.

    The negative cases stay negative: a chain rooted anywhere but ``__file__``
    says nothing about this checkout, and reading it as if it did would be the
    over-blocking direction. Both directions are asserted because each alone
    can be satisfied by a guard that answers the same thing everywhere."""
    for spelling in (
        "Path(__file__).resolve().parent.parent",
        "Path(__file__).parent.parent",
        "Path(__file__).absolute().parent.parent",
        "Path(__file__).resolve().parents[1]",
        "pathlib.Path(__file__).resolve().parents[1]",
        "pathlib.Path(__file__).absolute().parents[1]",
    ):
        assert _path_text(ast.parse(spelling).body[0].value) == _HERE, spelling

    for unknown in (
        "Path(other).resolve().parent.parent",
        "pathlib.Path(other).resolve().parents[1]",
        "SOMEWHERE.resolve().parent.parent",
    ):
        assert _path_text(ast.parse(unknown).body[0].value) is None, unknown

    # A chain that walks above the checkout is read as the directory it names
    # -- and that directory is not this repository, so the call stays exempt.
    # Asserting ``None`` here would have been wrong: the source *does* say
    # which path it means, and the exemption comes from where that path is.
    above = _path_text(ast.parse("Path(__file__).resolve().parent.parent.parent.parent").body[0].value)
    # Four hops up from the file, with the same start the ``parents[N]``
    # spelling uses: ``parents[0]`` is this file's directory, so four hops is
    # ``parents[3]``.
    assert above == str(_SELF.parents[3]), above
    assert _resolves_here(above, _HERE) is False, above


def test_every_subprocess_entry_that_takes_an_argv_is_collected():
    """``check_output`` and ``Popen`` read history as surely as ``run`` does.

    Collecting only ``subprocess.run`` left a call through either of the other
    two entirely invisible -- not exempted, not seen (claunch-o5il6.2). The
    guard's question is "does this call read this repository without a
    window", and it is answered by the argv, not by which wrapper carries it.

    Asserted on a sample so the answer does not depend on this file happening
    to contain the shape; the live file has all three only if someone wrote
    them."""
    sample = """
def test_a():
    out = subprocess.run([sys.executable, str(SCRIPT), "--json"], cwd=str(ROOT))

def test_b():
    out = subprocess.check_output([sys.executable, str(SCRIPT)], cwd=str(ROOT))

def test_c():
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "--limit", "5"], cwd=str(ROOT))

def test_d():
    out = subprocess.check_output(["git", "log"], cwd=str(ROOT))
"""
    calls = _tool_invocations(sample)
    by_line = {n: (a, r) for n, a, r in calls}

    assert len(calls) == 3, calls
    assert by_line[3] == (["--json"], True), by_line[3]
    assert by_line[6] == ([], True), by_line[6]
    assert by_line[9] == (["--limit", "5"], True), by_line[9]
    # The git call names no tool, so it is not an invocation at all.
    assert 12 not in by_line, by_line


def test_the_tool_is_recognised_by_its_file_name_in_another_module():
    """A sibling test cannot say ``SCRIPT`` -- that name is this module's.

    So the file name is what identifies the tool there, and reading only the
    bare name ``SCRIPT`` meant a call from another test file matched nothing
    (claunch-o5il6.3). The spelling is matched on the path's final component,
    so ``tools/related_surfaces.py`` and an absolute subprocess path both
    count while ``related_surfaces_extra.py`` does not.

    ``os.path.join(ROOT, "tools", "related_surfaces.py")`` counts too, and it
    is written as an *expression* below rather than as a string: the walk sees
    the constant inside the call, which is the shape a real module would
    write. Quoting it into the argv as text would have tested a string literal
    and never touched the join."""
    for spelling in ('"tools/related_surfaces.py"', '"/abs/checkout/tools/related_surfaces.py"',
                     'os.path.join(ROOT, "tools", "related_surfaces.py")'):
        sample = f'out = subprocess.run([sys.executable, {spelling}], cwd=str(ROOT))\n'
        calls = _tool_invocations(sample)
        assert len(calls) == 1, (spelling, calls)
        assert calls[0][2] is True, (spelling, calls)

    near_miss = 'out = subprocess.run([sys.executable, "related_surfaces_extra.py"], cwd=str(ROOT))\n'
    assert _tool_invocations(near_miss) == [], _tool_invocations(near_miss)
    # The tool's own module is a near miss too: naming it in a comment or a
    # docstring is not calling it, and the file that defines the guard is not
    # an invocation of the tool.
    assert _tool_invocations('"""talks about related_surfaces.py"""\n') == []


def test_every_test_module_that_names_the_tool_is_parsed():
    """The guard reads the other test modules, not only itself.

    It used to ``ast.parse`` its own source and nothing else, so a call from
    another test file was outside the guard entirely -- not exempted, not
    seen (claunch-o5il6.3). This asserts the widening three ways: the list is
    non-empty, it includes this module, and it includes at least one *other*
    module, and it is not the whole suite (a thousand files parsed to find the
    same few mentions would be a cost with no coverage behind it).

    The sibling assertion is the one that makes this test about widening
    rather than about parsing. Without it, a guard that reverted to
    ``[_SELF]`` passed every other line here -- the list was non-empty, this
    module was in it, and one file is still fewer than the whole suite. Only
    "some file that is not me" separates the two."""
    sources = _every_test_source()
    assert sources, "no test module names the tool"
    assert _SELF in sources, [str(p) for p in sources]
    siblings = [p for p in sources if p != _SELF]
    assert siblings, (
        "no other test module names the tool, so nothing exercises the widening "
        "and this guard would pass while reading only itself -- either a sibling "
        "does name it (and this assertion should pass) or the widening is dead"
    )
    every_test = sorted((ROOT / "tests").rglob("*.py"))
    assert len(sources) < len(every_test), (len(sources), len(every_test))

    # And what it finds in another module is actually checked: a sibling that
    # reads this repository with no window must fail, one that names its
    # window must pass. Written as source text and parsed the way the live
    # guard parses a sibling, because importing a test module would run it.
    reading_bare = """
def test_sibling():
    out = subprocess.run([sys.executable, "tools/related_surfaces.py", "--json"], cwd=str(ROOT))
"""
    reading_windowed = """
def test_sibling():
    out = subprocess.run(
        [sys.executable, "tools/related_surfaces.py", "--limit", "100000"], cwd=str(ROOT))
"""
    bare = _tool_invocations(reading_bare)
    assert len(bare) == 1 and bare[0][2] is True and not _names_a_window(bare[0][1]), bare
    windowed = _tool_invocations(reading_windowed)
    assert len(windowed) == 1 and windowed[0][2] is True and _names_a_window(windowed[0][1]), windowed


def test_the_surfaces_sample_does_not_put_two_calls_on_one_line():
    """A ``by_line`` dict silently drops one of two calls sharing a line.

    The previous version of the surfaces test handed ``commits=rs.history(...)``
    inline, so the outer ``rs.surfaces`` and the inner ``rs.history`` both sat
    on one line and only the inner survived into ``by_line``. ``_surfaces_window``
    could then be reduced to ``return []`` with the test still green, while the
    docstring said both were checked (s1262-rev, claunch-o5il6). This is that
    guard: if a future edit inlines the sample again, this fails first and says
    so, rather than leaving one assertion quietly vacuous."""
    sample = """
def test_c():
    window = rs.history(ROOT, limit=50)
    rs.surfaces(ROOT, ["a.py"], commits=window)
"""
    calls = _tool_invocations(sample)
    lines = [n for n, _a, _r in calls]
    assert len(calls) == len(set(lines)), f"two calls on one line: {calls}"
    assert len(calls) == 2, calls


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
