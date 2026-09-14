"""The PR wizard's engine (prflow.py) against real, temporary repositories.

What the module promises is negative as much as positive: a branch is pushed
and a pull request opened, and the session's checkout is *not* touched -- no
branch switched, no commit added to the one it is on, the index and working
tree as they were. Every test here reads that back from git after the call,
because "did not change" is the claim an agent mid-task depends on.

``gh`` never runs: the tests hand :func:`open_pr`/:func:`run` a scripted
runner with the same signature, and pin what the module asks of it -- the
``pr view`` probe before ``pr create``, the ``--head``/``--base`` pair, the
body going through a file -- since those are the lines ``improv-worker-
remote``'s pr-open step spells for a worker to type by hand.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from claude_launcher import prflow


# --------------------------------------------------------------------------- #
# repositories
# --------------------------------------------------------------------------- #
def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """A bare 'remote' whose url reads as a GitHub host, and a work repository
    with one commit on ``master`` that pushes to it."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "master", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-b", "master", str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "first")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "master")
    # A second remote name pointing at the same bare repo, but spelled as a
    # GitHub URL so the module sees a host to open a PR on. Pushes go to
    # ``origin`` (the path); ``gh`` is scripted, so the host is never called.
    _git(work, "remote", "add", "gh", "https://ghe.example.com/team/proj.git")
    return {"remote": remote, "work": work}


def _state(work: Path) -> dict:
    return {
        "branch": _git(work, "rev-parse", "--abbrev-ref", "HEAD"),
        "head": _git(work, "rev-parse", "HEAD"),
        "status": _git(work, "status", "--porcelain", "--untracked-files=all"),
        "index": _git(work, "write-tree"),
    }


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
def test_default_branch_is_session_first_and_stamped():
    name = prflow.default_branch("s545", datetime(2026, 9, 14, 20, 41))
    assert name == "s545-pr-20260914-2041"
    assert prflow.default_branch("", datetime(2026, 1, 1)).startswith("session-pr-")
    assert prflow.default_branch("a b/c", datetime(2026, 1, 1)).startswith("a-b-c-pr-")


def test_validate_branch_asks_git(repos):
    work = str(repos["work"])
    assert prflow.validate_branch(work, "s1-pr-x") == "s1-pr-x"
    with pytest.raises(prflow.PrError) as exc:
        prflow.validate_branch(work, "bad name")
    assert exc.value.step == "push"
    with pytest.raises(prflow.PrError):
        prflow.validate_branch(work, "")


# --------------------------------------------------------------------------- #
# preview
# --------------------------------------------------------------------------- #
def test_preview_reads_the_directory_and_names_blockers(repos):
    work = repos["work"]
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")
    pv = prflow.preview(str(work), session="s9", which=lambda _b: None,
                        now=datetime(2026, 9, 14, 20, 41))
    assert pv["repo"] is True
    assert pv["branch"] == "master"
    assert pv["head"] == _git(work, "rev-parse", "HEAD")
    assert pv["head_subject"] == "first"
    assert pv["dirty"] == {"tracked": 1, "untracked": 1}
    assert pv["worktree"] == ""
    assert pv["branch_default"] == "s9-pr-20260914-2041"
    names = {r["remote"]: r for r in pv["remotes"]}
    assert names["gh"]["host"] == "ghe.example.com"
    assert names["gh"]["slug"] == "team/proj"
    assert names["origin"]["host"] is None
    assert pv["remote"] == "origin"          # origin wins when nothing is configured
    assert pv["base"] == "master"
    assert any("gh is not installed" in b for b in pv["blockers"])


def test_preview_honours_the_configured_pr_remote_and_base(repos):
    work = repos["work"]
    _git(work, "config", "claunch.pr.remote", "gh")
    _git(work, "config", "claunch.pr.base", "develop")
    pv = prflow.preview(str(work), which=lambda _b: "/usr/bin/gh",
                        auth=lambda _gh, host: {"authenticated": False, "detail": ""})
    assert pv["remote"] == "gh"
    assert pv["base"] == "develop"
    assert "ghe.example.com" in pv["auth"]
    assert any("not signed in to ghe.example.com" in b for b in pv["blockers"])


def test_preview_outside_a_repository(tmp_path):
    pv = prflow.preview(str(tmp_path), which=lambda _b: None)
    assert pv["repo"] is False
    assert pv["blockers"][0].startswith("the session's directory is not inside a git repository")


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_of_a_clean_tree_is_head(repos):
    work = repos["work"]
    before = _state(work)
    snap = prflow.snapshot(str(work), include_uncommitted=True, session="s1")
    assert snap == {"sha": before["head"], "parent": before["head"], "created": False, "files": 0}
    assert _state(work) == before


def test_snapshot_builds_a_commit_off_head_without_touching_the_checkout(repos):
    work = repos["work"]
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    (work / "new.txt").write_text("n\n", encoding="utf-8")
    # something staged too: the scratch index must not read the real one,
    # and the real one must come back exactly as it was
    (work / "staged.txt").write_text("s\n", encoding="utf-8")
    _git(work, "add", "staged.txt")
    before = _state(work)

    snap = prflow.snapshot(str(work), include_uncommitted=True, session="s1")

    assert snap["created"] is True
    assert snap["parent"] == before["head"]
    assert snap["files"] == 3
    sha = snap["sha"]
    assert sha != before["head"]
    # the commit hangs off HEAD and holds the working tree
    assert _git(work, "rev-parse", f"{sha}^") == before["head"]
    assert _git(work, "show", f"{sha}:a.txt") == "two"
    assert _git(work, "show", f"{sha}:new.txt") == "n"
    assert _git(work, "show", f"{sha}:staged.txt") == "s"
    # ... and is on no branch
    assert _git(work, "branch", "--contains", sha) == ""
    msg = _git(work, "log", "-1", "--format=%B", sha)
    assert "Claunch-Session: s1" in msg
    assert "Claunch-Worktree" not in msg          # main checkout: no worktree trailer
    # the checkout is as it was: branch, HEAD, status, index
    assert _state(work) == before


def test_snapshot_skips_uncommitted_when_not_asked(repos):
    work = repos["work"]
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    before = _state(work)
    snap = prflow.snapshot(str(work), include_uncommitted=False)
    assert snap["created"] is False and snap["sha"] == before["head"]
    assert _state(work) == before


def test_snapshot_in_a_linked_worktree_carries_the_worktree_trailer(repos):
    work = repos["work"]
    wt = work / ".claude" / "worktrees" / "s1-thing"
    _git(work, "worktree", "add", "-b", "s1-thing", str(wt), "master")
    (wt / "b.txt").write_text("b\n", encoding="utf-8")
    snap = prflow.snapshot(str(wt), include_uncommitted=True, session="s1")
    assert snap["created"] is True
    msg = _git(wt, "log", "-1", "--format=%B", snap["sha"])
    assert "Claunch-Worktree: s1-thing" in msg
    assert prflow.preview(str(wt), which=lambda _b: None)["worktree"] == "s1-thing"


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def test_push_publishes_the_sha_under_the_name_and_leaves_the_checkout(repos):
    work, remote = repos["work"], repos["remote"]
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    snap = prflow.snapshot(str(work), include_uncommitted=True, session="s1")
    before = _state(work)

    out = prflow.push(str(work), "origin", snap["sha"], "s1-pr-1")

    assert out == {"remote": "origin", "branch": "s1-pr-1", "sha": snap["sha"], "forced": False}
    assert _git(remote, "rev-parse", "refs/heads/s1-pr-1") == snap["sha"]
    assert _state(work) == before
    assert "s1-pr-1" not in _git(work, "branch", "--list")     # no local branch was made


def test_push_refuses_to_move_an_existing_branch_unless_forced(repos):
    work, remote = repos["work"], repos["remote"]
    head = _git(work, "rev-parse", "HEAD")
    prflow.push(str(work), "origin", head, "s1-pr-2")
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    other = prflow.snapshot(str(work), include_uncommitted=True)["sha"]
    (work / "a.txt").write_text("three\n", encoding="utf-8")
    third = prflow.snapshot(str(work), include_uncommitted=True)["sha"]
    # a sibling commit (same parent) is not a fast-forward: refused
    _git(work, "push", "origin", f"{other}:refs/heads/s1-pr-2")
    with pytest.raises(prflow.PrError) as exc:
        prflow.push(str(work), "origin", third, "s1-pr-2")
    assert exc.value.step == "push"
    assert _git(remote, "rev-parse", "refs/heads/s1-pr-2") == other
    # with the lease it moves
    out = prflow.push(str(work), "origin", third, "s1-pr-2", force=True)
    assert out["forced"] is True
    assert _git(remote, "rev-parse", "refs/heads/s1-pr-2") == third


def test_push_validates_the_name_before_touching_the_remote(repos):
    work = repos["work"]
    head = _git(work, "rev-parse", "HEAD")
    with pytest.raises(prflow.PrError):
        prflow.push(str(work), "origin", head, "no spaces")
    with pytest.raises(prflow.PrError) as exc:
        prflow.push(str(work), "", head, "s1-pr-3")
    assert "no remote" in exc.value.message


# --------------------------------------------------------------------------- #
# gh, scripted
# --------------------------------------------------------------------------- #
class FakeGh:
    """A ``gh`` that answers ``pr view`` from a table and records ``pr create``."""

    def __init__(self, existing=None, create_ok=True):
        self.existing = existing            # dict answered by `pr view <branch>`
        self.create_ok = create_ok
        self.calls = []
        self.bodies = []

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(list(argv))
        assert argv[1] == "pr"
        if argv[2] == "view":
            if self.existing and argv[3] in (self.existing.get("_key"), self.existing.get("url")):
                import json
                return 0, json.dumps({k: v for k, v in self.existing.items() if k != "_key"}), ""
            return 1, "", "no pull requests found for branch"
        if argv[2] == "create":
            body_file = argv[argv.index("--body-file") + 1]
            self.bodies.append(Path(body_file).read_text(encoding="utf-8"))
            if not self.create_ok:
                return 1, "", "GraphQL: A pull request already exists"
            self.existing = {"_key": argv[argv.index("--head") + 1], "number": 7,
                             "url": "https://ghe.example.com/team/proj/pull/7", "state": "OPEN",
                             "headRefOid": "abc", "isDraft": "--draft" in argv, "title": argv[argv.index("--title") + 1]}
            return 0, "https://ghe.example.com/team/proj/pull/7\n", ""
        raise AssertionError(argv)


def test_open_pr_probes_then_creates_with_the_workers_flags(tmp_path):
    gh = FakeGh()
    pr = prflow.open_pr(str(tmp_path), repo="ghe.example.com/team/proj", base="master",
                        branch="s1-pr-1", title="s1: first", body="hello", draft=True, run=gh)
    assert pr["url"].endswith("/pull/7") and pr["number"] == 7 and pr["existed"] is False
    assert pr["isDraft"] is True
    view, create, view2 = gh.calls
    assert view[:4] == ["gh", "pr", "view", "s1-pr-1"] and "-R" in view
    assert create[:3] == ["gh", "pr", "create"]
    assert create[create.index("--base") + 1] == "master"
    assert create[create.index("--head") + 1] == "s1-pr-1"
    assert "--draft" in create
    assert gh.bodies == ["hello"]
    assert view2[3] == pr["url"]


def test_open_pr_reuses_an_open_pull_request(tmp_path):
    gh = FakeGh(existing={"_key": "s1-pr-1", "number": 3, "url": "u/3", "state": "OPEN",
                          "headRefOid": "x", "isDraft": False, "title": "t"})
    pr = prflow.open_pr(str(tmp_path), repo="h/o/r", base="master", branch="s1-pr-1",
                        title="t", body="", run=gh)
    assert pr["existed"] is True and pr["number"] == 3
    assert [c[2] for c in gh.calls] == ["view"]


def test_open_pr_does_not_revive_a_merged_one(tmp_path):
    gh = FakeGh(existing={"_key": "s1-pr-1", "number": 3, "url": "u/3", "state": "MERGED",
                          "headRefOid": "x", "isDraft": False, "title": "t"})
    pr = prflow.open_pr(str(tmp_path), repo="h/o/r", base="master", branch="s1-pr-1",
                        title="t", body="", run=gh)
    assert pr["existed"] is False and pr["number"] == 7


def test_open_pr_reports_a_refused_create(tmp_path):
    gh = FakeGh(create_ok=False)
    with pytest.raises(prflow.PrError) as exc:
        prflow.open_pr(str(tmp_path), repo="h/o/r", base="master", branch="b",
                       title="t", body="", run=gh)
    assert exc.value.step == "pr"
    assert "already exists" in exc.value.message


# --------------------------------------------------------------------------- #
# the whole thing
# --------------------------------------------------------------------------- #
_ORIGINAL_PUSH = prflow.push


@pytest.fixture
def pushes(monkeypatch):
    """``run`` pushes to the remote the form named, and the form names the
    GitHub one -- a URL nobody serves. The push is redirected to the bare
    repository (``origin``) and the remote it was asked for is recorded, so
    what is pinned is the addressing and the outcome, not a network."""
    asked = []

    def redirected(cwd, remote, sha, branch, *, force=False):
        asked.append(remote)
        return _ORIGINAL_PUSH(cwd, "origin", sha, branch, force=force)

    monkeypatch.setattr(prflow, "push", redirected)
    return asked


def test_run_pushes_and_opens_and_reports_every_step(repos, pushes):
    work, remote = repos["work"], repos["remote"]
    (work / "a.txt").write_text("two\n", encoding="utf-8")
    before = _state(work)
    gh = FakeGh()

    res = prflow.run(str(work), {"remote": "gh", "branch": "s1-pr-9", "draft": "true"},
                     session="s1", gh_run=gh, which=lambda _b: None)

    assert res["ok"] is True
    assert [s["id"] for s in res["steps"]] == ["inspect", "snapshot", "push", "pr"]
    assert all(s["ok"] for s in res["steps"])
    assert res["repo"] == "ghe.example.com/team/proj"
    assert res["branch"] == "s1-pr-9" and res["base"] == "master"
    assert res["checkout_branch"] == "master"
    assert res["snapshot"]["created"] is True and res["tip"] == res["snapshot"]["sha"]
    assert res["pr"]["url"].endswith("/pull/7")
    assert pushes == ["gh"]
    assert _git(remote, "rev-parse", "refs/heads/s1-pr-9") == res["tip"]
    create = next(c for c in gh.calls if c[2] == "create")
    assert create[create.index("--title") + 1] == "s1: first"
    assert "--draft" in create
    assert "session `s1`" in gh.bodies[0]
    assert _state(work) == before


def test_run_stops_at_the_failing_step_and_keeps_the_earlier_ones(repos, pushes):
    work, remote = repos["work"], repos["remote"]
    gh = FakeGh(create_ok=False)
    res = prflow.run(str(work), {"remote": "gh", "branch": "s1-pr-11", "title": "T", "body": "B"},
                     session="s1", gh_run=gh, which=lambda _b: None)
    assert res["ok"] is False
    assert res["failed"] == "pr" and "already exists" in res["error"]
    assert [(s["id"], s["ok"]) for s in res["steps"]] == [
        ("inspect", True), ("snapshot", True), ("push", True), ("pr", False)]
    # the push stood: the branch is on the remote, at HEAD (clean tree)
    assert _git(remote, "rev-parse", "refs/heads/s1-pr-11") == _git(work, "rev-parse", "HEAD")
    assert gh.bodies == ["B"]


def test_run_refuses_an_unknown_remote_before_doing_anything(repos, pushes):
    work = repos["work"]
    before = _state(work)
    res = prflow.run(str(work), {"remote": "nope"}, session="s1", gh_run=FakeGh(),
                     which=lambda _b: None)
    assert res["ok"] is False and res["failed"] == "inspect"
    assert "not one of this repository's remotes" in res["error"]
    assert pushes == [] and _state(work) == before


def test_run_refuses_a_remote_without_a_host(repos, pushes):
    work = repos["work"]
    res = prflow.run(str(work), {"remote": "origin"}, session="s1", gh_run=FakeGh(),
                     which=lambda _b: None)
    assert res["ok"] is False and res["failed"] == "inspect"
    assert "GitHub host" in res["error"] and pushes == []


def test_run_needs_gh_when_none_is_injected(repos, pushes):
    work = repos["work"]
    res = prflow.run(str(work), {"remote": "gh"}, session="s1", which=lambda _b: None)
    assert res["ok"] is False and res["failed"] == "inspect"
    assert "gh is not installed" in res["error"] and pushes == []


# --------------------------------------------------------------------------- #
# the block the session is told
# --------------------------------------------------------------------------- #
def test_report_block_says_what_happened_and_that_the_checkout_stands():
    ok = {"ok": True, "cwd": "C:/w", "remote": "gh", "branch": "s1-pr-1",
          "snapshot": {"sha": "a" * 40, "parent": "b" * 40, "created": True, "files": 2},
          "pr": {"url": "https://h/o/r/pull/7", "number": 7, "existed": False, "isDraft": True},
          "steps": []}
    text = prflow.report_block(ok)
    assert text.startswith("---\n# claunch pr:")
    assert "machine-generated" in text
    assert "branch: gh/s1-pr-1" in text
    assert f"tip: {'a' * 40} (a snapshot commit of 2 uncommitted path(s)" in text
    assert "pr: https://h/o/r/pull/7 (#7 opened, draft)" in text
    assert "status: ok" in text
    assert "checkout was not changed" in text
    assert text.endswith("---")

    bad = {"ok": False, "cwd": "C:/w", "remote": "gh", "branch": "s1-pr-1",
           "snapshot": {"sha": "c" * 40, "parent": "c" * 40, "created": False, "files": 0},
           "failed": "pr", "error": "gh pr create: nope",
           "steps": [{"id": "inspect", "ok": True}, {"id": "snapshot", "ok": True},
                     {"id": "push", "ok": True}, {"id": "pr", "ok": False}]}
    text = prflow.report_block(bad)
    assert "tip: " + "c" * 40 + " (HEAD as it stood)" in text
    assert "status: failed at step 'pr' -- gh pr create: nope" in text
    assert "done before that: inspect, snapshot, push" in text
