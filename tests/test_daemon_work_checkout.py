"""Where a session works after it moved into a worktree (claunch-4zc51.1.2).

A session started at a repository's main checkout keeps that directory as its
recorded ``cwd`` after it moves into a worktree. The enqueue-landing push
measured the recorded directory and sent ``branch=master`` with master's tip
as the session's landing request, which the leader's queue marked landed at
once. These tests pin the transcript reading that finds the real checkout,
the push that now measures it, and the refusal of a request measured on the
integration branch.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from claude_launcher.cflow import landing as cflow_landing
from claude_launcher.daemon import cflow2_triggers, ctxsize, work_checkout
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
