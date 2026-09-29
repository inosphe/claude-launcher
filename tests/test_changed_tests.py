"""The worker's gate: the tests this branch's change can affect, and no more.

``improv-worker``'s review step has always said it runs a *simplified* suite
and that the full sweep belongs to the leader's batch. The command armed on
that step said otherwise: ``pytest tests -q -m "not worktree" -n 8`` selects
about 1450 of this suite's 1558 tests. The prose said "not the full sweep"
while the command ran 93% of it, six sessions ran it at once on leaving a
step, and a user eventually reported the obvious symptom -- workers keep
running the full suite.

``tools/changed_tests.py`` replaces it with a selection, by two rules that a
reader can check by eye: a changed test module runs, and a changed
``src``/``tools`` module pulls in its same-named test module if one exists.

The cases below are about the edges of that selection rather than its happy
path, because every one of them is a way the gate could quietly stop
covering something:

* uncommitted and untracked changes must count, or a round's brand-new test
  module -- which is untracked until it is added -- would not be run by the
  gate meant to check it;
* a deleted test module must not be selected, or the gate fails on a file
  that is gone on purpose;
* selecting nothing must pass, or prose-only rounds go red and the incentive
  becomes to fake a test edit.

The pytest invocation is built but not run: ``--list`` stops before that, and
what these tests are about is which files get chosen.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The gate resolves which checkout to measure through this module, and
# three cases below replace that lookup -- the same idiom, and the same
# reason, as tests/test_landed_check.py.
from claude_launcher.cflow import checkout

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "changed_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("changed_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


changed_tests = _load()


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


def _build_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    _write(repo, "src/pkg/mesh.py")
    _write(repo, "src/pkg/lonely.py")
    _write(repo, "tools/deploy_check.py")
    _write(repo, "tests/test_mesh.py")
    _write(repo, "tests/test_deploy_check.py")
    _write(repo, "tests/test_unrelated.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "branch", "-M", "master")
    _git(repo, "checkout", "-q", "-b", "feature")


@pytest.fixture
def repo(tmp_path, repo_template) -> Path:
    """A repository shaped like this one: ``src/``, ``tools/``, ``tests/``.

    ``master`` carries a source module and its test, so a branch cut from it
    can change either and the mapping has something to find.

    Copied from a template, then the index's stat cache is refreshed: the
    copy has new inodes and ctimes, and the tests below that pin the
    stat-cache rules (``_edit_the_stat_cache_cannot_see`` and friends) need
    an index that agrees with the files it describes, exactly as a fresh
    ``git init`` would leave it.
    """
    repo = repo_template("changed-tests", _build_repo, tmp_path / "repo")
    _git(repo, "update-index", "-q", "--refresh")
    return repo


def _tree_from_empty_index(repo: Path) -> str:
    """``worktree_tree``'s answer with no stat cache to trust at all.

    Every file is re-hashed, so this cannot be wrong for the reason
    ``worktree_tree`` can be wrong -- which is what makes it usable as the
    ground truth the fast path is checked against.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        subprocess.run(["git", "add", "-A"], cwd=str(repo), env=env, check=True)
        proc = subprocess.run(
            ["git", "write-tree"], cwd=str(repo), env=env,
            capture_output=True, text=True, check=True,
        )
        return proc.stdout.strip()


def _select(repo: Path) -> list:
    return changed_tests.select(repo, changed_tests.changed_paths(repo, "master"))


def _seed_on_base(repo: Path, files: dict) -> None:
    """Put files in the *base* commit, not in the branch's change.

    Rules 1 and 3a would otherwise be impossible to tell apart: a test module
    written as part of the round is selected by rule 1 no matter what it
    contains, so a case meaning to prove "3a found this by its content" would
    pass on a file 3a never looked at.
    """
    _git(repo, "checkout", "-q", "master")
    for rel, text in files.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "rebase", "-q", "master")


def test_a_changed_test_module_is_selected(repo):
    _write(repo, "tests/test_mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the test")
    assert _select(repo) == ["tests/test_mesh.py"]


def test_a_changed_source_module_pulls_in_its_test(repo):
    """The rule that keeps the gate meaningful when only source moved.

    Without it a worker who edits ``mesh.py`` and extends its test in the
    same round is covered, but one who edits only source is not -- and that
    is the change most likely to break something.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the source")
    assert _select(repo) == ["tests/test_mesh.py"]


def test_a_changed_tools_script_pulls_in_its_test(repo):
    """``tools/`` follows the same convention as ``src/`` and must be read
    the same way -- ``tools/deploy_check.py`` has ``tests/test_deploy_check.py``
    exactly as a source module would."""
    _write(repo, "tools/deploy_check.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the tool")
    assert _select(repo) == ["tests/test_deploy_check.py"]


def test_a_changed_yaml_pulls_in_every_test_that_names_it(repo):
    """Rule 3a, and the regression that forced it to be derived.

    Rule 3 was first a hand-kept list of the tests guarding the workflows.
    It was wrong on its first outing: it missed ``test_cflow_window``, whose
    assertion about the leader's sweep gate that same round had just broken.
    The gate passed the round; a full sweep found the failure.

    A test that pins a yaml file *names* it, in order to load it. So the
    relationship is already written in the test's own source and can be read
    out of it -- there is no list to forget to update when a fourth test
    starts pinning the same workflow.
    """
    _seed_on_base(
        repo,
        {
            "tests/test_alpha.py": 'load("improv-worker.yaml")\n',
            "tests/test_beta.py": "# also pins improv-worker here\n",
            "tests/test_elsewhere.py": "nothing to do with it\n",
            "src/claude_launcher/workflows/improv-worker.yaml": "name: w\n",
        },
    )
    _write(repo, "src/claude_launcher/workflows/improv-worker.yaml", "name: w2\n")
    _git(repo, "commit", "-qam", "edit a workflow")

    picked = _select(repo)
    assert "tests/test_alpha.py" in picked
    assert "tests/test_beta.py" in picked
    assert "tests/test_elsewhere.py" not in picked


def test_a_test_that_globs_the_directory_is_still_reached(repo):
    """Rule 3b, for the case 3a provably cannot see.

    ``tests/test_sync_project_layer.py`` discovers the overrides through
    ``sync.overrides()``, which globs the directory -- it never writes
    "improv-worker" anywhere. Grepping for the name will never find it, so
    this one stays in a table, and the table stays that small.
    """
    _write(repo, "tests/test_sync_project_layer.py", "names = sync.overrides()\n")
    _write(repo, ".claunch/workflows/improv-leader.yaml", "name: l\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qam", "edit the override")

    assert "tests/test_sync_project_layer.py" in _select(repo)


def test_a_listed_glob_discoverer_that_is_absent_is_not_selected(repo):
    """The table names files, and a named file may be absent in a checkout
    that predates it -- selecting it would fail the gate on its own table."""
    _write(repo, "src/claude_launcher/workflows/improv-worker.yaml", "name: w\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qam", "edit a workflow, no guards present")

    assert _select(repo) == []


def test_a_non_python_asset_with_a_cross_language_guard_is_reached(repo):
    """Rule 3b's second shape, reported by a session before it bit them.

    ``app.js`` has no python importer -- ``test_web_topology`` boots it
    against a stub browser and drives ~40 ``tests/web/*_check.js`` files. So
    rule 2 skips it (not python) and rule 3a will not grep a three-letter
    stem, which left a whole round of web work selecting *nothing* and the
    gate passing green having run zero tests.
    """
    _seed_on_base(
        repo,
        {
            "tests/test_web_topology.py": "boots the frontend\n",
            "src/claude_launcher/web/static/app.js": "// v1\n",
        },
    )
    _write(repo, "src/claude_launcher/web/static/app.js", "// v2\n")
    _git(repo, "commit", "-qam", "edit the frontend")

    assert "tests/test_web_topology.py" in _select(repo)


def test_a_path_no_rule_can_map_is_reported_not_silently_skipped(repo, capsys):
    """The distinction the whole gate rests on: empty is not the same as fine.

    A gate that answers "nothing guards this" and "I could not work out what
    guards this" with the same green exit teaches people that green means
    checked. So unmappable paths are named, with what to do next.

    On **stdout**, with the selection: a landing procedure that split the
    streams was reading stdout alone, which put this list where nobody looked
    -- and unread spells the same as absent, which is the thing this case
    exists to prevent. See :data:`changed_tests.STREAMS`.
    """
    _write(repo, "assets/logo.bin", "\x00\x01\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qam", "add an asset nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "assets/logo.bin" in out
    assert "not that they are fine" in out
    assert "EXPLICIT_GUARDS" in out  # says how to fix it, not just that


def test_the_search_term_is_a_filename_not_an_english_word():
    """Why ``needle`` exists, in the two cases that shaped it.

    A compound stem is a real reference: a test pinning ``improv-worker.yaml``
    writes ``improv-worker`` to load it. A plain word is not -- searching for
    ``whatever`` selected fourteen modules that merely used the word in a
    docstring, which is the full suite creeping back in by another route.
    """
    assert changed_tests.needle("improv-worker.yaml") == "improv-worker"
    assert changed_tests.needle("whatever.md") == "whatever.md"
    assert changed_tests.needle("app.js") == "app.js"       # too short to stand alone
    assert changed_tests.needle("style.css") == "style.css"  # a word, not a name


def test_a_very_short_stem_does_not_drag_in_the_whole_directory(repo):
    """Substring matching on a two-letter name would hit almost every file.

    Rule 3a widens on purpose, but widening to "everything" is just the full
    suite with extra steps -- which is the thing this script exists to stop.
    """
    _seed_on_base(
        repo,
        {
            "tests/test_alpha.py": "the letter a appears here\n",
            "docs/a.yaml": "x\n",
        },
    )
    _write(repo, "docs/a.yaml", "y\n")
    _git(repo, "commit", "-qam", "edit a short-named file")

    assert _select(repo) == []


def test_a_source_module_with_no_test_selects_nothing_rather_than_guessing(repo):
    """The convention holds for 47 of 107 modules here, so it has to be allowed
    to miss. Widening is the only thing it may do; inventing a filename that
    does not exist would make the gate fail on its own guess.

    Nothing imports ``lonely`` and nothing names it, so rules 2b and 2c are
    silent too -- which is the point: they widen where there is a written
    relationship to read, and stay quiet where there is none.
    """
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert _select(repo) == []


# ---------------------------------------------------------------- rule 2b/2c
#
# Rule 2 alone maps a source file to its same-named test and stops there.
# Four rounds measured what that misses -- :func:`changed_tests.importers`
# carries the table -- and these pin the shape of each one.


def test_a_module_with_no_twin_is_still_reached_by_whoever_imports_it(repo):
    """The ``cflow_clock`` case: no ``tests/test_cflow_clock.py`` exists, so
    rule 2 selected nothing, the gate ran nothing, and it exited 0. Ten test
    modules import that file."""
    _seed_on_base(repo, {"tests/test_lonely_guard.py": "from pkg.lonely import thing\n"})
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert _select(repo) == ["tests/test_lonely_guard.py"]


def test_a_module_with_a_twin_also_pulls_in_its_other_importers(repo):
    """The ``daemon/mesh`` case, and the only one of the four that cost
    something: the twin was selected and passed, while ``test_mesh_wire.py``
    -- which imports the same module and pins the strings it writes -- was
    not selected and held three real failures."""
    _seed_on_base(repo, {"tests/test_mesh_wire.py": "from pkg.mesh import send\n"})
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the source")
    assert _select(repo) == ["tests/test_mesh.py", "tests/test_mesh_wire.py"]


@pytest.mark.parametrize(
    "statement",
    [
        "from pkg.lonely import thing",
        "from pkg import lonely",
        "import pkg.lonely",
        "import pkg.lonely as short",
    ],
)
def test_every_spelling_of_the_import_reaches_the_module(repo, statement):
    """A test reaches a module by any of these and the dependency is the same.
    Recording only one spelling would make the selection depend on the
    author's habit, which is the naming convention's mistake again."""
    _seed_on_base(repo, {"tests/test_importer.py": statement + "\n"})
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert "tests/test_importer.py" in _select(repo)


def test_importing_a_sibling_is_not_importing_this_one(repo):
    """The negative half of 2b. A rule that selected every test importing
    anything from the package would pass every test above and be worthless."""
    _seed_on_base(repo, {"tests/test_sibling.py": "from pkg.mesh import send\n"})
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert "tests/test_sibling.py" not in _select(repo)


def test_the_import_graph_is_not_followed_past_the_test(repo):
    """2b reads what a test imports, not what those imports reach.

    The transitive closure was measured on this suite before the bound was
    chosen: it selects a median of 1054 of 2489 tests -- 42% of the suite for
    the median source module, and 25% or more for 92 of the 107 of them. That
    is the leader's full sweep wearing the worker's name, and it would end the
    only thing this gate is for. The cost of the bound is this test's subject:
    a test that reaches the changed module only through another module is
    genuinely missed, and that is a documented limit rather than an oversight.
    """
    _seed_on_base(
        repo,
        {
            "src/pkg/middle.py": "from pkg.lonely import thing\n",
            "tests/test_middle.py": "from pkg.middle import thing\n",
        },
    )
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert _select(repo) == []


def test_a_guard_that_names_the_file_without_importing_it_is_reached(repo):
    """Rule 2c. ``test_delivery_contract`` keys a table on
    ``("cli_sessions.py", "_cmd_send_keys")`` and imports nothing from it --
    an import graph alone cannot see that, and it is a real guard."""
    _seed_on_base(
        repo,
        {"tests/test_contract.py": 'PINS = {("cli_sessions.py", "_cmd_send"): "x"}\n'},
    )
    _write(repo, "src/pkg/cli_sessions.py", "x = 2\n")
    assert "tests/test_contract.py" in _select(repo)


def test_a_tools_script_is_reached_by_name_since_it_has_no_module_path(repo):
    """``tools/`` is not importable under a package name -- its tests load it
    with ``spec_from_file_location`` -- so 2b returns nothing for it by
    construction and 2c is what speaks."""
    _seed_on_base(repo, {"tests/test_runs_the_script.py": 'SCRIPT = "tools/merge_ready.py"\n'})
    _write(repo, "tools/merge_ready.py", "x = 2\n")
    assert "tests/test_runs_the_script.py" in _select(repo)


def test_a_package_init_is_reached_by_whoever_imports_the_package(repo):
    """``src/pkg/__init__.py`` is named by its *package*, which is what a test
    writes when it imports from it."""
    _seed_on_base(
        repo,
        {"src/pkg/__init__.py": "", "tests/test_pkg_user.py": "from pkg import mesh\n"},
    )
    _write(repo, "src/pkg/__init__.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the package init")
    assert "tests/test_pkg_user.py" in _select(repo)


def test_a_package_init_does_not_drag_in_every_test_that_defines_a_class(repo):
    """Why :func:`changed_tests._is_dunder` exists. 2c's search term for
    ``__init__.py`` is the stem ``__init__``, which is not a reference to
    anything -- in this repository it appears in 35 of 118 test modules, none
    of them about a three-line package init. 2b already holds that file's real
    dependents."""
    _seed_on_base(
        repo,
        {
            "src/pkg/__init__.py": "",
            "tests/test_says_init.py": "class C:\n    def __init__(self):\n        pass\n",
        },
    )
    _write(repo, "src/pkg/__init__.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the package init")
    assert "tests/test_says_init.py" not in _select(repo)


def test_a_test_module_that_will_not_parse_does_not_break_the_gate(repo):
    """This gate runs on working trees, so a half-typed file is a normal state
    for one. A parser that raised here would turn "somebody is mid-edit" into
    a gate that cannot run at all."""
    _seed_on_base(repo, {"tests/test_half_typed.py": "def broken(:\n"})
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert _select(repo) == []


def test_this_repository_reaches_the_clock_tests_that_have_no_twin():
    """The case the rule was written for, pinned against the real tree.

    ``CflowReminderSource`` lives in ``daemon/cflow_clock.py`` and its
    canonical test is ``tests/test_reminder_clock.py`` -- a name the
    convention cannot reach, because the clocks in that file are tested one
    class per module.
    """
    repo = Path(__file__).resolve().parents[1]
    assert not (repo / "tests" / "test_cflow_clock.py").exists(), (
        "the twin now exists, so this case no longer proves what it was "
        "written to prove -- pick another module with no same-named test"
    )
    picked = changed_tests.select(repo, ["src/claude_launcher/daemon/cflow_clock.py"])
    assert "tests/test_reminder_clock.py" in picked

    picked = changed_tests.select(
        repo, ["src/claude_launcher/daemon/session_reminder.py"]
    )
    assert "tests/test_reminder_clock.py" in picked


def test_uncommitted_and_untracked_changes_count(repo):
    """The state the gate actually runs in.

    The review step comes *before* the commit step, so at the moment this
    runs the round's work is usually still in the working tree -- and a test
    module written this round is untracked until somebody adds it. A gate
    that read only committed history would skip precisely the tests the
    round just wrote.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")          # tracked, uncommitted
    _write(repo, "tests/test_brand_new.py")             # untracked
    assert _select(repo) == ["tests/test_brand_new.py", "tests/test_mesh.py"]


def test_a_deleted_test_module_is_not_selected(repo):
    """Removing a test is a legitimate change; running the removed file is not.

    git reports the deletion as a changed path, so the selection has to check
    that what it picked still exists or the gate fails on the absence it was
    told about.
    """
    (repo / "tests" / "test_mesh.py").unlink()
    _git(repo, "commit", "-qam", "drop the test")
    assert _select(repo) == []


def test_an_unrelated_test_is_never_dragged_in(repo):
    """The negative half of the claim. If the selection quietly widened back
    toward the whole directory nothing above would fail -- this is what
    notices."""
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit the source")
    assert "tests/test_unrelated.py" not in _select(repo)


def test_a_base_that_moved_ahead_does_not_drag_in_its_commits(repo):
    """Selection is about *this branch's* change, measured from the merge base.

    master moves constantly here -- twice in the hour this was written. A
    two-dot diff would hand the worker every test touched by everyone else's
    landings since it branched, which is the full suite again by another
    route.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "my change")

    _git(repo, "checkout", "-q", "master")
    _write(repo, "tests/test_unrelated.py", "x = 99\n")
    _git(repo, "commit", "-qam", "somebody else's landing")
    _git(repo, "checkout", "-q", "feature")

    assert _select(repo) == ["tests/test_mesh.py"]


def test_selecting_nothing_passes(repo, capsys):
    """A prose-only round must not be red.

    One landed in this repository on the day this was written: three yaml
    files, no tests. Failing it would leave a worker two options, and the
    cheap one is to touch a test file so the gate lets go.
    """
    _write(repo, "README.md", "docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qam", "docs only")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    assert "no test modules map to this change" in capsys.readouterr().out


def test_the_gate_reports_what_it_chose(repo, capsys):
    """The worker has to copy this into its report, and the step now asks for
    the module list by name -- so the selection has to be printed, not just
    acted on."""
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "tests/test_mesh.py" in out
    assert "1 test module(s) selected" in out


def test_outside_a_repository_it_cannot_tell_rather_than_passing(tmp_path):
    """Green is the dangerous answer to a broken query: it would report "no
    tests to run" for a checkout it could not read at all."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert changed_tests.main(["--repo", str(plain)]) == changed_tests.CANNOT_TELL


# --------------------------------------------------------------------------- #
# The command it builds.
# --------------------------------------------------------------------------- #


def test_one_module_runs_serially(repo):
    """xdist costs a process spawn per worker, and this suite's wall clock is
    process spawns rather than CPU. Parallelising a single module is slower
    than not."""
    cmd = changed_tests.build_command(["tests/test_mesh.py"])
    assert "-n" not in cmd


def test_several_modules_run_parallel_but_bounded(repo):
    """Bounded well under the sweep's measured optimum of 8, because several
    of these run at once across sessions -- the mesh's normal state is six."""
    cmd = changed_tests.build_command([f"tests/test_{i}.py" for i in range(10)])
    assert cmd[cmd.index("-n") + 1] == str(changed_tests.MAX_WORKERS)
    assert changed_tests.MAX_WORKERS <= 4


def test_a_window_grant_sets_the_parallel_width(repo):
    cmd = changed_tests.apply_worker_advice(
        changed_tests.build_command([f"tests/test_{i}.py" for i in range(10)]), 6
    )
    assert cmd[cmd.index("-n") + 1] == "6"


def test_the_command_never_names_the_whole_test_directory():
    """The regression this whole file guards.

    ``pytest tests`` is one small edit away from every selection above, and
    it would restore exactly the behaviour that was reported: the worker
    running the full suite.
    """
    cmd = changed_tests.build_command(["tests/test_mesh.py"])
    assert "tests" not in cmd, (
        "the worker gate must name individual modules, never the test "
        "directory -- that is the full suite again"
    )
    assert "--no-sync" in cmd  # a gate does not re-resolve a live daemon's venv


# --------------------------------------------------------------------------- #
# The basetemp: one string doing three jobs.
# --------------------------------------------------------------------------- #
#
# The path passed to ``--basetemp`` is pytest's scratch root, it is the only
# thing in a process listing that says whose pytest that is, and -- because a
# cflow gate keeps no stdout -- its CreationTime and ``popen-gw*`` count are
# the only surviving record of when a dead run started and how wide it ran.
#
# pytest empties an explicit basetemp when the run starts, so those three only
# coexist if the name is unique per run. It used to be ``C:/t/<session>c``,
# fixed for the session's whole life, which meant a round's second gate erased
# the first's record at startup -- measured, and the reason these exist.


def test_basetemp_is_unique_per_run(monkeypatch):
    """The regression this section guards: a reused name is a lost timeline.

    pytest ``rm_rf``s an explicit basetemp at startup, so two runs on one name
    do not merely collide -- the later one destroys the earlier one's record
    of itself, and a gate has no stdout to fall back on.
    """
    monkeypatch.setenv("CLAUNCH_SESSION", "s99")
    first = changed_tests.session_basetemp("s99", now=1_000_000)
    later = changed_tests.session_basetemp("s99", now=1_000_061)
    assert first != later


def test_basetemp_still_says_whose_run_it_is(monkeypatch):
    """Generation must not cost identity: a process scan matches the prefix.

    Uniqueness and recognisability are different properties of the same
    string, and conflating them is what pinned the old name -- the session
    only ever needed to be *findable*, not the whole path to be stable.
    """
    path = changed_tests.session_basetemp("s99", now=1_000_000)
    assert "/s99c" in path
    assert path.startswith(changed_tests.basetemp_root().as_posix())


def test_basetemp_stays_under_the_measured_path_ceiling():
    """48 characters, measured, not guessed: xdist nests ``popen-gwN/`` under
    it and the transcript tests fold an absolute cwd back into a filename, so
    the path is ``2*basetemp+162`` against MAX_PATH -- 48 passes at 258, 49
    fails at 260. The generation suffix has to be spent out of that budget."""
    path = changed_tests.session_basetemp("s1234567890", now=1_000_000)
    assert len(path) <= 48, path


def test_a_pinned_basetemp_reaches_the_command():
    """A hand run needs to be able to name its own tree -- that is how the
    two runs of one round stay separable when one of them is not the gate."""
    cmd = changed_tests.build_command(
        ["tests/test_mesh.py"], basetemp="C:/t/pinned"
    )
    assert "--basetemp=C:/t/pinned" in cmd


def test_pruning_keeps_the_newest_and_leaves_other_sessions_alone(
    tmp_path, monkeypatch
):
    """Unique names mean nothing ever reclaims a directory -- with an explicit
    basetemp pytest skips its own end-of-session cleanup as well -- so this
    round added the reclaiming. Two properties matter more than the count:

    * the newest survive, because a *live* sibling run of the same session is
      always among them (a hand run overlapping a gate run is normal), and
    * another session's directories are that session's evidence. Deleting
      them would trade this bug for a worse one.
    """
    root = tmp_path / "t"  # conftest seeds tmp_path itself, so take a subdir
    root.mkdir()
    monkeypatch.setattr(changed_tests, "basetemp_root", lambda: root)
    for stamp in ("0101000001", "0101000002", "0101000003"):
        (root / f"s99c{stamp}").mkdir()
    (root / "s99c").mkdir()  # the pre-generation name, aged out by sorting
    theirs = root / "s98c0101000001"
    theirs.mkdir()

    dropped = changed_tests.prune_basetemps("s99", keep=2)

    survivors = sorted(p.name for p in root.iterdir())
    assert survivors == ["s98c0101000001", "s99c0101000002", "s99c0101000003"]
    assert theirs.exists(), "another session's timeline is not ours to delete"
    assert sorted(p.name for p in dropped) == ["s99c", "s99c0101000001"]


def test_pruning_survives_a_locked_directory(tmp_path, monkeypatch):
    """Housekeeping never decides a gate. A directory that cannot be removed
    is a live run or an open handle; going red over it would turn a lost temp
    tree into a lost round."""
    root = tmp_path / "t"
    root.mkdir()
    monkeypatch.setattr(changed_tests, "basetemp_root", lambda: root)
    for stamp in ("0101000001", "0101000002"):
        (root / f"s99c{stamp}").mkdir()

    def boom(path):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(changed_tests.shutil, "rmtree", boom)
    assert changed_tests.prune_basetemps("s99", keep=1) == []


def test_the_script_runs_as_a_script_and_its_exit_status_reaches_the_shell():
    """The verify line runs it this way, and only this path proves the file
    is executable and its exit code survives the shell."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(SCRIPT.parent), "--list"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1, 2)


# --------------------------------------------------------------------------- #
# receipts: the same tree is not judged twice
# --------------------------------------------------------------------------- #
@pytest.fixture
def gate(repo, tmp_path, monkeypatch):
    """Drive ``main`` with a stand-in for pytest, and count what it ran.

    The gate's *selection* is tested above; what is under test here is
    whether it runs at all, so the run itself is replaced by a one-line
    process. That keeps these cases at one spawn each instead of a real
    pytest, and it makes "did it run?" a fact on disk rather than an
    inference from a duration.
    """
    tally = tmp_path / "runs.txt"
    state = {"exit": 0}
    body = f"open(r{str(tally)!r}, 'a').write('x'); print('1 passed')"

    def stub(files, *, basetemp=None):
        return [sys.executable, "-c", f"{body}; raise SystemExit({state['exit']})"]

    monkeypatch.setattr(changed_tests, "build_command", stub)

    class Gate:
        receipts = tmp_path / "receipts"

        def __call__(self, *extra):
            return changed_tests.main(
                ["--repo", str(repo), "--base", "master",
                 "--receipts", str(self.receipts), *extra]
            )

        def runs(self):
            return len(tally.read_text()) if tally.exists() else 0

        def red(self):
            state["exit"] = 1

    return Gate()


def test_a_green_receipt_stops_the_second_run(repo, gate, capsys):
    """The measured waste, gone: same tree, same selection, no second run.

    s150 ran sixteen modules by hand in 578s and watched the round's gate run
    the same selection over the same tree again for 656s. This is that pair,
    in miniature.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")

    assert gate() == 0
    assert gate.runs() == 1

    assert gate() == 0
    assert gate.runs() == 1, "the second call ran the selection again"

    out = capsys.readouterr().out
    assert "not running" in out
    assert "1 passed" in out, "it must say what it stood on, not just that it did"


def test_an_edit_is_a_different_tree_and_gets_its_own_run(repo, gate):
    """Uncommitted work is inside the key, which is the whole point.

    A key that was ``HEAD^{tree}`` would answer for the commit and hand its
    verdict to every edit made on top of it -- silently, and green.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    _write(repo, "src/pkg/mesh.py", "x = 3\n")
    assert gate() == 0
    assert gate.runs() == 2


def test_a_red_receipt_is_not_reused(repo, gate):
    """A red verdict is recorded, and re-running is how it gets fixed.

    Recorded because a verdict that lives only in a terminal is one process
    death away from a re-measurement (``claunch-p5n``). Not reused because a
    fix loop needs the run -- and the moment the fix is typed the tree, and
    so the key, is different anyway.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    gate.red()
    assert gate() == 1
    assert gate() == 1
    assert gate.runs() == 2

    tree = changed_tests.worktree_tree(repo)
    files = changed_tests.select(repo, changed_tests.changed_paths(repo, "master"))
    path = changed_tests.receipt_path(repo, tree, files, gate.receipts)
    assert json.loads(path.read_text(encoding="utf-8"))["exit_code"] == 1


def test_a_corrupt_targeted_receipt_warns_instead_of_disappearing(repo, gate, capsys):
    """The changed_tests echo of the sweep defect.

    ``sweep``'s ``_newest_green`` used to swallow an unparseable receipt
    without a word, and this exact-path read did the same: a green verdict
    filed and then unreadable re-measured what had already been measured,
    with no way to see why. Now the read names the file, and the selection
    still runs (a receipt is evidence, not a promise).
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    assert gate.runs() == 1

    tree = changed_tests.worktree_tree(repo)
    files = changed_tests.select(repo, changed_tests.changed_paths(repo, "master"))
    changed_tests.receipt_path(repo, tree, files, gate.receipts).write_text(
        "{trunc", encoding="utf-8"
    )

    assert gate() == 0
    assert gate.runs() == 2, "the unreadable receipt must not be reused"
    err = capsys.readouterr().err
    assert "cannot be read" in err
    assert "tree" in err and "receipt" in err


def test_a_narrower_selection_does_not_answer_for_a_wider_one(repo, gate):
    """Same tree, fewer modules, is a different verdict (``claunch-p5n``).

    Forged by hand rather than produced, because producing it would mean
    finding two changes with the same tree and different selections -- which
    cannot happen, and that impossibility is exactly what makes a key without
    the selection in it look safe.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    tree = changed_tests.worktree_tree(repo)
    files = changed_tests.select(repo, changed_tests.changed_paths(repo, "master"))

    assert changed_tests.find_receipt(repo, tree, files, gate.receipts) is not None
    wider = sorted(files + ["tests/test_unrelated.py"])
    assert changed_tests.find_receipt(repo, tree, wider, gate.receipts) is None


def test_a_targeted_receipt_is_invisible_to_the_sweep_gate(repo, gate):
    """The false green this filing scheme exists to make impossible.

    ``sweep.find_receipt_by_tree`` globs the receipt directory and accepts any
    green receipt matching the tree. A three-module run filed there would be
    read by ``sweep.py check`` as a verdict about the *whole suite* -- green,
    for a suite nobody ran. A non-recursive glob cannot see a subdirectory,
    so the two kinds are separated by the filing rather than by care.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    tree = changed_tests.worktree_tree(repo)

    assert changed_tests.receipts_dir(repo, gate.receipts).is_dir()
    assert changed_tests.sweep.find_receipt_by_tree(repo, tree, gate.receipts) is None


def test_check_abstains_when_no_receipt_answers(repo, gate, capsys):
    """The reviewer's door, closed: no verdict is not a pass, and not a run.

    A peer-review responder is a different session, past its own intake scan,
    so its confirming run is load nobody counted -- which is where two
    sessions have already come away with no verdict at all. Abstaining is the
    cheap answer; running is the expensive one.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate("--check") == changed_tests.CANNOT_TELL
    assert gate.runs() == 0
    captured = capsys.readouterr()
    assert "abstain" in captured.out
    # On stdout, with the selection it is abstaining about, and with its own
    # green twin ("green receipt for tree ..."), which was always there. Split
    # by outcome, a procedure reading stdout alone saw a healthy-looking
    # selection line and exit 2 with no reason attached (merger-r5).
    assert "abstain" not in captured.err


@pytest.mark.parametrize(
    "rel",
    [
        "src/pkg/cli_sessions.py",                 # 2c
        "tools/merge_ready.py",                    # 2c, no module path
        "src/claude_launcher/workflows/flow-x.yaml",  # 3a
        "src/pkg/__main__.py",                     # 2c skips a dunder
        "tests/test_mesh.py",                      # 1: the module, not its readers
    ],
)
def test_map_one_selects_the_naming_tests_named_by_finds_except_for_a_test_module(repo, rel):
    """Rules 2c and 3a are :func:`changed_tests.named_by`, and
    ``tools/related_surfaces.py`` lists its answer as tests "the gate runs".
    While that tool searched with :func:`changed_tests.mentioning` on its own
    it also searched where the gate does not, and marked four modules the
    gate never selected (``claunch-wt12o.1.2``). This pins the one relation
    both depend on: every path but a test module selects all of
    :func:`named_by`, and a test module selects itself alone."""
    _seed_on_base(
        repo,
        {
            "src/pkg/__main__.py": "x = 1\n",
            "src/claude_launcher/workflows/flow-x.yaml": "name: flow-x\n",
            "tests/test_names_all.py": (
                'PINS = ["cli_sessions.py", "tools/merge_ready.py", "flow-x", "test_mesh"]\n'
                'if __name__ == "__main__":\n    pass\n'
            ),
        },
    )
    named = set(changed_tests.named_by(repo, rel))
    picked = set(changed_tests.map_one(repo, rel))
    if rel == "tests/test_mesh.py":
        assert named == {"tests/test_names_all.py"}
        assert picked == {rel}
    elif rel == "src/pkg/__main__.py":
        assert named == set()
        assert "tests/test_names_all.py" not in picked
    else:
        assert named == {"tests/test_names_all.py"}
        assert named <= picked


def test_the_selection_only_grows_as_changed_paths_are_added(repo):
    """Adding a changed path can never remove a test module from the selection.

    This is not decoration -- a session used it to decide what NOT to run.
    Asked whether a wide ``--base master`` selection would contain a module it
    had found in its own narrower one, worker-deploy answered from the
    structure instead of spending 220 seconds measuring: the changed-path set
    of the wider base is a superset, and the selection only ever unions, so
    the answer follows. That reasoning is now load-bearing, so the property it
    rests on is pinned here.

    The property lives in :func:`changed_tests.map_one` being *pure* -- one
    path in, a set out, no accumulator. Keep it that way: if it were ever
    given the running set to add into, :func:`changed_tests.select` would read
    exactly as it does today, four lines that look like a union, and this
    guarantee would be gone with nothing at the call site to show it. That is
    what this case is watching, and it is why the assertion is about subsets
    rather than about any particular module (observed by worker-deploy,
    2026-08-27).
    """
    _seed_on_base(
        repo,
        {
            "src/pkg/facade.py": "from . import deep\n",
            "src/pkg/deep.py": "x = 1\n",
            "tests/test_deep.py": "x = 1\n",
            "tests/test_facade_user.py": "from pkg import facade\n",
        },
    )
    # Two of these select tests/test_mesh.py -- the module by rule 2, the test
    # itself by rule 1. The overlap is the point: a union and a symmetric
    # difference are the same function until some module arrives twice, so a
    # case built only from disjoint paths cannot tell them apart. (Measured:
    # without this pair, a symmetric-difference mutant passed here.)
    every = [
        "src/pkg/mesh.py",
        "tests/test_mesh.py",
        "src/pkg/deep.py",
        "tools/deploy_check.py",
        "tests/test_unrelated.py",
        "assets/logo.bin",
    ]
    assert "tests/test_mesh.py" in changed_tests.map_one(repo, "src/pkg/mesh.py")
    assert "tests/test_mesh.py" in changed_tests.map_one(repo, "tests/test_mesh.py")
    whole = set(changed_tests.select(repo, every))
    for i in range(len(every)):
        fewer = every[:i] + every[i + 1:]
        assert set(changed_tests.select(repo, fewer)) <= whole, (
            f"dropping {every[i]} grew the selection"
        )
    for rel in every:
        assert set(changed_tests.select(repo, [rel])) <= whole


def test_a_base_it_cannot_resolve_says_so_on_stdout(repo, capsys):
    """The other exit-2 path, and the same rule.

    Here stdout would otherwise be *empty* -- the run stops before there is a
    selection to print -- so a reader keeping only stdout gets exit 2 and not
    one word about why.
    """
    assert changed_tests.main(
        ["--repo", str(repo), "--base", "no-such-ref-zzz", "--list"]
    ) == changed_tests.CANNOT_TELL
    captured = capsys.readouterr()
    assert "cannot tell" in captured.out
    assert "cannot tell" not in captured.err


def test_check_reads_a_green_receipt_without_running(repo, gate, capsys):
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    assert gate("--check") == 0
    assert gate.runs() == 1
    assert "green receipt" in capsys.readouterr().out


def test_no_reuse_runs_anyway(repo, gate):
    """An escape hatch, because a receipt is evidence and not a promise."""
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    assert gate("--no-reuse") == 0
    assert gate.runs() == 2


def test_an_untracked_file_is_in_the_tree_key(repo):
    """``changed_paths`` counts untracked files, so the key must too.

    A new test module is untracked by definition, and it is the file most
    likely to be the reason this round is being gated at all.
    """
    before = changed_tests.worktree_tree(repo)
    _write(repo, "tests/test_brand_new.py")
    assert changed_tests.worktree_tree(repo) != before


def _edit_git_cannot_see_by_stat(repo: Path, rel: str, text: str) -> None:
    """Edit ``rel`` so that git's stat cache says nothing happened.

    Three things have to line up, and every one of them happens by itself in
    the field. What is forged is only the *timing*, so that the case is a
    fact rather than a one-second window a test may or may not land inside:

    * same size -- ``x = 2`` for ``x = 3`` is the ordinary shape of a fix,
      and it is what makes size useless as a discriminator;
    * same mtime -- restored after the write, standing in for an edit that
      landed in the second the index last recorded the file in;
    * an index whose own mtime is that same instant -- an editor save
      followed by any git command produces exactly this.

    Git then compares size and whole-second mtime (``st_ino`` is 0 here and
    ``st_ctime`` is the creation time, so neither discriminates), finds both
    equal, and is entitled to skip reading the file. Its one guard is the
    racy-clean rule -- ``ce_mtime >= index_mtime`` forces a content re-read
    -- and the third bullet is what arms it. Everything sits 60 seconds in
    the past so that a copy stamped with ``now`` is unambiguously outside
    that window; the wall clock does not get a vote.

    ``add -A`` is here, and no ``git status``: the point is to make git
    *cache* this stat for the pre-edit content. ``status`` would be actively
    wrong -- it rewrites the index, smudging the racily-clean entry, and the
    case evaporates.

    Where git compares ctime or nanoseconds the edit is simply visible and
    the cases below pass on the direct path. They do not go flaky there;
    they go quiet.
    """
    _blind_edit(repo, rel, text, _blind_setup(repo, rel))


def _blind_setup(repo: Path, rel: str) -> int:
    """Park ``rel`` and the index at one instant in the past, and cache it.

    Split out from the edit because a case that wants the *gate* to be fooled
    has to be set up before the run it will reuse, not after: the stale
    answer is the tree of whatever content this cached, so the run being
    reused has to be a run of that same content. Do it in between and the
    stale answer accidentally differs from the receipt's key, the gate runs
    again for the wrong reason, and the case passes against broken code.
    Measured: that ordering passed 1 time in 1 against the unfixed tree.
    """
    path = repo / rel
    past = path.stat().st_mtime_ns - 60 * 10**9
    os.utime(path, ns=(past, past))
    _git(repo, "add", "-A")
    index = Path(_git(repo, "rev-parse", "--absolute-git-dir").strip()) / "index"
    os.utime(index, ns=(past, past))
    return past


def _blind_edit(repo: Path, rel: str, text: str, past: int) -> None:
    """The edit itself: same size, and the parked mtime put straight back."""
    path = repo / rel
    size = path.stat().st_size
    path.write_text(text, encoding="utf-8")
    assert path.stat().st_size == size, "the case needs the size to stay put"
    os.utime(path, ns=(past, past))


def test_an_edit_the_stat_cache_cannot_see_is_still_in_the_key(repo):
    """The key names content, not what the index remembers about content.

    ``worktree_tree`` copies the real index to keep its stat cache, and a
    stat cache is a licence to answer from ``lstat`` alone. Copied without
    its mtime, that licence has no expiry: git reads the cached blob and
    ``write-tree`` returns the tree the edit was made *on top of* -- which
    is ``HEAD^{tree}``, the one value the docstring says this function must
    never be. Measured that way 23 times in 24
    (``claunch-gate-receipt-key-mismatch-t6zp``).

    Pinned against a tree built from an *empty* index rather than only
    against ``HEAD^{tree}``: "not the stale answer" would also be satisfied
    by a different wrong answer.
    """
    head = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    _edit_git_cannot_see_by_stat(repo, "src/pkg/mesh.py", "x = 2\n")

    truth = _tree_from_empty_index(repo)
    assert truth != head, "the fixture no longer models an edit at all"
    assert changed_tests.worktree_tree(repo) == truth


def test_an_edit_the_stat_cache_cannot_see_does_not_reuse_the_green(repo, gate):
    """The consequence, stated where it bites: a green for a run nobody made.

    The tree key is the whole of the receipt's identity. If an edit can slip
    out of the key, the previous content's green receipt answers for content
    that was never run -- silently, and the gate reports 0. That is the
    false green the receipt scheme exists to make impossible, and it is
    worse than the failing lookup that led here: a mismatched key costs a
    second run, this costs the run itself.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    past = _blind_setup(repo, "src/pkg/mesh.py")

    assert gate() == 0
    assert gate.runs() == 1

    _blind_edit(repo, "src/pkg/mesh.py", "x = 3\n", past)

    assert gate() == 0
    assert gate.runs() == 2, "the previous content's receipt answered for this one"
def test_a_same_second_same_size_rewrite_still_changes_the_tree(repo):
    """The stat cache must never answer for content (claunch-vuta).

    A rewrite inside the cached stat's second, at the same size, leaves
    mtime-second and size matching the index exactly, and git calls the
    file clean without reading it -- the scratch tree then carries the
    PRE-edit blob, and a green verdict answers for the edit that
    replaced it. The mtimes are pinned with os.utime so the race does
    not have to happen for the hole to be tested.
    """
    target = repo / "src/pkg/mesh.py"
    _git(repo, "add", "-A")          # cache the committed content's stat
    cached = os.stat(target)
    target.write_text("x = 9\n")     # same size, different content
    os.utime(target, ns=(cached.st_atime_ns, cached.st_mtime_ns))
    index = repo / ".git" / "index"
    later = time.time() + 5          # and the index reads as newer than
    os.utime(index, (later, later))  # the entry, so nothing is racy
    time.sleep(1.1)                  # the scratch copy is made past the
                                     # second boundary: without the fix
                                     # this is stale EVERY run, not just
                                     # when the clock happens to flip

    tree = changed_tests.worktree_tree(repo)
    assert tree != _git(repo, "rev-parse", "HEAD^{tree}").strip()


def test_a_restore_that_preserves_mtime_still_changes_the_tree(repo):
    """The same hole, reached without touching a single timestamp by hand.

    git protects the ordinary same-second rewrite itself: writing the
    index smudges a racily-clean entry's cached size to 0, so it is
    re-read forever after. Preserving the scratch copy's mtime keeps that
    guard armed and is worth doing -- but the guard only fires while the
    entry is racy, and any restore that carries the old mtime (cp -p,
    tar -x, rsync -t, unzip -- here shutil.copystat) puts stale content
    behind a non-racy entry. Nothing below fabricates a state git would
    not write: add, commit, status, diff, and a file copy.
    """
    target = repo / "src/pkg/mesh.py"
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    backup = repo.parent / "mesh.py.backup"
    shutil.copy2(target, backup)     # any mtime-preserving tool

    time.sleep(1.1)
    _git(repo, "status", "--porcelain")   # rehashes the entry and rewrites
    _git(repo, "diff", "--name-only", "HEAD")   # the index a second later,
                                                # so nothing is racy now
    target.write_text("x = 9\n")     # same size, different content,
    shutil.copystat(backup, target)  # restored under the old mtime

    tree = changed_tests.worktree_tree(repo)
    assert tree != head_tree, (
        "git status calls this tree clean; the gate must not"
    )


def test_the_scratch_index_leaves_the_real_one_alone(repo):
    """Keying the tree must not stage the worker's files under them."""
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    changed_tests.worktree_tree(repo)
    assert _git(repo, "diff", "--cached", "--name-only").strip() == ""


def test_a_repository_that_cannot_be_hashed_still_runs(repo, gate, monkeypatch):
    """Receipts are an optimisation; losing them must not lose the gate.

    The failure mode being refused is a gate that answers "cannot tell"
    because its bookkeeping broke -- which would be a worse gate than the one
    that had no bookkeeping at all.
    """
    def boom(_repo):
        raise LookupError("no git here")

    monkeypatch.setattr(changed_tests, "worktree_tree", boom)
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    assert gate.runs() == 1
    assert gate() == 0
    assert gate.runs() == 2


# ------------------------------------------------- reported, never silent (a9t)
#
# ``unmapped`` used to skip every ``.py`` on the grounds that rules 1 and 2
# owned python. Rule 2 is a naming convention that holds for 48 of this
# repository's 102 source modules, so where it was absent the path was
# selected by nothing *and* reported by nothing. These pin the half that was
# missing: a path either names test modules, or it is named.


def test_a_source_module_no_rule_can_map_is_reported_too(repo, capsys):
    """The gap ``claunch-uf7m`` landed a red batch through.

    ``src/pkg/lonely.py`` has no same-named test, nothing imports it and
    nothing names it -- so the selection is empty, which the gate is allowed
    to call a pass. What it is not allowed to do is stay quiet about which
    path it could not place.
    """
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "src/pkg/lonely.py" in out
    assert "not that they are fine" in out


def test_a_source_module_something_does_guard_is_not_reported(repo, capsys):
    """The other direction, or the report is noise and stops being read."""
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with a twin")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "tests/test_mesh.py" in out
    assert "map to no test module" not in out


def test_a_tools_script_nothing_guards_is_reported(repo, capsys):
    """``tools/*.py`` has no importable module name, so rule 2b is silent for
    it by construction and 2c is all it has. That makes it the likeliest
    shape to go unplaced, not the least."""
    _write(repo, "tools/orphan_tool.py", "x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a tools script nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    assert "tools/orphan_tool.py" in capsys.readouterr().out


def test_a_deleted_test_module_is_not_reported_as_unplaced(repo, capsys):
    """Rule 1 answered for it; the file is simply gone. "Grep for what guards
    it" is not advice about a file the round deleted on purpose."""
    (repo / "tests" / "test_mesh.py").unlink()
    _git(repo, "commit", "-qam", "drop the test")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    assert "map to no test module" not in capsys.readouterr().out


def test_the_report_says_which_relations_it_searched_for(repo, capsys):
    """"Nothing maps to it" is not a size until you know what was swept for.

    A path no *import* reaches is a different statement from one no test
    *names*, and the fix differs. Required of the landing request as of this
    round (``claunch-uf7m``, raised by s181).
    """
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "searched:" in out
    assert "same-named test (tests/test_lonely.py)" in out
    assert "direct import by a test" in out


# ------------------------------------------- a healthy-looking selection (a9t)
#
# Every device above asks whether the selection came back EMPTY. The failure
# that actually landed a red batch is the other one: nine modules selected,
# and the module that guards the changed behaviour is not among them because
# it reaches the file one import hop away. ``reached_indirectly`` names those
# without running them.


def _named_as_missed(out: str) -> list:
    """The ``<test> <- <changed path> via <module>`` lines, and only those.

    The note is printed before the selection, so "everything after the header"
    also swallows the selected modules -- which would make a test of "this one
    is NOT in the note" pass on a note that never mentioned it and fail on one
    that did not either.
    """
    return [line for line in out.splitlines() if "<-" in line and " via " in line]


def _facade(repo: Path, extra: dict = None) -> None:
    """``deep`` <- ``facade`` <- a test. The shape of ``cli_sessions``.

    ``tests/test_deep.py`` exists so the selection is *not* empty when
    ``deep.py`` changes -- which is the whole point: this failure hides
    behind a selection that looks like it worked.
    """
    files = {
        "src/pkg/deep.py": "x = 1\n",
        "src/pkg/facade.py": "from . import deep\n",
        "tests/test_deep.py": "x = 1\n",
        "tests/test_facade_user.py": "from pkg import facade\n",
    }
    files.update(extra or {})
    _seed_on_base(repo, files)
    _write(repo, "src/pkg/deep.py", "x = 2\n")
    _git(repo, "commit", "-qam", "change the module behind the facade")


def test_a_guard_one_hop_out_is_named_when_the_selection_looks_healthy(repo, capsys):
    """``bd87fdc`` in miniature.

    ``test_daemon_wedge`` writes ``from claude_launcher import cli`` and
    ``cli.py`` is what imports ``cli_sessions``; the round that changed
    ``cli_sessions`` selected nine modules, none of them that one, and shipped
    three red modules two batches deep before a bisect found them.
    """
    _facade(repo)

    assert _select(repo) == ["tests/test_deep.py"]      # not empty: looks fine
    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "tests/test_facade_user.py" in out
    assert "were NOT selected" in out
    assert "via pkg.facade" in out                      # the relation, checkable


def test_a_module_the_selection_already_has_is_not_listed_as_a_miss(repo, capsys):
    """A test that imports the changed module directly is rule 2b's, and
    naming it again as "not selected" would be false."""
    _facade(repo, {"tests/test_direct.py": "from pkg import deep\n"})

    assert "tests/test_direct.py" in _select(repo)
    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    named = _named_as_missed(capsys.readouterr().out)
    assert not any("test_direct.py" in line for line in named)
    assert any("test_facade_user.py" in line for line in named)


def test_the_indirect_list_is_whole_rather_than_capped(repo, capsys):
    """A truncated list reads as a complete one. If the number is large that
    is the finding -- so the count and the lines have to agree."""
    extra = {f"tests/test_reader{i}.py": "from pkg import facade\n" for i in range(12)}
    _facade(repo, extra)

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    named = _named_as_missed(out)
    assert len(named) == 13                            # 12 readers + the original
    assert f"NOTE: {len(named)} test module(s)" in out


def test_both_caveats_are_stated_even_when_they_are_empty(repo, capsys):
    """A clean run says both caveats in words rather than by printing nothing.

    An absent block cannot be quoted, and it carries two readings a landing
    request has to keep apart: "swept, found nothing" and "this build of the
    tool has no such check". Removing that ambiguity is the whole reason the
    two caveats exist, so they must not reintroduce it in the common case.
    This session hit it from the reader's side -- asked for the two lines, it
    paraphrased the absent blocks to merger-r5 as if they were tool output
    (2026-08-27).

    The earlier rule here, that the note is absent rather than empty, is
    reversed on purpose. What it was protecting -- a block people learn to
    scroll past -- is answered by keeping the empty form to one line while the
    loud form stays a multi-line block with a filename in it.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with a twin and no facade")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    out = capsys.readouterr().out
    assert "map to at least one test module." in out
    assert "no test module sits one import hop outside this selection." in out
    assert "WARNING" not in out
    assert "were NOT selected" not in out


# ------------------------------------------------------------------ streams
#
# A landing procedure split the streams to avoid a pipe, read ``out.txt``
# alone, and treated ``err.txt`` as discardable -- reasonably, since the only
# thing that had ever been in it was uv's VIRTUAL_ENV line. Both of this
# gate's caveats were on the discarded side. Unread and absent spell the same.


def test_the_caveats_go_where_the_verdict_goes(repo, capsys):
    """Both blocks on stdout, because a reader who keeps only stdout must not
    read their contents as "none" (merger-r5, 2026-08-27)."""
    _facade(repo)
    _write(repo, "assets/logo.bin", "\x00\x01\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "and an asset nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    captured = capsys.readouterr()
    assert "map to no test module" in captured.out
    assert "were NOT selected" in captured.out
    assert "map to no test module" not in captured.err
    assert "were NOT selected" not in captured.err
    # One form per caveat per run. The positive line stands in for the
    # block, so printing both would put a claim next to the
    # counterexample that contradicts it.
    assert "map to at least one test module" not in captured.out
    assert "no test module sits one import hop outside" not in captured.out


# ------------------------------------------- the runner's own scratch (62yg)


def test_the_runners_own_lock_file_is_not_a_changed_path(repo):
    """``uv run`` is how every gate here is invoked and it writes a zero-byte
    ``uv-<hash>.lock`` into the project root. Counting it made the gate warn
    about a file its own launcher had just created, on a checkout ``git
    status`` calls clean. Measured by worker-64hs: 4 changed paths under ``uv
    run`` against 3 under the interpreter directly."""
    _write(repo, "uv-ae5a39a361c97824.lock", "")
    assert changed_tests.changed_paths(repo, "master") == []


def test_an_untracked_file_that_is_not_the_lock_still_counts(repo):
    """The exclusion is one shape at the root and nothing more. A test module
    written this round is untracked until it is added, and running it is the
    reason untracked files are in the change set at all."""
    _write(repo, "tests/test_brand_new.py", "x = 1\n")
    _write(repo, "uv-ae5a39a361c97824.lock", "")
    _write(repo, "src/pkg/uv-notahash.lock", "")
    _write(repo, "uv-metadata.txt", "")

    paths = changed_paths_sorted = changed_tests.changed_paths(repo, "master")
    assert "tests/test_brand_new.py" in paths
    assert "src/pkg/uv-notahash.lock" in paths          # not at the root
    assert "uv-metadata.txt" in paths                   # not a .lock
    assert "uv-ae5a39a361c97824.lock" not in changed_paths_sorted


# ------------------------------------------------ the base a worker is on (eghh)


@pytest.fixture
def stacked(repo) -> Path:
    """A worker branch on an integration branch on master -- this formation.

    ``master`` -> ``integration`` (two other workers' landed batch) ->
    ``worker`` (one file). Measured against master the worker's change reads
    as the whole batch, which is what three sessions measured at 78%, 80% and
    1945 tests in 221s on one day.
    """
    _git(repo, "checkout", "-q", "master")
    _git(repo, "checkout", "-q", "-b", "integration")
    _write(repo, "src/pkg/batch_one.py")
    _write(repo, "tests/test_batch_one.py")
    _write(repo, "src/pkg/batch_two.py")
    _write(repo, "tests/test_batch_two.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two other workers, already landed")
    _git(repo, "checkout", "-q", "-b", "worker")
    _write(repo, "src/pkg/mesh.py", "mine = 1\n")
    _git(repo, "commit", "-qam", "my one file")
    return repo


def test_base_auto_measures_against_the_branch_this_one_integrates_into(stacked):
    """Both numbers, side by side -- the control's true value is not zero.

    Against master the selection is the batch; against the upstream it is this
    round. Git already records which branch that is, under the name
    ``tools/merge_ready.py`` reads for the same question.
    """
    against_master = changed_tests.select(
        stacked, changed_tests.changed_paths(stacked, "master")
    )
    assert against_master == [
        "tests/test_batch_one.py",
        "tests/test_batch_two.py",
        "tests/test_mesh.py",
    ]

    _git(stacked, "branch", "--set-upstream-to=integration", "worker")
    base, how = changed_tests.resolve_base(stacked, changed_tests.BASE_AUTO)
    assert (base, how) == ("integration", "upstream")
    assert changed_tests.changed_paths(stacked, base) == ["src/pkg/mesh.py"]
    assert changed_tests.select(stacked, changed_tests.changed_paths(stacked, base)) == [
        "tests/test_mesh.py"
    ]


def test_base_auto_with_no_upstream_falls_back_and_names_the_axis(stacked, capsys):
    """Falling back to master is right for a branch cut from master and is the
    original defect for a stacked one, and nothing here can tell those apart.
    So it falls back, says which axis it used, and names the one command that
    settles it -- without changing the exit code, because a missing upstream
    is a thing to fix and not a reason to refuse a verdict."""
    assert changed_tests.resolve_base(stacked, changed_tests.BASE_AUTO) == (
        "master",
        "no-upstream",
    )
    assert changed_tests.main(["--repo", str(stacked), "--base", "auto", "--list"]) == 0
    out = capsys.readouterr().out
    assert "no upstream" in out
    assert "--set-upstream-to" in out


def test_the_output_names_the_axis_it_measured_on(stacked, capsys):
    """A number is only readable next to its axis, and this tool's axis moved."""
    _git(stacked, "branch", "--set-upstream-to=integration", "worker")
    assert changed_tests.main(["--repo", str(stacked), "--base", "auto", "--list"]) == 0
    assert "vs integration (upstream)" in capsys.readouterr().out


def test_an_explicit_base_is_left_exactly_as_given(stacked):
    """``auto`` is opt-in. Every other caller keeps the ref it passed."""
    assert changed_tests.resolve_base(stacked, "master") == ("master", "given")
    assert changed_tests.resolve_base(stacked, "integration") == (
        "integration",
        "given",
    )


# --------------------------------- an upstream that is only a push target (d4yo)


@pytest.fixture
def pushed(repo) -> Path:
    """``master``, tracking an ``origin/master`` that is behind it.

    The shape a session on the root checkout stands in: the branch has an
    upstream, so ``--base auto`` finds one, but it names this same branch on a
    remote rather than a branch to integrate into. Nothing here has been
    pushed, so ``origin/master`` sits at the first commit while master carries
    two more. Nothing is ever transferred: ``git remote add`` is here for the
    fetch refspec, without which ``@{upstream}`` cannot map ``refs/heads/master``
    on ``origin`` to the tracking ref and answers "no upstream" instead.
    """
    _git(repo, "checkout", "-q", "master")
    old = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "update-ref", "refs/remotes/origin/master", old)
    _git(repo, "config", "branch.master.remote", "origin")
    _git(repo, "config", "branch.master.merge", "refs/heads/master")

    _write(repo, "src/pkg/batch_one.py")
    _write(repo, "tests/test_batch_one.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "another session's landed work")
    _write(repo, "tools/deploy_check.py", "changed = 1\n")
    _git(repo, "commit", "-qam", "and another")
    return repo


def test_base_auto_does_not_measure_against_the_branchs_own_remote_copy(pushed):
    """The defect, and the control next to it -- the unpushed work is not mine.

    ``origin/master`` is where master is pushed, not a branch master
    integrates into, so measuring against it reads everything unpushed as this
    round's change. On the real repository that was 102 paths -> 114 modules
    for a session that had committed nothing (``claunch-d4yo``).
    """
    against_the_remote_copy = changed_tests.select(
        pushed, changed_tests.changed_paths(pushed, "origin/master")
    )
    assert against_the_remote_copy == [
        "tests/test_batch_one.py",
        "tests/test_deploy_check.py",
    ]

    base, how = changed_tests.resolve_base(pushed, changed_tests.BASE_AUTO)
    assert (base, how) == ("master", "self-tracking")
    assert changed_tests.changed_paths(pushed, base) == []
    assert changed_tests.select(pushed, changed_tests.changed_paths(pushed, base)) == []


def test_falling_back_off_a_self_tracking_upstream_says_so_and_still_passes(
    pushed, capsys
):
    """Loud for the same reason ``no-upstream`` is loud.

    The selection line would otherwise read ``vs master`` with nothing to say
    that ``auto`` was asked at all, and an empty selection is a pass the step
    reports on -- so the exit code does not move.
    """
    assert changed_tests.main(["--repo", str(pushed), "--base", "auto", "--list"]) == 0
    out = capsys.readouterr().out
    assert "own remote copy" in out
    assert "--set-upstream-to" in out
    assert "no test modules map to this change" in out


def test_an_upstream_naming_another_branch_holds_even_when_it_is_an_ancestor(stacked):
    """Why the test is ancestry-free: a parent being an ancestor is the stack.

    Three real branches in this repository sit exactly here --
    ``s127-7w7g-gate-dirty``, ``s127-qj03-doc-body`` and ``s217-wf-followup``
    -- so a rule that fell back to master whenever the upstream was an
    ancestor of HEAD would put all three back on the base ``--base auto``
    exists to keep them off.
    """
    _git(stacked, "checkout", "-q", "integration")
    _git(stacked, "merge", "-q", "--ff-only", "worker")
    _git(stacked, "checkout", "-q", "worker")
    _git(stacked, "branch", "--set-upstream-to=integration", "worker")
    assert _git(stacked, "merge-base", "--is-ancestor", "integration", "worker") == ""

    assert changed_tests.resolve_base(stacked, changed_tests.BASE_AUTO) == (
        "integration",
        "upstream",
    )


def test_a_worker_branch_tracking_a_differently_named_remote_ref_is_untouched(repo):
    """The edge of the rule, stated so it is not read as wider than it is.

    ``branch.<X>.merge`` is compared against ``refs/heads/<X>``, so only a
    branch paired with its own name falls back. A worker branch pointed at
    ``origin/master`` names a different branch and keeps that axis, stale or
    not -- this rule is about what an upstream *means*, not about how old one
    is.
    """
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "update-ref", "refs/remotes/origin/master",
         _git(repo, "rev-parse", "master").strip())
    _git(repo, "config", "branch.feature.remote", "origin")
    _git(repo, "config", "branch.feature.merge", "refs/heads/master")

    assert changed_tests.resolve_base(repo, changed_tests.BASE_AUTO) == (
        "origin/master",
        "upstream",
    )


def test_the_board_is_not_a_change_this_branch_made(repo):
    """``.beads`` is tracked, is written by every session, and guards nothing.

    On the root checkout it is uncommitted essentially always, and it maps to
    four modules for a round that committed nothing. The count is not even
    stable -- one tree read 113 then 114 because another session wrote an
    issue in between (``claunch-d4yo``). Subtracted with the set the other two
    gates already subtract, so one rule covers all three.
    """
    _write(repo, ".beads/issues.jsonl", '{"id": "x"}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "board exists and is tracked")
    _write(repo, ".beads/issues.jsonl", '{"id": "x"}\n{"id": "another session"}\n')

    assert ".beads" in changed_tests.sweep.NON_CODE_ENTRIES
    assert changed_tests.changed_paths(repo, "master") == []

    _write(repo, "src/pkg/mesh.py", "mine = 1\n")
    assert changed_tests.changed_paths(repo, "master") == ["src/pkg/mesh.py"]


def test_the_code_that_handles_the_board_is_not_dropped_with_it(repo):
    """The first question anyone asks of the rule above, pinned rather than answered.

    What comes off is the data under ``.beads/``. A source file that *handles*
    the board has a first path component of ``src`` or ``tools``, so it never
    meets the rule and maps as it always did. Measured on this repository:
    ``src/claude_launcher/daemon/beads.py`` selects ``test_beads_daemon``,
    ``test_beads_protocol`` and ``test_reports``; ``tools/sweep.py`` selects
    eight. A round editing either still gets its gate.
    """
    _write(repo, "src/pkg/beads.py", "board = 1\n")
    _write(repo, "tests/test_beads.py", "from pkg import beads\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "code that handles the board")
    _write(repo, ".beads/issues.jsonl", '{"id": "someone else"}\n')
    _write(repo, "src/pkg/beads.py", "board = 2\n")

    paths = changed_tests.changed_paths(repo, "master")
    assert ".beads/issues.jsonl" not in paths       # the data comes off
    assert "src/pkg/beads.py" in paths              # the code does not
    assert "tests/test_beads.py" in changed_tests.select(repo, paths)


def test_a_path_no_rule_recognises_is_still_code(repo):
    """Subtraction, not selection -- the direction that fails towards running.

    Only the entries named in ``NON_CODE_ENTRIES`` come off. A top-level name
    that merely looks like data keeps its place in the changed set, because
    the cost of running a module nobody needed is one module and the cost of
    skipping the one that guarded the change is a red landing.
    """
    _write(repo, ".beadsdata/issues.jsonl", "{}\n")
    _write(repo, "notes.jsonl", "{}\n")
    assert changed_tests.changed_paths(repo, "master") == [
        ".beadsdata/issues.jsonl",
        "notes.jsonl",
    ]


# --------------------------------------------------------------------------- #
# which tree the gate measures (claunch-7sj, tree axis)
#
# The engine runs a step's verify with cwd set to the RUN's directory, and
# that is not this session's own tree whenever the run was keyed somewhere the
# session does not stand. `landed_check.py` and `merge_ready.py` already route
# the question through `own_checkout`; this gate kept `--repo` defaulting to
# `Path(".")`, so the diff, the selection and the suite were all about
# whatever tree the run happened to be keyed to -- and it reported green about
# it.
# --------------------------------------------------------------------------- #
def test_the_run_directory_is_not_taken_for_the_sessions_tree(
    repo, tmp_path, monkeypatch, capsys
):
    """The rescue: with no ``--repo``, the gate asks where this session stands.

    The working directory here is deliberately NOT the repository, because
    that is the shape the defect needs: before the lookup, the gate measured
    this empty directory instead.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    monkeypatch.setattr(
        checkout, "own_checkout", lambda *a, **k: (str(repo), checkout.SESSION)
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert changed_tests.main(["--base", "master", "--list"]) == 0
    out = capsys.readouterr().out
    assert str(repo) in out, "the tree it measured has to be in the output"
    assert "(session)" in out, "and how it came to think so"
    assert "tests/test_mesh.py" in out


def test_an_explicit_repo_survives_the_lookup_failing(repo, monkeypatch, capsys):
    """``--repo`` may not depend on a daemon.

    The case where somebody reaches for it is the case where the machine
    could not work the tree out by itself, so the fallback has to keep the
    named directory rather than the working one.
    """

    def boom(*a, **k):
        raise RuntimeError("no daemon")

    monkeypatch.setattr(checkout, "own_checkout", boom)
    assert changed_tests.main(
        ["--repo", str(repo), "--base", "master", "--list"]
    ) == 0
    out = capsys.readouterr().out
    assert str(repo) in out
    assert "(named)" in out


def test_with_no_lookup_the_working_directory_is_still_the_answer(
    repo, monkeypatch, capsys
):
    """Degrades in one direction only: no package, no daemon and no managed
    session all fall back to what this gate always did."""

    def boom(*a, **k):
        raise RuntimeError("no package")

    monkeypatch.setattr(checkout, "own_checkout", boom)
    monkeypatch.chdir(repo)
    assert changed_tests.main(["--base", "master", "--list"]) == 0
    assert "(run cwd)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# which pytest would run (claunch-7sj, environment axis)
#
# `uv run --no-sync pytest` resolves a console script: the project
# environment's Scripts/bin first, then the ambient PATH. A worktree whose
# .venv was never built therefore runs ANOTHER checkout's pytest and says
# nothing about it. Measured in this repository on 2026-09-22: a worker
# worktree holding no pytest of its own answered `pytest 9.1.1` out of
# `F:\works\claude-launcher\.venv\Scripts\pytest.EXE`.
# --------------------------------------------------------------------------- #
class _Probe:
    """A stand-in for the one subprocess ``foreign_pytest`` runs."""

    def __init__(self, stdout: str, code: int = 0):
        self.returncode = code
        self.stdout = stdout
        self.stderr = ""


def _answers(monkeypatch, stdout: str, code: int = 0) -> list:
    """Make the probe answer ``stdout``; return the list of calls it saw."""
    seen: list = []

    def fake(cmd, **kw):
        seen.append((cmd, kw))
        return _Probe(stdout, code)

    monkeypatch.setattr(changed_tests.subprocess, "run", fake)
    return seen


UV_COMMAND = ["uv", "run", "--no-sync", "pytest", "tests/test_mesh.py", "-q"]


def test_a_command_that_is_not_uv_has_no_console_script_to_resolve(
    tmp_path, monkeypatch
):
    """The decision that keeps this check off every other caller's back.

    A command that is not ``uv run`` resolves nothing through uv, so there is
    nothing to answer, and a caller substituting its own command is not
    measured against a resolution it never asked for.
    """
    seen = _answers(monkeypatch, "")
    assert changed_tests.foreign_pytest(tmp_path, [sys.executable, "-c", "x"]) is None
    assert seen == [], "it must not even ask"


def test_a_pytest_from_another_checkout_is_named_and_refused(tmp_path, monkeypatch):
    """The finding, in the words the operator needs in order to act on it."""
    stranger = tmp_path / "other" / ".venv" / "Scripts" / "pytest.exe"
    stranger.parent.mkdir(parents=True)
    stranger.write_text("")
    here = tmp_path / "mine"
    here.mkdir()
    _answers(monkeypatch, str(stranger) + "\n")

    note = changed_tests.foreign_pytest(here, UV_COMMAND)
    assert note is not None
    assert str(stranger) in note, "which binary would have run"
    assert str(here) in note, "and which tree it does not belong to"
    assert "uv sync --extra test" in note, "the one action that fixes it"
    assert "claunch-7sj" in note


def test_the_trees_own_pytest_is_not_a_finding(tmp_path, monkeypatch):
    """The ordinary case has to stay silent, or the check is a wall."""
    here = tmp_path / "mine"
    mine = here / ".venv" / "Scripts" / "pytest.exe"
    mine.parent.mkdir(parents=True)
    mine.write_text("")
    _answers(monkeypatch, str(mine) + "\n")
    assert changed_tests.foreign_pytest(here, UV_COMMAND) is None


def test_no_pytest_anywhere_is_left_to_the_run_to_report(tmp_path, monkeypatch):
    """``shutil.which`` found nothing: that is the run's finding rather than
    this one's, and the run states it in the words of whatever is missing."""
    here = tmp_path / "mine"
    here.mkdir()
    _answers(monkeypatch, "\n")
    assert changed_tests.foreign_pytest(here, UV_COMMAND) is None


@pytest.mark.parametrize(
    "explode",
    [
        OSError(2, "no uv"),
        subprocess.TimeoutExpired(cmd="uv", timeout=1),
    ],
    ids=["no-uv", "timeout"],
)
def test_a_probe_that_cannot_run_is_not_an_accusation(tmp_path, monkeypatch, explode):
    """Unmeasurable is a different answer from "another checkout's".

    The command this gate is about to run is itself ``uv run``, so an
    environment that cannot answer fails loudly at the run. Turning a probe's
    silence into a blocked round is the trade this file already refuses to
    make for its git calls.
    """

    def boom(*a, **k):
        raise explode

    monkeypatch.setattr(changed_tests.subprocess, "run", boom)
    assert changed_tests.foreign_pytest(tmp_path, UV_COMMAND) is None


def test_the_gate_refuses_before_it_takes_a_window(repo, gate, monkeypatch, capsys):
    """A refusal starts no run, so it must not hold a slot others queue for.

    The window is made to explode: reaching it at all is the failure this
    case pins, and the exit code has to be ``CANNOT_TELL`` rather than a pass.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    monkeypatch.setattr(
        changed_tests, "foreign_pytest", lambda repo, cmd: "another checkout's pytest"
    )

    def never(*a, **k):
        raise AssertionError("a window was taken for a run that was refused")

    monkeypatch.setattr(changed_tests.test_window, "acquire", never)

    assert gate() == changed_tests.CANNOT_TELL
    assert gate.runs() == 0
    assert "another checkout's pytest" in capsys.readouterr().err


def test_the_stub_command_in_these_tests_is_never_called_foreign(repo, gate):
    """The property the exemption above buys, stated where it can break.

    Every case driven through the ``gate`` fixture substitutes a plain
    ``python -c`` command. If the check ever stopped reading the command and
    began probing the environment regardless, those cases would start
    spawning uv, so this asserts the exemption directly instead of leaving it
    to be noticed as a slowdown.
    """
    _write(repo, "src/pkg/mesh.py", "x = 2\n")
    assert gate() == 0
    assert gate.runs() == 1


# --------------------------------------------------------------------------- #
# git's output is read as UTF-8 (claunch-gds6-subprocess-decode-cp949-ja5ih)
# --------------------------------------------------------------------------- #
def test_a_korean_subject_is_read_whole(tmp_path):
    """git writes UTF-8. Read with the locale codec (cp949 on this machine),
    the first Korean byte kills subprocess.run's reader thread and the gate
    selects from whatever part of the output survived."""
    repo = tmp_path / "ko"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _write(repo, "a.py")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "표적 선택이 한글 제목을 읽는다")

    out = changed_tests._git(repo, "log", "-1", "--format=%s")

    assert out.strip() == "표적 선택이 한글 제목을 읽는다"
