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

import importlib.util
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
    assert rs.summary_line(found) == "related surfaces: 1 to check, 2 touched by this diff"


def test_the_reasons_are_ordered_guard_then_history_then_tests(repo):
    groups = (("grp", ("src/pkg/a.py", "src/pkg/b.py")),)
    found = _by_path(_surfaces(repo, ["src/pkg/a.py"], groups=groups))
    assert found["src/pkg/b.py"]["reasons"] == ["guard:grp", "cochange:4"]


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
    assert lines[-1] == "related surfaces: 2 to check, 0 touched by this diff"
    assert any(line.startswith("CHECK") and "src/pkg/b.py" in line for line in lines)


def test_json_output_carries_the_same_facts(repo):
    _write(repo, "src/pkg/a.py", "x = 99\n")
    out = _run("--repo", str(repo), "--json", cwd=repo)
    data = json.loads(out.stdout)
    assert data["changed"] == ["src/pkg/a.py"]
    assert {s["path"] for s in data["surfaces"]} == {"src/pkg/b.py", "tests/test_a_guard.py"}
    assert data["summary"] == "related surfaces: 2 to check, 0 touched by this diff"


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
    history."""
    out = _run("--paths", "src/claude_launcher/wizard.py", "src/claude_launcher/cli_sessions.py", "--json")
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
