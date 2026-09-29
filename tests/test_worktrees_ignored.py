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
