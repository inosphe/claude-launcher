"""Launcher worktrees must not read as an untracked checkout (claunch-6pcyo).

``git worktree add .claude/worktrees/<name>`` leaves a directory that git lists
as untracked. ``daemon.runtime_state._dirty_paths`` counts untracked paths as
code that is in no commit, so ``tools/deploy_check.py`` exited 3 on the main
checkout whenever any launcher worktree existed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from claude_launcher import worktree
from claude_launcher.daemon import runtime_state

REPO = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_repo_gitignore_hides_launcher_worktrees(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text((REPO / ".gitignore").read_text(encoding="utf-8"))
    (repo / "a.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD")

    wt = worktree.create(repo, "some-session")
    assert wt.path.is_relative_to(repo / worktree.WORKTREES_SUBDIR)

    assert runtime_state._dirty_paths(repo, head) == []


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _exclude_lines(repo: Path) -> list:
    path = repo / ".git" / "info" / "exclude"
    return [x.strip() for x in path.read_text(encoding="utf-8").splitlines()]


def test_create_registers_worktrees_dir_in_info_exclude(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    assert ".gitignore" not in _git(repo, "ls-files")

    worktree.create(repo, "one")
    worktree.create(repo, "one")  # reuse path
    worktree.create(repo, "two")

    assert _exclude_lines(repo).count("/.claude/worktrees/") == 1
    assert runtime_state._dirty_paths(repo, head) == []
    assert _git(repo, "ls-files", "--others", "--exclude-standard") == ""
    assert not (repo / ".gitignore").exists()


def test_create_backfills_worktree_made_before_the_exclude(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "worktree", "add", "-q", "-b", "old", str(repo / ".claude/worktrees/old"))
    assert runtime_state._dirty_paths(repo, head) != []

    worktree.create(repo, "old")

    assert runtime_state._dirty_paths(repo, head) == []


def test_exclude_keeps_existing_content_and_missing_newline(tmp_path):
    repo = _repo(tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.log", encoding="utf-8")  # no trailing newline

    worktree.create(repo, "x")

    assert _exclude_lines(repo) == ["*.log", "/.claude/worktrees/"]


def test_override_dir_inside_repo_is_registered_relative(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv(worktree.WORKTREE_DIR_ENV, str(repo / "wt" / "deep"))

    worktree.create(repo, "x")

    assert "/wt/deep/" in _exclude_lines(repo)
    assert "/.claude/worktrees/" not in _exclude_lines(repo)
    assert runtime_state._dirty_paths(repo, head) == []


def test_override_relative_dir_is_resolved_against_root(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv(worktree.WORKTREE_DIR_ENV, "trees")

    worktree.create(repo, "x")

    assert "/trees/" in _exclude_lines(repo)


def test_override_dir_outside_repo_leaves_exclude_alone(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    before = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    monkeypatch.setenv(worktree.WORKTREE_DIR_ENV, str(tmp_path / "elsewhere"))

    wt = worktree.create(repo, "x")

    assert wt.path.parent == tmp_path / "elsewhere"
    assert (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8") == before


def test_exclude_pattern_is_forward_slashed_and_anchored(tmp_path):
    assert worktree.exclude_pattern(tmp_path, tmp_path / "a" / "b") == "/a/b/"
    assert worktree.exclude_pattern(tmp_path, tmp_path) is None
    assert worktree.exclude_pattern(tmp_path / "r", tmp_path / "o") is None
