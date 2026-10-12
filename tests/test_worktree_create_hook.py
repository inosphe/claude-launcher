"""A repository's own create command, and saying copy-on-write before it runs.

``.claunch/worktree.yaml`` lets a repository make its checkouts itself (a
block clone of an Unreal tree, build caches included). These tests hold the
three things that must be true about it: the command is held to the same
contract as ``git worktree add`` (a registered checkout at the path, or the
launch fails), how a NEW checkout would be made is decided and said *before*
it is made, and how an existing one was made is read back from its record.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import worktree
from claude_launcher.daemon import worktree_inventory

pytestmark = pytest.mark.worktree

REAL_RUN = subprocess.run

#: A create hook that does what a real one must: make a registered worktree
#: at $CLAUNCH_WT_PATH on $CLAUNCH_WT_BRANCH. It also writes the environment it
#: was given beside the repository, so the contract can be asserted, and
#: exits with $HOOK_EXIT / skips the worktree on $HOOK_SKIP to fail on cue.
HOOK = r'''
import json, os, subprocess, sys
env = {k: v for k, v in os.environ.items() if k.startswith("CLAUNCH_WT_")}
open(os.environ["HOOK_ENV_OUT"], "w", encoding="utf-8").write(json.dumps(env))
if os.environ.get("HOOK_EXIT"):
    print("clone failed: volume said no", file=sys.stderr)
    sys.exit(int(os.environ["HOOK_EXIT"]))
if not os.environ.get("HOOK_SKIP"):
    args = ["git", "worktree", "add"]
    if env["CLAUNCH_WT_NEW_BRANCH"] == "1":
        args += ["-b", env["CLAUNCH_WT_BRANCH"], env["CLAUNCH_WT_PATH"]]
        if env["CLAUNCH_WT_START"]:
            args.append(env["CLAUNCH_WT_START"])
    else:
        args += [env["CLAUNCH_WT_PATH"], env["CLAUNCH_WT_BRANCH"]]
    subprocess.run(args, cwd=env["CLAUNCH_WT_ROOT"], check=True,
                   capture_output=True)
'''


def git(*args, cwd):
    return REAL_RUN(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _build_repo(root):
    git("init", "-q", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)


@pytest.fixture
def repo(tmp_path, repo_template, monkeypatch):
    monkeypatch.delenv(worktree.SESSION_ENV, raising=False)
    monkeypatch.delenv(worktree.WORKTREE_DIR_ENV, raising=False)
    monkeypatch.setenv("HOOK_ENV_OUT", str(tmp_path / "hook-env.json"))
    for name in ("HOOK_EXIT", "HOOK_SKIP"):
        monkeypatch.delenv(name, raising=False)
    return repo_template("create-hook", _build_repo, tmp_path / "repo")


def write_config(repo: Path, text: str) -> None:
    path = repo / worktree.CREATE_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def use_hook(repo: Path, *, cow: bool = False, timeout: float = 120) -> None:
    script = repo.parent / "hook.py"
    script.write_text(HOOK, encoding="utf-8")
    write_config(repo, json.dumps({"create": {
        "command": [sys.executable, str(script)],
        "timeout": timeout,
        "cow": cow,
    }}))


def on_volume(monkeypatch, filesystem: str, same: bool = True) -> None:
    """Pretend the worktrees directory is on ``filesystem``."""
    monkeypatch.setattr(worktree, "volume_filesystem", lambda path: filesystem)
    monkeypatch.setattr(worktree, "_same_volume", lambda a, b: same)


# --------------------------------------------------------------------------- #
# reading the config
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, error", [
    ("", ""),
    ("other: 1\n", ""),
    ("create: [1]\n", "create must be a mapping"),
    ("create: {command: []}\n", "command must be a non-empty list"),
    ("create: {command: run.ps1}\n", "command must be a non-empty list"),
    ("create: {command: [a], timeout: 0}\n", "timeout must be a positive"),
    ("create: {command: [a], cow: yes-please}\n", "cow must be true or false"),
    ("create: {command: [a\n", "not valid YAML"),
    ("- a\n", "must be a mapping"),
])
def test_a_config_is_read_or_refused_with_its_reason(repo, text, error):
    assert worktree.load_create_hook(repo) == (None, "")  # no file at all
    write_config(repo, text)
    hook, why = worktree.load_create_hook(repo)
    assert hook is None
    assert (error in why) if error else why == ""


def test_a_valid_config_becomes_a_hook(repo):
    write_config(repo, "create:\n  command: [pwsh, -File, x.ps1]\n  cow: true\n")
    hook, why = worktree.load_create_hook(repo)
    assert why == ""
    assert hook.command == ["pwsh", "-File", "x.ps1"]
    assert hook.cow is True
    assert hook.timeout == worktree.DEFAULT_CREATE_TIMEOUT


# --------------------------------------------------------------------------- #
# deciding, before anything is made
# --------------------------------------------------------------------------- #
def test_the_mode_is_decided_before_creation(repo, monkeypatch):
    on_volume(monkeypatch, "ReFS")
    plain = worktree.creation_mode(repo)
    assert plain["mode"] == worktree.MODE_GIT and not plain["cow"]
    assert "tracked files only" in plain["summary"]

    use_hook(repo)
    assert worktree.creation_mode(repo)["mode"] == worktree.MODE_HOOK

    use_hook(repo, cow=True)
    cow = worktree.creation_mode(repo)
    assert cow["mode"] == worktree.MODE_COW and cow["cow"] and cow["block_clone"]
    assert cow["summary"].startswith("block-cloned on ReFS by ")

    # The case worth a warning: a hook that claims CoW where it cannot clone.
    on_volume(monkeypatch, "NTFS")
    ntfs = worktree.creation_mode(repo)
    assert ntfs["mode"] == worktree.MODE_COW_UNAVAILABLE and not ntfs["cow"]
    assert "NTFS" in ntfs["summary"] and "every file will be copied" in ntfs["summary"]
    on_volume(monkeypatch, "ReFS", same=False)
    assert "different volume" in worktree.creation_mode(repo)["summary"]

    write_config(repo, "create: {command: []}\n")
    broken = worktree.creation_mode(repo)
    assert broken["mode"] == worktree.MODE_INVALID
    assert "will fail" in broken["summary"]


def test_the_pickers_get_the_mode_with_the_rest_of_the_git_answer(repo, monkeypatch):
    on_volume(monkeypatch, "ReFS")
    use_hook(repo, cow=True)
    assert worktree.info(str(repo))["create"]["mode"] == worktree.MODE_COW


# --------------------------------------------------------------------------- #
# making, and the record left behind
# --------------------------------------------------------------------------- #
def test_the_hook_makes_the_checkout_and_the_mode_is_recorded(
    repo, monkeypatch, tmp_path, capsys,
):
    on_volume(monkeypatch, "ReFS")
    use_hook(repo, cow=True)
    wt = worktree.create(repo, "cow-one")
    assert wt.created and wt.create_mode == worktree.MODE_COW
    assert worktree._registered(repo, wt.path)
    assert worktree.current_branch(wt.path) == "cow-one"
    assert worktree.created_mode(wt.path) == worktree.MODE_COW
    env = json.loads((tmp_path / "hook-env.json").read_text(encoding="utf-8"))
    assert env["CLAUNCH_WT_BRANCH"] == "cow-one"
    assert env["CLAUNCH_WT_NEW_BRANCH"] == "1"
    assert Path(env["CLAUNCH_WT_PATH"]) == wt.path
    assert Path(env["CLAUNCH_WT_ROOT"]) == repo
    # The record is in git's admin directory, so the tree stays clean.
    assert git("status", "--porcelain", cwd=wt.path).stdout == ""

    # A reused checkout reports what its record says; the hook is not rerun.
    (tmp_path / "hook-env.json").unlink()
    again = worktree.create(repo, "cow-one")
    assert not again.created and again.create_mode == worktree.MODE_COW
    assert not (tmp_path / "hook-env.json").exists()

    worktree.announce(again)
    assert "[copy-on-write]" in capsys.readouterr().err

    # The Worktrees page reads the same record, and the repository's next mode.
    listed = worktree_inventory.list_repo(str(repo), [])
    assert [w["create_mode"] for w in listed["worktrees"]] == [worktree.MODE_COW]
    assert listed["create"]["mode"] == worktree.MODE_COW


def test_an_existing_branch_is_handed_over_as_such(repo, tmp_path):
    git("branch", "kept", cwd=repo)
    use_hook(repo)
    wt = worktree.create(repo, "kept")
    env = json.loads((tmp_path / "hook-env.json").read_text(encoding="utf-8"))
    assert env["CLAUNCH_WT_NEW_BRANCH"] == "0"
    assert wt.create_mode == worktree.MODE_HOOK


def test_without_a_hook_git_makes_it_and_says_so(repo):
    wt = worktree.create(repo, "plain")
    assert wt.create_mode == worktree.MODE_GIT
    assert worktree.created_mode(wt.path) == worktree.MODE_GIT


def test_a_checkout_without_a_record_is_unknown_not_git(repo):
    path = repo / ".claude" / "worktrees" / "byhand"
    git("worktree", "add", "-b", "byhand", str(path), cwd=repo)
    assert worktree.created_mode(path) == ""
    assert worktree.created_mode(repo) == ""  # the main checkout has no record


@pytest.mark.parametrize("env, why", [
    ({"HOOK_EXIT": "3"}, "exited 3:\nclone failed: volume said no"),
    ({"HOOK_SKIP": "1"}, "is not a registered worktree"),
])
def test_a_hook_that_breaks_the_contract_fails_the_launch(
    repo, monkeypatch, env, why,
):
    use_hook(repo)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(worktree.WorktreeError) as exc:
        worktree.create(repo, "broken")
    assert why in str(exc.value)


def test_a_missing_command_and_a_slow_one_fail_with_their_reason(repo):
    write_config(repo, "create:\n  command: [no-such-command-claunch-test]\n")
    with pytest.raises(worktree.WorktreeError, match="could not start"):
        worktree.create(repo, "missing")
    write_config(repo, json.dumps({"create": {
        "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        "timeout": 0.5,
    }}))
    with pytest.raises(worktree.WorktreeError, match="did not finish"):
        worktree.create(repo, "slow")


def test_a_broken_config_refuses_rather_than_falling_back_to_git(repo):
    write_config(repo, "create: {command: []}\n")
    with pytest.raises(worktree.WorktreeError, match="non-empty list"):
        worktree.create(repo, "fallback")
    assert not (worktree.worktrees_dir(repo) / "fallback").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="volume probe is Windows-only")
def test_the_volume_probe_names_a_real_filesystem(tmp_path):
    # Whatever this machine's temp volume is, it has a name; a path that does
    # not exist yet is answered for its nearest existing ancestor.
    assert worktree.volume_filesystem(tmp_path / "not" / "yet")
    assert worktree._same_volume(tmp_path, tmp_path / "not" / "yet") is True
