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
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repository shaped like this one: ``src/``, ``tools/``, ``tests/``.

    ``master`` carries a source module and its test, so a branch cut from it
    can change either and the mapping has something to find.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
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
    return repo


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
    checked. So unmappable paths are named on stderr, with what to do next.
    """
    _write(repo, "assets/logo.bin", "\x00\x01\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qam", "add an asset nothing guards")

    assert changed_tests.main(["--repo", str(repo), "--list"]) == 0
    err = capsys.readouterr().err
    assert "assets/logo.bin" in err
    assert "not that they are fine" in err
    assert "EXPLICIT_GUARDS" in err  # says how to fix it, not just that


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
    """The convention holds for 31 of 79 modules here, so it has to be allowed
    to miss. Widening is the only thing it may do; inventing a filename that
    does not exist would make the gate fail on its own guess."""
    _write(repo, "src/pkg/lonely.py", "x = 2\n")
    _git(repo, "commit", "-qam", "edit a module with no twin")
    assert _select(repo) == []


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
    assert "abstain" in capsys.readouterr().err


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
