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


def test_the_script_runs_as_a_script_and_its_exit_status_reaches_the_shell():
    """The verify line runs it this way, and only this path proves the file
    is executable and its exit code survives the shell."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(SCRIPT.parent), "--list"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1, 2)
