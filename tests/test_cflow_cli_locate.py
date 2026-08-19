"""CLI run location: a shell standing elsewhere still finds the run.

The scenario these guard: a chat session's ``!`` shell pinned inside a git
worktree under the project root, while the run is keyed to the root — where
``claunch cflow select ...`` (and ``-t <session>``) used to fail with "no
active cflow run in this directory".
"""

from __future__ import annotations

import pytest

from claude_launcher import cli
from claude_launcher.cflow import engine, state as state_mod

USER_BRANCH = """
steps:
  triage:
    select:
      prompt: risky?
      chooser: user
      options:
        auto:  {description: go, next: after}
        human: {description: review, next: after}
  after:
    instructions: continue
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".claunch" / "workflows").mkdir(parents=True)
    (proj / ".claunch" / "workflows" / "branchy.yaml").write_text(
        USER_BRANCH, encoding="utf-8"
    )
    monkeypatch.chdir(proj)
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    return proj


def _start_user_branch(monkeypatch, session="s15"):
    """A run held at a user-chooser selection, with the agent's proposal filed."""
    monkeypatch.setenv(state_mod.SESSION_ENV, session)
    engine.start("branchy")
    engine.select("auto", "looks safe", by="agent")
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)


def _step_after(project, session="s15"):
    return engine.status(cwd=str(project), scope=session).get("step_id")


def test_select_from_worktree_walks_up(project, monkeypatch, capsys):
    _start_user_branch(monkeypatch)
    wt = project / ".claude" / "worktrees" / "propose-preview"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    assert cli.main(["cflow", "select", "auto"]) == 0
    out = capsys.readouterr().out
    assert f"note: using the cflow run at {project}" in out
    assert _step_after(project) == "after"


def test_select_with_t_from_anywhere_via_registry(
    project, tmp_path, monkeypatch, capsys
):
    _start_user_branch(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["cflow", "select", "-t", "s15", "auto"]) == 0
    assert "note: using the cflow run at" in capsys.readouterr().out
    assert _step_after(project) == "after"


def test_select_with_session_env_from_subdirectory(project, monkeypatch):
    _start_user_branch(monkeypatch)
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.setenv(state_mod.SESSION_ENV, "s15")
    assert cli.main(["cflow", "select", "auto"]) == 0
    assert _step_after(project) == "after"


def test_status_from_worktree_reports_the_run(project, monkeypatch, capsys):
    _start_user_branch(monkeypatch)
    wt = project / ".claude" / "worktrees" / "propose-preview"
    wt.mkdir(parents=True)
    monkeypatch.chdir(wt)
    assert cli.main(["cflow", "status"]) == 0
    out = capsys.readouterr().out
    assert "no active cflow run" not in out
    assert "workflow: branchy" in out


def test_miss_names_the_known_runs(project, tmp_path, monkeypatch, capsys):
    _start_user_branch(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["cflow", "select", "-t", "nope", "auto"]) == 1
    err = capsys.readouterr().err
    assert "no cflow run for session 'nope'" in err
    assert "known runs:" in err
    assert str(project) in err


def test_same_session_in_two_directories_is_ambiguous(
    project, tmp_path, monkeypatch, capsys
):
    _start_user_branch(monkeypatch)
    other = tmp_path / "other"
    (other / ".claunch" / "workflows").mkdir(parents=True)
    (other / ".claunch" / "workflows" / "branchy.yaml").write_text(
        USER_BRANCH, encoding="utf-8"
    )
    monkeypatch.chdir(other)
    _start_user_branch(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.main(["cflow", "select", "-t", "s15", "auto"]) == 1
    err = capsys.readouterr().err
    assert "several directories" in err


def test_run_in_this_directory_still_wins(project, monkeypatch):
    # The redirect only fires on a miss: a run right here is used untouched.
    _start_user_branch(monkeypatch)
    assert cli.main(["cflow", "select", "auto"]) == 0
    assert _step_after(project) == "after"
