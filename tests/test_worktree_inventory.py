"""The Worktrees page's reading and its removal (daemon/worktree_inventory.py).

What the page promises the operator: every launcher worktree with the
sessions standing in it, whether those sessions are archived, whether the
branch is already in the trunk -- and that removing one never deletes more
than the checkout: not the branch, not a checkout a session can still come
back into (unless it is archived in the same act), and never the contents of
a tree a link inside the checkout points at (claunch-4m2s9).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_launcher import worktree
from claude_launcher.daemon import api
from claude_launcher.daemon import worktree_inventory as inv

pytestmark = pytest.mark.worktree


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _build_repo(root):
    git("init", "-q", "-b", "master", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)


@pytest.fixture(autouse=True)
def plain_worktree_dir(monkeypatch):
    monkeypatch.delenv(worktree.WORKTREE_DIR_ENV, raising=False)


@pytest.fixture
def repo(tmp_path, repo_template):
    return repo_template("inventory", _build_repo, tmp_path / "repo")


def _link(target: Path, link: Path) -> None:
    """A directory link the way Windows tooling makes one (a junction), or a
    symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def test_listing_joins_sessions_and_reads_the_merge_state(repo):
    busy = worktree.create(repo, "busy")
    done = worktree.create(repo, "done")
    bare = worktree.create(repo, "bare")
    # `busy` carries a commit master does not have; the other two carry none.
    (busy.path / "b.txt").write_text("b\n", encoding="utf-8")
    git("add", "-A", cwd=busy.path)
    git("commit", "-qm", "work", cwd=busy.path)
    sessions = [
        {"name": "s1", "cwd": str(busy.path), "category": "running"},
        {"name": "s2", "cwd": str(done.path / "sub"), "category": "archived"},
        {"name": "s3", "cwd": str(done.path), "category": "archived"},
        {"name": "far", "cwd": str(repo), "category": "running"},
    ]
    answer = inv.list_repo(str(repo), sessions)
    assert answer["trunk"] == "master"
    by = {w["name"]: w for w in answer["worktrees"]}
    # The main checkout is not a launcher worktree and is not listed.
    assert set(by) == {"busy", "done", "bare"}

    assert by["busy"]["state"] == inv.STATE_ACTIVE
    assert [s["name"] for s in by["busy"]["sessions"]] == ["s1"]
    assert by["busy"]["merged"] is False and by["busy"]["ahead"] == 1

    # Every joined session archived: orphaned. A session in a subdirectory
    # of the checkout belongs to it too.
    assert by["done"]["state"] == inv.STATE_ORPHANED
    assert {s["name"] for s in by["done"]["sessions"]} == {"s2", "s3"}
    assert by["done"]["merged"] is True and by["done"]["ahead"] == 0

    assert by["bare"]["state"] == inv.STATE_UNLINKED
    assert by["bare"]["branch"] == "bare"
    assert by["bare"]["created_at"]


def test_repo_roots_come_from_a_worktree_path_without_git(repo, monkeypatch):
    wt = worktree.create(repo, "x")
    calls = []
    real = worktree.repo_root
    monkeypatch.setattr(worktree, "repo_root",
                        lambda cwd: calls.append(cwd) or real(cwd))
    inv._ROOT_CACHE.clear()
    roots = inv.repo_roots([str(wt.path), str(wt.path / "deeper"), "", "Z:/nope"])
    assert [inv._key(r) for r in roots] == [inv._key(str(repo))]
    assert calls == []  # the worktree marker answered; no git was run


def test_remove_keeps_the_branch_and_refuses_uncommitted_work_without_force(repo):
    wt = worktree.create(repo, "dirty")
    (wt.path / "scratch.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(inv.RemoveError):
        inv.remove(str(repo), str(wt.path))
    assert wt.path.is_dir()

    inv.remove(str(repo), str(wt.path), force=True)
    assert not wt.path.exists()
    assert git("branch", "--list", "dirty", cwd=repo).stdout.strip()
    assert "dirty" not in [w["name"] for w in inv.list_repo(str(repo), [])["worktrees"]]


def test_remove_unlinks_a_link_and_leaves_its_target_alone(repo, tmp_path):
    """claunch-4m2s9: git worktree remove --force deleted through a junction."""
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep\n", encoding="utf-8")
    wt = worktree.create(repo, "borrower")
    _link(precious, wt.path / "node_modules")

    done = inv.remove(str(repo), str(wt.path), force=True)
    assert not wt.path.exists()
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert [Path(p).name for p in done["unlinked"]] == ["node_modules"]


def test_find_answers_only_launcher_worktrees(repo):
    wt = worktree.create(repo, "one")
    assert inv.find([str(repo)], str(wt.path))[0] == str(repo)
    assert inv.find([str(repo)], str(repo)) is None


# --------------------------------------------------------------------------- #
# the route: the archive gate
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, name, cwd, category):
        self.sdef = type("D", (), {"name": name, "cwd": cwd})()
        self.category = category
        self.created_at = "2026-01-01T00:00:00+00:00"
        self.archived_at = "x" if category == "archived" else None
        self.paused_at = "x" if category == "paused" else None

    @property
    def exited(self):
        return self.category != "running"

    def status(self):
        return "idle"


class _Manager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.stopped = []

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        return self.sessions[name]

    def archive(self, name):
        s = self.sessions[name]
        s.category, s.archived_at = "archived", "now"
        return s

    async def stop_and_archive(self, name, force=False):
        self.stopped.append(name)
        return self.archive(name)


class _Request:
    def __init__(self, manager, body):
        forget = type("H", (), {"forget": lambda self, name: None})()
        beads = type("B", (), {"winddowns": {}})()
        self.app = {"manager": manager, "handoff": forget, "beads": beads}
        self._body = body

    async def json(self):
        return self._body


def _call(manager, body, monkeypatch, repo):
    import json

    monkeypatch.setattr(api, "_worktree_roots", lambda sessions: [str(repo)])
    response = asyncio.run(api.h_worktrees_remove(_Request(manager, body)))
    return response.status, json.loads(response.text)


def test_route_refuses_a_checkout_with_live_sessions_unless_archiving(repo, monkeypatch):
    live = worktree.create(repo, "live")
    gone = worktree.create(repo, "gone")
    manager = _Manager([
        _Session("run", str(live.path), "running"),
        _Session("paused", str(live.path), "paused"),
        _Session("old", str(gone.path), "archived"),
    ])
    status, body = _call(
        manager, {"paths": [str(live.path), str(gone.path)]}, monkeypatch, repo
    )
    assert status == 200
    assert [inv._key(r["path"]) for r in body["removed"]] == [inv._key(str(gone.path))]
    assert body["failed"][0]["sessions"] == ["run", "paused"]
    assert live.path.is_dir() and not gone.path.exists()
    assert body["archived"] == []

    status, body = _call(
        manager, {"paths": [str(live.path)], "archive": True}, monkeypatch, repo
    )
    assert status == 200 and len(body["removed"]) == 1
    assert body["archived"] == ["run", "paused"]
    assert manager.stopped == ["run"]  # only the running one needed ending
    assert all(s.category == "archived" for s in manager.list())
    assert not live.path.exists()


def test_route_rejects_a_path_that_is_not_a_launcher_worktree(repo, monkeypatch):
    status, body = _call(_Manager([]), {"paths": [str(repo)]}, monkeypatch, repo)
    assert status == 200 and body["removed"] == []
    assert body["failed"][0]["error"] == "not a launcher worktree"
    status, _ = _call(_Manager([]), {"paths": []}, monkeypatch, repo)
    assert status == 400


def test_route_lists_the_worktrees_with_their_sessions(repo, monkeypatch):
    import json

    wt = worktree.create(repo, "listed")
    manager = _Manager([_Session("s9", str(wt.path), "killed")])
    monkeypatch.setattr(api, "_worktree_roots", lambda sessions: [str(repo)])
    response = asyncio.run(api.h_worktrees(_Request(manager, None)))
    body = json.loads(response.text)
    assert body["errors"] == []
    (entry,) = body["repos"][0]["worktrees"]
    assert entry["name"] == "listed" and entry["state"] == inv.STATE_ACTIVE
    assert entry["sessions"][0]["name"] == "s9"
    assert entry["sessions"][0]["category"] == "killed"
