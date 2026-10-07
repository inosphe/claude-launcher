"""reposync: one master kept in step across machines through a shared remote.

Every scenario is real git: a bare repository stands for the shared remote
and two clones stand for two machines, each under its own remote name, so
the address -- not the name -- is what ties them together (claunch-dh73y).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_launcher import reposync


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str = "", msg: str = "") -> str:
    (repo / name).write_text(text or name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg or f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _land(repo: Path, branch: str, name: str) -> str:
    """A work-unit branch landed on master with --no-ff, as sessions do."""
    _git(repo, "switch", "-q", "-c", branch)
    _commit(repo, name)
    _git(repo, "switch", "-q", "master")
    _git(repo, "merge", "-q", "--no-ff", "-m", f"merge: {branch}", branch)
    return _git(repo, "rev-parse", "HEAD")


def _clone(remote: Path, where: Path, remote_name: str) -> Path:
    subprocess.run(["git", "clone", "-q", "-o", remote_name, str(remote), str(where)], check=True)
    _git(where, "config", "user.email", "t@example.com")
    _git(where, "config", "user.name", "t")
    _git(where, "config", "commit.gpgsign", "false")
    return where


@pytest.fixture
def world(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "master")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _git(seed, "config", "commit.gpgsign", "false")
    _commit(seed, "base.txt")
    remote = tmp_path / "shared.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    a = _clone(remote, tmp_path / "a", "github.ncsoft.com")
    b = _clone(remote, tmp_path / "b", "origin")
    return {"remote": remote, "a": a, "b": b, "scratch": str(tmp_path / "scratch")}


# --------------------------------------------------------------------------- #
# addresses
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "https://github.ncsoft.com/RnIAgentDev/claunch.git",
    "https://user:secret@GitHub.NCSoft.com/RnIAgentDev/claunch",
    "git@github.ncsoft.com:RnIAgentDev/claunch.git",
    "ssh://git@github.ncsoft.com/RnIAgentDev/claunch/",
])
def test_url_key_is_one_address_for_every_spelling(url):
    assert reposync.url_key(url) == "github.ncsoft.com/rniagentdev/claunch"


def test_url_key_never_carries_credentials():
    assert "secret" not in reposync.url_key("https://u:secret@host.example/o/r.git")


@pytest.mark.parametrize("url", ["/srv/git/r.git", "C:\\repos\\r", "file:///srv/r.git", "../r"])
def test_url_key_skips_local_paths(url):
    assert reposync.url_key(url) is None


def test_choose_remote_picks_the_address_the_peer_shares(world):
    a = world["a"]
    _git(a, "remote", "set-url", "github.ncsoft.com", "https://github.ncsoft.com/RnIAgentDev/claunch.git")
    _git(a, "remote", "add", "origin", "https://github.com/inosphe/claude-launcher.git")
    shared = "github.ncsoft.com/rniagentdev/claunch"
    assert reposync.choose_remote(str(a), peer_keys=[shared, "other/x/y"]) == "github.ncsoft.com"


def test_choose_remote_refuses_to_guess_between_two(world):
    a = world["a"]
    _git(a, "remote", "add", "origin", "https://github.com/inosphe/claude-launcher.git")
    with pytest.raises(reposync.SyncError, match="--remote"):
        reposync.choose_remote(str(a))


def test_choose_remote_reads_the_remembered_choice(world):
    a = world["a"]
    _git(a, "remote", "add", "origin", "https://github.com/inosphe/claude-launcher.git")
    _git(a, "config", reposync.REMOTE_CONFIG, "origin")
    assert reposync.choose_remote(str(a)) == "origin"


def test_describe_sends_keys_and_tips_not_urls(world):
    a = world["a"]
    _git(a, "remote", "set-url", "github.ncsoft.com", "https://u:tok@git.example/o/r.git")
    _commit(a, "x.txt")
    doc = reposync.describe(str(a))
    assert doc["remotes"] == [{"name": "github.ncsoft.com", "key": "git.example/o/r"}]
    master = doc["branches"]["master"]
    assert master["sha"] == _git(a, "rev-parse", "master")
    assert master["tracking"][0]["ahead"] == 1 and master["tracking"][0]["behind"] == 0
    assert "tok" not in repr(doc)


# --------------------------------------------------------------------------- #
# master: plan and execute
# --------------------------------------------------------------------------- #
def test_up_to_date(world):
    p = reposync.plan(str(world["a"]), "github.ncsoft.com")
    assert p.action == reposync.UP_TO_DATE
    assert reposync.execute(p, scratch=world["scratch"]).pushed is False


def test_behind_only_fast_forwards_without_a_merge_commit(world):
    a, b = world["a"], world["b"]
    tip = _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    p = reposync.plan(str(a), "github.ncsoft.com")
    assert (p.action, p.ahead, p.behind) == (reposync.FAST_FORWARD, 0, 1)
    reposync.execute(p, scratch=world["scratch"])
    assert _git(a, "rev-parse", "master") == tip
    assert (a / "from-b.txt").exists()  # the checked-out tree followed


def test_ahead_only_pushes(world):
    a = world["a"]
    tip = _commit(a, "from-a.txt")
    p = reposync.plan(str(a), "github.ncsoft.com")
    assert p.action == reposync.PUSH
    r = reposync.execute(p, scratch=world["scratch"])
    assert r.pushed and _git(world["remote"], "rev-parse", "master") == tip


def test_diverged_rebases_onto_the_remote_and_keeps_landing_merges(world):
    a, b = world["a"], world["b"]
    _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    upstream = _git(b, "rev-parse", "master")
    old_merge = _land(a, "s1-feature", "feature.txt")

    p = reposync.plan(str(a), "github.ncsoft.com")
    assert p.action == reposync.REBASE and p.merges == [old_merge]
    r = reposync.execute(p, scratch=world["scratch"])

    new = _git(a, "rev-parse", "master")
    assert r.new == new and r.pushed
    assert _git(world["remote"], "rev-parse", "master") == new
    # one line: the remote tip is on the first-parent line, no sync merge
    first_parent = _git(a, "rev-list", "--first-parent", "master").splitlines()
    assert upstream in first_parent
    assert _git(a, "rev-list", "--merges", f"{upstream}..master").splitlines() == [new]
    # the --no-ff landing survived as a merge, with its message
    assert _git(a, "log", "-1", "--format=%s", new) == "merge: s1-feature"
    assert r.rewritten[old_merge] == new
    # the landed work branch is not stranded: its tip is already in master
    assert p.affected == []


def test_rebase_lists_stranded_branches_and_moves_none(world):
    a, b = world["a"], world["b"]
    _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    _land(a, "s1-feature", "feature.txt")
    old_tip = _git(a, "rev-parse", "master")
    _git(a, "branch", "s2-on-top", "master")
    wt = a.parent / "wt-s2"
    _git(a, "worktree", "add", "-q", str(wt), "s2-on-top")

    p = reposync.plan(str(a), "github.ncsoft.com")
    assert p.affected == ["s2-on-top"]
    r = reposync.execute(p, scratch=world["scratch"])
    assert _git(a, "rev-parse", "s2-on-top") == old_tip  # untouched
    [entry] = r.restack
    assert entry["branch"] == "s2-on-top"
    assert Path(entry["worktree"]).resolve() == wt.resolve()
    assert entry["command"] == f"git rebase --onto {r.new} {old_tip} s2-on-top"


def test_conflict_aborts_and_moves_nothing(world):
    a, b = world["a"], world["b"]
    _commit(b, "same.txt", "b side")
    _git(b, "push", "-q", "origin", "master")
    before = _commit(a, "same.txt", "a side")
    p = reposync.plan(str(a), "github.ncsoft.com")
    with pytest.raises(reposync.RebaseConflict) as caught:
        reposync.execute(p, scratch=world["scratch"])
    assert caught.value.files == ["same.txt"] and not caught.value.only_board
    assert _git(a, "rev-parse", "master") == before
    assert _git(world["remote"], "rev-parse", "master") == _git(b, "rev-parse", "master")
    assert "reposync-" not in _git(a, "worktree", "list")


def test_board_only_conflict_names_the_board_way_out(world):
    a, b = world["a"], world["b"]
    (b / ".beads").mkdir()
    _commit(b, ".beads/issues.jsonl", '{"id":"x-1"}\n')
    _git(b, "push", "-q", "origin", "master")
    (a / ".beads").mkdir()
    _commit(a, ".beads/issues.jsonl", '{"id":"x-2"}\n')
    p = reposync.plan(str(a), "github.ncsoft.com")
    with pytest.raises(reposync.RebaseConflict) as caught:
        reposync.execute(p, scratch=world["scratch"])
    assert caught.value.only_board
    assert "claunch beads sync --merge" in str(caught.value)


def test_checked_out_master_keeps_local_edits_across_the_rebase(world):
    a, b = world["a"], world["b"]
    _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    _commit(a, "from-a.txt")
    (a / "base.txt").write_text("edited, not committed", encoding="utf-8")
    p = reposync.plan(str(a), "github.ncsoft.com")
    assert Path(p.checkout).resolve() == a.resolve()
    reposync.execute(p, scratch=world["scratch"])
    assert (a / "base.txt").read_text(encoding="utf-8") == "edited, not committed"
    assert (a / "from-b.txt").exists() and (a / "from-a.txt").exists()


def test_idle_master_moves_by_ref_only(world):
    a, b = world["a"], world["b"]
    tip = _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    _git(a, "switch", "-q", "-c", "elsewhere")
    p = reposync.plan(str(a), "github.ncsoft.com")
    assert p.checkout is None
    reposync.execute(p, scratch=world["scratch"])
    assert _git(a, "rev-parse", "master") == tip
    assert _git(a, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"


def test_rejected_push_after_a_late_remote_move(world):
    a, b = world["a"], world["b"]
    _commit(a, "from-a.txt")
    p = reposync.plan(str(a), "github.ncsoft.com")
    _commit(b, "late.txt")
    _git(b, "push", "-q", "origin", "master")
    with pytest.raises(reposync.SyncError, match="push"):
        reposync.execute(p, scratch=world["scratch"])


def test_three_runs_converge_both_machines(world):
    a, b = world["a"], world["b"]
    _land(a, "s1-x", "x.txt")
    _land(b, "s9-y", "y.txt")
    reposync.execute(reposync.plan(str(a), "github.ncsoft.com"), scratch=world["scratch"])
    reposync.execute(reposync.plan(str(b), "origin"), scratch=world["scratch"])
    reposync.execute(reposync.plan(str(a), "github.ncsoft.com"), scratch=world["scratch"])
    assert _git(a, "rev-parse", "master") == _git(b, "rev-parse", "master") \
        == _git(world["remote"], "rev-parse", "master")


# --------------------------------------------------------------------------- #
# work branches
# --------------------------------------------------------------------------- #
def test_machine_prefix_is_ref_safe():
    assert reposync.shared_ref("yusanghyun-D09", "s875-x") == "yusanghyun-d09/s875-x"
    assert reposync.machine_prefix(" shared daemon:main ") == "shared-daemon-main"


def test_branch_published_on_one_machine_is_received_on_the_other(world):
    a, b = world["a"], world["b"]
    _git(a, "switch", "-q", "-c", "s118-sweep")
    tip = _commit(a, "work.txt")
    target = reposync.publish_branch(str(a), "github.ncsoft.com", "s118-sweep", "yusanghyun-D09")
    assert target == "yusanghyun-d09/s118-sweep"
    # b has its own s118-sweep (session names are per daemon): untouched
    _git(b, "branch", "s118-sweep")
    local, sha = reposync.receive_branch(str(b), "origin", "yusanghyun-D09", "s118-sweep")
    assert (local, sha) == ("yusanghyun-d09/s118-sweep", tip)
    assert _git(b, "rev-parse", "s118-sweep") != tip


def test_receive_refuses_a_non_fast_forward(world):
    a, b = world["a"], world["b"]
    _git(a, "switch", "-q", "-c", "w")
    _commit(a, "one.txt")
    reposync.publish_branch(str(a), "github.ncsoft.com", "w", "m1")
    reposync.receive_branch(str(b), "origin", "m1", "w")
    _git(b, "switch", "-q", "m1/w")
    _commit(b, "b-only.txt")
    _git(a, "commit", "-q", "--amend", "-m", "rewritten")
    reposync.publish_branch(str(a), "github.ncsoft.com", "w", "m1", force_with_lease=True)
    with pytest.raises(reposync.SyncError, match="fast-forward"):
        reposync.receive_branch(str(b), "origin", "m1", "w")


def test_unlanded_work_on_a_rewritten_side_commit_is_stranded(world):
    a, b = world["a"], world["b"]
    _commit(b, "from-b.txt")
    _git(b, "push", "-q", "origin", "master")
    _land(a, "s1-feature", "feature.txt")
    _git(a, "switch", "-q", "s1-feature")
    _commit(a, "more.txt")  # work after the landing, not in master yet
    _git(a, "switch", "-q", "master")
    p = reposync.plan(str(a), "github.ncsoft.com")
    assert p.affected == ["s1-feature"]
