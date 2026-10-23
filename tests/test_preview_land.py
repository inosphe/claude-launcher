"""tools/preview_land.py: the preview merge merge_ready reads, as one command.

D1 a green measurement leaves refs/claunch/preview/<branch> on a merge whose
   parents are the candidate's tip and --base, and no worktree is left
D2 a failed measurement or a conflict leaves no ref and no worktree
D3 a preview made before another change landed on other paths is accepted by
   merge_ready --allow-target-advance after the target moved
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "tools" / "preview_land.py"
READY = ROOT / "tools" / "merge_ready.py"


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(repo, name, text, message):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@example.invalid")
    commit(r, "base.txt", "base\n", "base")
    for name in ("a", "b"):
        git(r, "switch", "-q", "-c", name, "master")
        commit(r, f"{name}.txt", f"{name}\n", name)
        git(r, "switch", "-q", "master")
    return r


def run_preview(repo, scratch, branch, measure="true", base="master"):
    return subprocess.run(
        [sys.executable, str(PREVIEW), "--repo", str(repo), "--branch", branch, "--base", base,
         "--scratch", str(scratch), "--measure-cmd", measure],
        capture_output=True, text=True)


def worktrees(repo):
    return [l for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]


def test_green_measurement_leaves_a_two_parent_preview_ref(repo, tmp_path):
    done = run_preview(repo, tmp_path / "scratch", "a")
    assert done.returncode == 0, done.stderr
    head = git(repo, "rev-parse", "refs/claunch/preview/a")
    parents = git(repo, "log", "-1", "--format=%P", head).split()
    assert set(parents) == {git(repo, "rev-parse", "master"), git(repo, "rev-parse", "a")}
    assert len(worktrees(repo)) == 1


def test_failed_measurement_and_conflict_leave_no_ref(repo, tmp_path):
    red = run_preview(repo, tmp_path / "scratch", "a", measure="false")
    assert red.returncode == 1
    git(repo, "switch", "-q", "-c", "clash", "master")
    commit(repo, "a.txt", "other\n", "clash")
    git(repo, "switch", "-q", "master")
    commit(repo, "a.txt", "master edit\n", "master edit")
    conflict = run_preview(repo, tmp_path / "scratch", "clash")
    assert conflict.returncode == 3
    for name in ("a", "clash"):
        assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                               f"refs/claunch/preview/{name}"], capture_output=True).returncode != 0
    assert len(worktrees(repo)) == 1


def test_older_preview_is_accepted_after_the_target_moves_on_other_paths(repo, tmp_path):
    assert run_preview(repo, tmp_path / "scratch", "b").returncode == 0
    git(repo, "merge", "-q", "--no-ff", "-m", "land a", "a")
    ready = subprocess.run(
        [sys.executable, str(READY), "--repo", str(repo), "--branch", "b", "--allow-target-advance"],
        capture_output=True, text=True)
    assert ready.returncode == 0, ready.stdout + ready.stderr
    without = subprocess.run(
        [sys.executable, str(READY), "--repo", str(repo), "--branch", "b"],
        capture_output=True, text=True)
    assert without.returncode == 1, without.stdout + without.stderr
