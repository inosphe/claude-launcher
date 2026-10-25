"""The daemon does not run in the directory of the shell that started it
(claunch-2yiij.1).

The live daemon of 2026-10-15 had been spawned by an ``ensure_running`` in
s609's shell and ran in s609's worktree (``.claude/worktrees/w3-p1-...``):
``spawn_daemon`` passed no ``cwd``, so the child took its caller's. Every
daemon read that falls back to ``os.getcwd()`` -- the API calls sent without a
``cwd`` (``cflow_state.resolve_cwd(None)``), the dashboard's CLI shell, the
board pinned at startup -- then answered for that one session's checkout, and
for a directory that stops existing when the worktree is removed.

The daemon's directory is the main checkout of the caller's repository
(``worktree.repo_root``, through the common git dir, so every worktree of a
repository names the same one), or the home directory outside any
repository. It is set where the daemon is spawned and again at boot, the same
two points the session environment is removed at (claunch-2yiij).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_launcher import daemon_client
from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import instance_manifest


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _build(root: Path) -> None:
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)


@pytest.fixture
def repo(tmp_path, repo_template):
    return repo_template("daemon-cwd", _build, tmp_path / "repo").resolve()


@pytest.fixture
def linked(repo):
    """A session's worktree, where ``.claude/worktrees/<name>`` puts it."""
    tree = repo / ".claude" / "worktrees" / "s609-x"
    _git("worktree", "add", "-q", "-b", "s609-x", str(tree), cwd=repo)
    return tree


def _spawned_cwd(monkeypatch):
    seen = {}
    real = subprocess.Popen

    def fake_popen(argv, **kw):
        if argv[0] == "git":  # daemon_cwd asking git where the checkout is
            return real(argv, **kw)
        seen.update(kw)
        raise OSError("not really")

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        daemon_client.spawn_daemon(reason="test")
    return seen.get("cwd")


def test_a_daemon_spawned_from_a_sessions_worktree_runs_in_the_main_checkout(
        repo, linked, monkeypatch):
    monkeypatch.chdir(linked)
    assert Path(_spawned_cwd(monkeypatch)) == repo


def test_a_daemon_spawned_from_a_subdirectory_runs_in_its_checkout(repo, monkeypatch):
    sub = repo / "docs"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert Path(_spawned_cwd(monkeypatch)) == repo


def test_a_daemon_spawned_outside_any_repository_runs_in_the_home_directory(
        tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert Path(_spawned_cwd(monkeypatch)) == Path.home()


def test_a_caller_whose_directory_was_removed_still_spawns_one(tmp_path, monkeypatch):
    """A worktree removed under a live shell: ``os.getcwd()`` raises there,
    and the spawn goes on in the home directory instead of failing."""
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    try:
        gone.rmdir()
    except PermissionError:
        pytest.skip("Windows will not remove the current directory of a process")
    assert Path(_spawned_cwd(monkeypatch)) == Path.home()


def test_a_machine_without_git_still_spawns_one(repo, monkeypatch):
    """No git to ask is not a reason to leave the daemon down."""
    monkeypatch.chdir(repo)
    seen = {}

    def fake_popen(argv, **kw):
        if argv[0] == "git":
            raise FileNotFoundError("git")
        seen.update(kw)
        raise OSError("not really")

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError, match="not really"):
        daemon_client.spawn_daemon(reason="test")
    assert Path(seen["cwd"]) == Path.home()


class _Booted(Exception):
    pass


@pytest.mark.usefixtures("daemon_config")
def test_a_daemon_started_by_hand_in_a_worktree_moves_to_the_main_checkout(
        repo, linked, monkeypatch):
    """A start that did not go through spawn_daemon (``python -m
    claude_launcher.daemon`` by hand, a predecessor on older code) is moved
    at boot, before anything reads the directory."""
    monkeypatch.chdir(linked)
    monkeypatch.setattr(instance_manifest, "apply", lambda: None)
    monkeypatch.setattr(instance_manifest, "pinned_port", lambda: None)
    seen = {}

    def booted(_foreground):
        seen["cwd"] = os.getcwd()
        raise _Booted

    monkeypatch.setattr(daemon_main, "_setup_logging", booted)
    with pytest.raises(_Booted):
        daemon_main.main([])
    assert Path(seen["cwd"]) == repo


def test_a_cwd_less_fallback_answers_the_main_checkout(repo, linked, monkeypatch):
    """What the fallbacks read after the move: ``resolve_cwd(None)`` -- the
    API calls sent without a ``cwd`` -- names the main checkout, the same
    directory from every worktree."""
    from claude_launcher.cflow import state as cflow_state

    monkeypatch.chdir(linked)
    monkeypatch.chdir(daemon_client.daemon_cwd())
    assert Path(cflow_state.resolve_cwd(None)) == repo


@pytest.mark.usefixtures("daemon_config")
def test_a_test_that_boots_the_daemon_in_process_leaves_the_directory_alone(
        repo, linked, monkeypatch, request):
    """``main()`` moves the process; in a test that move would outlive the
    test and every later one on the worker would start in the daemon's
    directory. The autouse ``cwd_restore`` puts it back -- this boot runs
    without ``monkeypatch.chdir`` to stand for the callers that have none."""
    from conftest import restored_cwd

    assert "cwd_restore" in request.fixturenames
    monkeypatch.setattr(instance_manifest, "apply", lambda: None)
    monkeypatch.setattr(instance_manifest, "pinned_port", lambda: None)

    def booted(_foreground):
        raise _Booted

    monkeypatch.setattr(daemon_main, "_setup_logging", booted)
    os.chdir(linked)
    try:
        with restored_cwd() as before:
            with pytest.raises(_Booted):
                daemon_main.main([])
            assert Path(os.getcwd()) == repo
        assert os.getcwd() == before
    finally:
        os.chdir(request.config.invocation_params.dir)
