"""Where a session works after it moved into a worktree (claunch-4zc51.1.2).

A session started at a repository's main checkout keeps that directory as its
recorded ``cwd`` after it moves into a worktree. The enqueue-landing push
measured the recorded directory and sent ``branch=master`` with master's tip
as the session's landing request, which the leader's queue marked landed at
once. These tests pin the transcript reading that finds the real checkout,
the push that now measures it, and the refusal of a request measured on the
integration branch -- on the v2 trigger and on the v1 clock's enqueue-landing,
which kept measuring the run's checkout (claunch-4zc51.1.3).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from claude_launcher.cflow import engine as cflow_engine, landing as cflow_landing
from claude_launcher.daemon import cflow2_triggers, cflow_clock, ctxsize, landing_route, work_checkout
from claude_launcher.daemon.harness import SessionDef


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A main checkout on master and a linked worktree on a feature branch."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "base")
    tree = tmp_path / "wt"
    _git(root, "worktree", "add", "-q", "-b", "w1-feature", str(tree))
    (tree / "b.txt").write_text("b\n", encoding="utf-8")
    _git(tree, "add", "b.txt")
    _git(tree, "commit", "-q", "-m", "feature")
    return root, tree


@pytest.fixture(autouse=True)
def _fresh_caches():
    work_checkout._reads.clear()
    work_checkout._repos.clear()
    yield
    work_checkout._reads.clear()
    work_checkout._repos.clear()


def _transcript(path: Path, *entries) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


# -- reading the transcript -----------------------------------------------------


def test_the_newest_recorded_cwd_wins(tmp_path):
    path = _transcript(tmp_path / "t.jsonl",
                       {"type": "user", "cwd": "C:/root"},
                       {"type": "assistant", "cwd": "C:/root/.claude/worktrees/x"},
                       {"type": "summary"})
    assert work_checkout.last_cwd(path) == "C:/root/.claude/worktrees/x"


def test_a_subagent_entry_says_nothing_about_the_session(tmp_path):
    path = _transcript(tmp_path / "t.jsonl",
                       {"type": "user", "cwd": "C:/root/wt"},
                       {"type": "assistant", "cwd": "C:/elsewhere", "isSidechain": True})
    assert work_checkout.last_cwd(path) == "C:/root/wt"


def test_no_transcript_is_no_answer(tmp_path):
    assert work_checkout.last_cwd(None) is None
    assert work_checkout.last_cwd(tmp_path / "missing.jsonl") is None


# -- accepting the answer ---------------------------------------------------------


def test_a_worktree_of_the_same_repository_is_taken(repo):
    root, tree = repo
    assert work_checkout.resolve(str(root), str(tree)) == str(tree)


def test_the_recorded_directory_itself_is_no_move(repo):
    root, _ = repo
    assert work_checkout.resolve(str(root), str(root)) is None


def test_another_repository_is_not_where_the_work_moved(repo, tmp_path):
    root, _ = repo
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    assert work_checkout.resolve(str(root), str(other)) is None


def test_a_directory_that_is_gone_is_ignored(repo, tmp_path):
    root, _ = repo
    assert work_checkout.resolve(str(root), str(tmp_path / "removed")) is None


def test_of_reads_the_sessions_transcript(repo, tmp_path, monkeypatch):
    root, tree = repo
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(root)}, {"cwd": str(tree)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    sdef = SessionDef(name="w1", cwd=str(root))
    assert work_checkout.of(sdef) == str(tree)
    info = work_checkout.attach({"name": "w1"}, sdef)
    assert info[work_checkout.FIELD] == str(tree)


def test_a_session_that_never_moved_carries_no_field(repo, tmp_path, monkeypatch):
    root, _ = repo
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(root)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    info = work_checkout.attach({"name": "w1"}, SessionDef(name="w1", cwd=str(root)))
    assert work_checkout.FIELD not in info


def test_other_harnesses_are_not_read(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(ctxsize, "transcript_of",
                        lambda sdef: pytest.fail("a codex session has no claude transcript"))
    assert work_checkout.of(SessionDef(name="w1", cwd=str(root), harness="codex")) is None


# -- the landing push ---------------------------------------------------------------


class _Session:
    def __init__(self, name, cwd):
        self.sdef = SessionDef(name=name, cwd=str(cwd))
        self.exited = False


class _Manager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def get(self, name):
        return self.sessions[name]


def test_landing_facts_come_from_the_worktree(repo, tmp_path, monkeypatch):
    """The claunch-4zc51.1.2 shape: the run and the session record sit at the
    main checkout (on master), the work is in a worktree."""
    root, tree = repo
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(root)}, {"cwd": str(tree)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    facts = cflow2_triggers.session_facts(_Manager(_Session("w1", root)))(str(root), "w1")
    assert facts["branch"] == "w1-feature"
    assert facts["head"] == _git(tree, "rev-parse", "HEAD")
    assert facts["head"] != _git(root, "rev-parse", "HEAD")
    assert facts["checkout"] == str(tree)
    assert cflow2_triggers.landing_refusal(facts) is None


def test_without_a_move_the_run_checkout_is_measured(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: None)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    facts = cflow2_triggers.session_facts(_Manager(_Session("w1", root)))(str(root), "w1")
    assert (facts["branch"], facts["checkout"]) == ("master", str(root))


@pytest.mark.parametrize("branch,word", [("master", "integration branch"),
                                         ("main", "integration branch"),
                                         (None, "detached")])
def test_a_request_measured_on_the_trunk_or_detached_is_refused(branch, word):
    facts = {"head": "abc", "branch": branch, "in_review": ["c-1"],
             "session": "w1", "checkout": "C:/repo"}
    assert word in cflow2_triggers.landing_refusal(facts)


def test_a_tip_already_in_the_trunk_is_refused_on_a_feature_branch(repo):
    """The name is not a trunk, but the tip is master's: nothing to land, and
    the queue would mark it landed at once."""
    root, tree = repo
    facts = {"head": _git(root, "rev-parse", "HEAD"), "branch": "w1-feature",
             "in_review": ["c-1"], "session": "w1", "checkout": str(tree)}
    assert "already contained in master" in cflow2_triggers.landing_refusal(facts)
    facts["head"] = _git(tree, "rev-parse", "HEAD")
    assert cflow2_triggers.landing_refusal(facts) is None


def test_a_session_that_cd_ed_to_the_trunk_is_measured_in_its_run_checkout(
        repo, tmp_path, monkeypatch):
    """Started in the worktree, last cd-ed to the main checkout: the trunk
    reading is not taken over the feature branch the run stands on."""
    root, tree = repo
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(tree)}, {"cwd": str(root)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    facts = cflow2_triggers.session_facts(_Manager(_Session("w1", tree)))(str(tree), "w1")
    assert (facts["branch"], facts["checkout"]) == ("w1-feature", str(tree))
    assert cflow2_triggers.landing_refusal(facts) is None


def test_a_detached_scratch_worktree_does_not_displace_the_run_checkout(
        repo, tmp_path, monkeypatch):
    root, tree = repo
    scratch = tmp_path / "scratch"
    _git(root, "worktree", "add", "-q", "--detach", str(scratch))
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(tree)}, {"cwd": str(scratch)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    facts = cflow2_triggers.session_facts(_Manager(_Session("w1", tree)))(str(tree), "w1")
    assert (facts["branch"], facts["checkout"]) == ("w1-feature", str(tree))


def test_a_trunk_refusal_says_how_to_retry():
    facts = {"head": "abc", "branch": "master", "checkout": "C:/repo"}
    assert cflow2_triggers.RETRY_HINT in cflow2_triggers.landing_refusal(facts)


def _v1_clock(manager, monkeypatch):
    """A v1 trigger clock whose target is a v1 parent queue; the filed
    requests are collected instead of written."""
    filed = []
    clock = cflow_clock.TriggerClock(manager)
    monkeypatch.setattr(clock, "landing_target", lambda cwd, scope: landing_route.Target(
        owner="lead", holder=landing_route.Holder(cwd="C:/lead", run=None, main=True)))
    monkeypatch.setattr(cflow_engine, "enqueue_landing",
                        lambda issues, branch, tip, **kw: filed.append((branch, tip))
                        or {"added": list(issues)})
    return clock, filed


def test_the_v1_trigger_files_the_worktree_of_a_root_anchored_run(repo, tmp_path, monkeypatch):
    """claunch-4zc51.1.3: the v1 route measured the run's checkout (master)."""
    root, tree = repo
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(root)}, {"cwd": str(tree)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    clock, filed = _v1_clock(_Manager(_Session("w1", root)), monkeypatch)
    ok, detail = asyncio.run(clock._enqueue_landing(str(root), "w1"))
    assert ok, detail
    assert filed == [("w1-feature", _git(tree, "rev-parse", "HEAD"))]


def test_the_v1_trigger_files_nothing_measured_on_master(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: None)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    clock, filed = _v1_clock(_Manager(_Session("w1", root)), monkeypatch)
    ok, detail = asyncio.run(clock._enqueue_landing(str(root), "w1"))
    assert not ok
    assert "integration branch itself" in detail and "nothing was pushed" in detail
    assert filed == []


def test_the_enqueue_landing_trigger_pushes_nothing_from_master(tmp_path):
    """Refused before any parent is looked up: the manager here knows no
    parent at all, so a push attempt would fail with a different reason."""
    facts = {"head": "abc", "branch": "master", "in_review": ["c-1"], "session": "w1",
             "checkout": str(tmp_path)}
    ok, detail = asyncio.run(cflow2_triggers.perform(
        _Manager(_Session("w1", tmp_path)), str(tmp_path), "w1",
        {"do": "enqueue-landing"}, driver="w1", facts_fn=lambda c, s: dict(facts)))
    assert not ok
    assert "integration branch itself" in detail and "nothing was pushed" in detail


@pytest.mark.parametrize("to", ["parent:landing", "leader:landing"])
def test_a_landing_push_trigger_pushes_nothing_from_master(tmp_path, to):
    """claunch-c4zwi: improv-worker-v2 files its request with ``do: push`` to
    ``parent:landing`` / ``leader:landing``, not ``enqueue-landing``. That
    push skipped the guard, and a request measured on master (tip 33a57783)
    reached the leader's queue and was marked landed at once."""
    facts = {"head": "abc", "branch": "master", "in_review": ["c-1"], "session": "w1",
             "checkout": str(tmp_path)}
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to=to)
    ok, detail = asyncio.run(cflow2_triggers.perform(
        _Manager(_Session("w1", tmp_path)), str(tmp_path), "w1",
        {"do": "push", "push": push}, driver="w1", facts_fn=lambda c, s: dict(facts)))
    assert not ok
    assert "integration branch itself" in detail and "nothing was pushed" in detail


def test_a_push_to_another_collection_is_not_a_landing_request(tmp_path):
    """The guard is for landing requests: a push of the same facts to a
    collection that is not ``landing`` is not refused for the branch."""
    facts = {"head": "abc", "branch": "master", "in_review": ["c-1"], "session": "w1",
             "checkout": str(tmp_path)}
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="parent:notes")
    ok, detail = asyncio.run(cflow2_triggers.perform(
        _Manager(_Session("w1", tmp_path)), str(tmp_path), "w1",
        {"do": "push", "push": push}, driver="w1", facts_fn=lambda c, s: dict(facts)))
    assert not ok
    assert "integration branch itself" not in detail

# -- a move the transcript never recorded (claunch-1o6cs) ----------------------

#: The shape a spawned worker hits: the run's cwd is the PARENT's checkout
#: (spawn inherits it) and the round's branch lives in a worktree cut for the
#: session -- `s1803-harness-pw164` for `s1803`. Nothing in the transcript
#: says so: every entry records the inherited cwd, because the session reaches
#: the other checkout with `git -C` rather than by standing in it. Measured on
#: that session: 1154 cwd entries, all of them the parent's worktree.
#:
#: The landing request then carried the PARENT's branch and tip (7b78b56c of
#: s1762-harness-flake instead of cf4957c0 of s1803-skip-receipt), and the
#: leader held the merge on the mismatch.


@pytest.fixture
def parent_and_worker(tmp_path):
    """A repository, the parent's checkout, and the worker's own worktree.

    ``s1`` is the parent session; ``s2`` the worker spawned into ``s1``'s
    checkout, whose round branch was cut in ``s2-round``.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "base")
    parent = tmp_path / "s1-checkout"
    _git(root, "worktree", "add", "-q", "-b", "s1-round", str(parent))
    (parent / "p.txt").write_text("p\n", encoding="utf-8")
    _git(parent, "add", "p.txt")
    _git(parent, "commit", "-q", "-m", "parent round")
    worker = tmp_path / "s2-round"
    _git(root, "worktree", "add", "-q", "-b", "s2-round", str(worker))
    (worker / "w.txt").write_text("w\n", encoding="utf-8")
    _git(worker, "add", "w.txt")
    _git(worker, "commit", "-q", "-m", "worker round")
    return root, parent, worker


def _spawned(session, checkout, parent="s1"):
    return SessionDef(name=session, cwd=str(checkout), parent=parent)


def test_the_sessions_own_worktree_is_found_by_its_branch(
        parent_and_worker, tmp_path, monkeypatch):
    """The round branch is `<session>-...`; its checkout is the session's own,
    even though the transcript never left the inherited directory."""
    _root, parent, worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)}, {"cwd": str(parent)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    sdef = _spawned("s2", parent)
    assert work_checkout.of(sdef) == str(worker)


def test_a_branch_of_another_session_is_not_taken(parent_and_worker, tmp_path, monkeypatch):
    """`s1-round` belongs to the parent. A session called `s2` must not claim
    it by prefix, and must not fall through to it either."""
    _root, parent, _worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s3", parent)) is None


def test_a_prefix_that_is_only_a_string_prefix_is_not_taken(
        parent_and_worker, tmp_path, monkeypatch):
    """`s2-round` is the branch; a session named `s2-r` must not match it --
    the separator, not the letters, is what makes it the session's own."""
    _root, parent, _worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s2-r", parent)) is None


def test_the_recorded_move_still_wins_over_the_branch_guess(
        parent_and_worker, tmp_path, monkeypatch):
    """A transcript that DOES record the move is the better evidence: it is
    what the session stood in, while a branch-prefix checkout is an inference.
    Both must agree here, and the answer must not depend on which is read."""
    _root, parent, worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)}, {"cwd": str(worker)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s2", parent)) == str(worker)


def test_a_detached_worktree_is_not_the_round_the_session_works_in(
        parent_and_worker, tmp_path, monkeypatch):
    """A `git worktree add --detach` scratch tree has no branch, so it says
    nothing about which round the session is on. With the session's own round
    worktree gone, the only checkout left is the parent's and a detached
    scratch -- the answer must be None, not the scratch tree."""
    root, parent, worker = parent_and_worker
    _git(root, "worktree", "remove", str(worker))
    scratch = tmp_path / "s2-scratch"
    _git(root, "worktree", "add", "-q", "--detach", str(scratch))
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s2", parent)) is None


def test_a_main_checkout_session_does_not_adopt_a_leftover_round_branch(
        parent_and_worker, tmp_path, monkeypatch):
    """Session names are reused after `clear-sessions` (worktree.child_name),
    so a session standing at the repository's main checkout must not be given
    a same-named round branch some earlier session left behind -- it would
    wake on someone else's branch."""
    root, _parent, _worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(root)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s2", root)) is None


def test_the_landing_facts_of_a_spawned_worker_are_its_own_branch(
        parent_and_worker, tmp_path, monkeypatch):
    """The defect as the leader saw it: the queue row carried the parent's
    branch and tip. It must carry the worker's."""
    _root, parent, worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(parent)}, {"cwd": str(parent)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, s: ["c-1"])
    facts = cflow2_triggers.session_facts(_Manager(_Session("s2", parent)))(str(parent), "s2")
    assert facts["branch"] == "s2-round"
    assert facts["head"] == _git(worker, "rev-parse", "HEAD")
    assert facts["head"] != _git(parent, "rev-parse", "HEAD")
    assert cflow2_triggers.landing_refusal(facts) is None


def test_a_session_already_on_its_own_round_branch_has_not_moved(
        parent_and_worker, tmp_path, monkeypatch):
    """The worker's own checkout is where it was started and stands on its own
    round branch: there is no move to report. Searching anyway would answer
    with an OLDER round's checkout the same session left behind."""
    _root, _parent, worker = parent_and_worker
    path = _transcript(tmp_path / "t.jsonl", {"cwd": str(worker)})
    monkeypatch.setattr(ctxsize, "transcript_of", lambda sdef: path)
    assert work_checkout.of(_spawned("s2", worker)) is None
