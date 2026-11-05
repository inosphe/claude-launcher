"""Bounded, self-describing git timeouts (claunch-jfqhr).

A local git call that never answered held the caller for the full network
budget -- ``reposync_mcp`` runs two of them before it reaches any remote, so one
wedged ``rev-parse`` looked like ten minutes of silence. A timeout now has its
own short budget and says where it happened and what else was running.

Nothing here sleeps: the budget is read off the ``timeout`` argument the call
receives, and the timeout path is entered by raising ``TimeoutExpired``.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from claude_launcher import reposync


def _capturing(seen: list):
    """A ``subprocess.run`` stand-in that records the timeout and answers
    with empty successful output."""
    def run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


# --------------------------------------------------------------------------- #
# which budget a call gets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("args,budget", [
    (("fetch", "--quiet", "origin"), reposync.GIT_TIMEOUT),
    (("push", "--quiet", "origin", "abc:refs/heads/master"), reposync.GIT_TIMEOUT),
    (("ls-remote", "origin"), reposync.GIT_TIMEOUT),
    (("rev-parse", "--show-toplevel"), reposync.LOCAL_TIMEOUT),
    (("rev-parse", "--path-format=absolute", "--git-common-dir"), reposync.LOCAL_TIMEOUT),
    (("for-each-ref", "refs/heads"), reposync.LOCAL_TIMEOUT),
    (("worktree", "list", "--porcelain"), reposync.LOCAL_TIMEOUT),
])
def test_a_remote_subcommand_keeps_the_long_budget_and_a_local_one_does_not(args, budget):
    assert reposync.timeout_for(args) == budget


@pytest.mark.parametrize("args", [
    # the three call sites the review named (claunch-jfqhr, s996 @ 11f44c6f)
    ("reset", "--keep", "0" * 40),                                   # reposync._move_branch
    ("merge", "--ff-only", "--quiet", "0" * 40),                     # reposync.receive_branch
    ("merge", "--ff-only", "--no-edit", "origin/master"),            # local_sync
    # the rest of the family: a budget that fires mid-update leaves the tree
    # half-updated and an index.lock behind
    ("checkout", "--quiet", "master"),
    ("switch", "-c", "branch", "master"),
    ("rebase", "--rebase-merges", "--quiet", "upstream"),
    ("cherry-pick", "abc123"),
    ("stash", "push"),
])
def test_a_subcommand_that_rewrites_the_tree_keeps_the_long_budget(args):
    assert reposync.timeout_for(args) == reposync.GIT_TIMEOUT


def test_a_tree_updating_call_site_passes_the_long_budget_to_git(monkeypatch):
    """The classification is what the call sites rely on, so one of them is
    measured end to end: `reset --keep` on a checked-out branch moves a
    working tree, and the rev-parse that guards it stays a short read."""
    seen: list = []

    def run(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        stdout = "a" * 40 if "rev-parse" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(reposync.subprocess, "run", run)
    plan = reposync.Plan(repo="C:/repo", remote="origin", branch="master",
                         local="a" * 40, upstream="b" * 40,
                         action=reposync.FAST_FORWARD, ahead=1, behind=0,
                         checkout="C:/tree")
    reposync._move_branch(plan, "b" * 40)
    assert seen == [reposync.LOCAL_TIMEOUT, reposync.GIT_TIMEOUT]


def test_a_git_option_before_the_subcommand_does_not_hide_it():
    # window_borrow runs git with `-c user.name=... -c user.email=...` in front
    assert reposync.timeout_for(("-c", "user.name=x", "-c", "user.email=y",
                                 "rev-parse", "HEAD")) == reposync.LOCAL_TIMEOUT
    assert reposync.timeout_for(("-c", "user.name=x", "merge", "--no-ff")) \
        == reposync.GIT_TIMEOUT
    assert reposync.timeout_for(("--git-dir", "/somewhere", "fetch")) == reposync.GIT_TIMEOUT


def test_a_local_git_call_passes_the_short_budget_to_git(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(reposync.subprocess, "run", _capturing(seen))
    reposync.git(str(tmp_path), "rev-parse", "--show-toplevel")
    assert seen == [reposync.LOCAL_TIMEOUT]


def test_a_network_git_call_passes_the_long_budget_to_git(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(reposync.subprocess, "run", _capturing(seen))
    reposync.git(str(tmp_path), "fetch", "--quiet", "origin")
    assert seen == [reposync.GIT_TIMEOUT]


def test_an_explicit_timeout_wins(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(reposync.subprocess, "run", _capturing(seen))
    reposync.git(str(tmp_path), "rev-parse", "--show-toplevel", timeout=7)
    assert seen == [7]


def test_an_ok_check_is_bounded_by_the_same_rule(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(reposync.subprocess, "run", _capturing(seen))
    reposync._ok(str(tmp_path), "merge-base", "--is-ancestor", "a", "b")
    assert seen == [reposync.LOCAL_TIMEOUT]


# --------------------------------------------------------------------------- #
# what a timeout says
# --------------------------------------------------------------------------- #
def test_a_timeout_names_the_command_the_directory_and_how_long(monkeypatch, tmp_path):
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(reposync.subprocess, "run", boom)
    monkeypatch.setattr(reposync, "process_snapshot", lambda: "git processes: 1 (pid 42 7,000 K)")
    with pytest.raises(reposync.SyncError) as err:
        reposync.git(str(tmp_path), "rev-parse", "--show-toplevel")

    text = str(err.value)
    assert "git rev-parse --show-toplevel" in text
    assert str(tmp_path) in text
    assert "timed out after" in text
    assert "limit 30s" in text
    assert "git processes: 1 (pid 42 7,000 K)" in text


def test_a_network_timeout_says_which_budget_it_hit(monkeypatch, tmp_path):
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(reposync.subprocess, "run", boom)
    monkeypatch.setattr(reposync, "process_snapshot", lambda: "git processes: none")
    with pytest.raises(reposync.SyncError) as err:
        reposync.git(str(tmp_path), "fetch", "--quiet", "origin")
    assert "limit 300s" in str(err.value)


def test_the_timeout_diagnostic_survives_to_the_mcp_error(monkeypatch):
    """The MCP server writes a refused tool call's message to
    ``mcp-server.log``; the diagnostic has to still be in it there."""
    from claude_launcher import reposync_mcp

    def boom(path):
        raise reposync.SyncError(
            f"git rev-parse --show-toplevel timed out after 30.0s (limit 30s) in {path}; "
            "git processes: 2 (pid 1 5,120 K; pid 2 8,900 K); caller pid 3")

    monkeypatch.setattr(reposync, "toplevel", boom)
    with pytest.raises(reposync_mcp.ReposyncMcpError) as err:
        reposync_mcp.call_tool("repo_sync", {"action": "status", "workspace": "anywhere"})
    text = str(err.value)
    assert "timed out after 30.0s" in text
    assert "git processes: 2" in text


# --------------------------------------------------------------------------- #
# the process snapshot
# --------------------------------------------------------------------------- #
def _listing(stdout: str, returncode: int = 0):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")
    return run


def test_the_snapshot_lists_the_git_processes_it_finds(monkeypatch):
    if os.name == "nt":
        out = ('"git.exe","111","Console","1","5,120 K"\n'
               '"git.exe","222","Console","1","8,900 K"\n')
        monkeypatch.setattr(reposync.subprocess, "run", _listing(out))
        note = reposync.process_snapshot()
        assert "git processes: 2" in note
        assert "pid 111 5,120 K" in note and "pid 222 8,900 K" in note
    else:
        out = "  111  45 S git\n  222  12 R git\n"
        monkeypatch.setattr(reposync.subprocess, "run", _listing(out))
        note = reposync.process_snapshot()
        assert "git processes: 2" in note
        assert "pid 111 45s S" in note and "pid 222 12s R" in note
    assert f"caller pid {os.getpid()}" in note


def test_the_snapshot_is_bounded(monkeypatch):
    if os.name == "nt":
        out = "".join(f'"git.exe","{100 + i}","Console","1","1,000 K"\n' for i in range(9))
    else:
        out = "".join(f"  {100 + i}  {i}s S git\n" for i in range(9))
    monkeypatch.setattr(reposync.subprocess, "run", _listing(out))
    note = reposync.process_snapshot()
    assert "git processes: 9" in note
    assert note.count("pid ") == reposync.PROCESS_LIST_MAX + 1   # listed pids + caller
    assert "+4 more" in note


def test_no_git_processes_says_so(monkeypatch):
    out = "INFO: No tasks are running which match the specified criteria.\n"
    monkeypatch.setattr(reposync.subprocess, "run", _listing(out, returncode=1))
    assert reposync.process_snapshot().startswith("git processes: none")


def test_a_listing_that_fails_is_recorded_and_never_raised(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("no tasklist here")

    monkeypatch.setattr(reposync.subprocess, "run", boom)
    note = reposync.process_snapshot()
    assert note.startswith("git processes: not listed")
    assert "no tasklist here" in note


def test_the_listing_cannot_hang(monkeypatch):
    seen: list = []

    def slow(argv, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(reposync.subprocess, "run", slow)
    note = reposync.process_snapshot()
    assert seen == [reposync.PROCESS_LIST_TIMEOUT]
    assert note.startswith("git processes: not listed")


# --------------------------------------------------------------------------- #
# the path the incident took
# --------------------------------------------------------------------------- #
def test_resolve_repo_runs_its_git_calls_on_the_short_budget(monkeypatch, tmp_path):
    from claude_launcher import reposync_flow

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)

    seen: list = []
    real = subprocess.run

    def capture(argv, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            seen.append(kwargs.get("timeout"))
        return real(argv, **kwargs)

    monkeypatch.setattr(reposync.subprocess, "run", capture)
    root, _workspace = reposync_flow.resolve_repo(str(repo))
    assert os.path.normcase(os.path.normpath(root)) == os.path.normcase(str(repo))
    assert seen, "resolve_repo ran no git call at all"
    assert set(seen) == {reposync.LOCAL_TIMEOUT}
