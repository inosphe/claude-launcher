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
    # The missing directory is absolute on every platform. "Z:/nope" was
    # relative on POSIX: abspath put it under the cwd, and a suite run from a
    # launcher worktree found the .claude/worktrees marker in that and got
    # the main checkout back (claunch-ob8sz.1).
    gone = str(repo.parent / "gone")
    roots = inv.repo_roots([str(wt.path), str(wt.path / "deeper"), "", gone])
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


def test_changes_reads_staged_unstaged_untracked_and_renames(repo):
    wt = worktree.create(repo, "changes").path
    assert inv.changes(str(wt)) == []
    git("mv", "a.txt", "이름 변경.txt", cwd=wt)
    (wt / "이름 변경.txt").write_text("changed\n", encoding="utf-8")
    (wt / "new.txt").write_text("staged\n", encoding="utf-8")
    git("add", "new.txt", cwd=wt)
    (wt / "nested").mkdir()
    (wt / "nested" / "한 글.txt").write_text("untracked\n", encoding="utf-8")
    (wt / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (wt / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    by = {f["path"]: f for f in inv.changes(str(wt))}
    assert by["이름 변경.txt"] == {"path": "이름 변경.txt", "status": "RM", "original_path": "a.txt"}
    assert by["new.txt"]["status"] == "A "
    assert by["nested/한 글.txt"]["status"] == "??"
    assert "ignored.txt" not in by
    (wt / "new.txt").unlink()
    assert {f["path"]: f["status"] for f in inv.changes(str(wt))}["new.txt"] == "AD"


def test_changes_preserves_nul_delimited_paths_and_conflicts(monkeypatch):
    def status(args, cwd):
        assert "-z" in args and "--no-optional-locks" in args
        return subprocess.CompletedProcess(args, 0, ' M line\nbreak.txt\0UU conflict.txt\0?? <tag>.txt\0', '')
    monkeypatch.setattr(inv, "_git", status)
    assert inv.changes("unused") == [
        {"path": "line\nbreak.txt", "status": " M"},
        {"path": "conflict.txt", "status": "UU"},
        {"path": "<tag>.txt", "status": "??"},
    ]


def test_changes_failure_is_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(inv, "_git", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "timeout"))
    with pytest.raises(worktree.WorktreeError, match="timeout"):
        inv.changes("unused")


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

    async def stop_and_archive(self, name, force=False, by=None):
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


def test_changes_route_validates_path_and_returns_files(repo, monkeypatch):
    import json

    wt = worktree.create(repo, "read-changes").path
    (wt / "a.txt").write_text("modified\n", encoding="utf-8")
    request = _Request(_Manager([]), {})
    monkeypatch.setattr(api, "_worktree_roots", lambda sessions: [str(repo)])
    request.query = {"path": str(wt)}
    response = asyncio.run(api.h_worktree_changes(request))
    assert response.status == 200
    assert json.loads(response.text)["files"] == [{"path": "a.txt", "status": " M"}]
    request.query = {"path": str(repo)}
    assert asyncio.run(api.h_worktree_changes(request)).status == 404
    request.query = {}
    assert asyncio.run(api.h_worktree_changes(request)).status == 400
    request.query = {"path": str(wt)}
    monkeypatch.setattr(inv, "changes", lambda path: (_ for _ in ()).throw(worktree.WorktreeError("failed")))
    response = asyncio.run(api.h_worktree_changes(request))
    assert response.status == 500
    assert json.loads(response.text)["error"] == "failed"


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


def test_a_refused_removal_leaves_the_links_in_place(repo, tmp_path):
    """claunch-0n31r: without force, a dirty checkout is refused before any
    link inside it is touched."""
    target = tmp_path / "shared"
    target.mkdir()
    wt = worktree.create(repo, "keeps-links")
    _link(target, wt.path / "node_modules")
    (wt.path / "note.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(inv.RemoveError, match="--force"):
        inv.remove(str(repo), str(wt.path))
    assert (wt.path / "node_modules").exists()
    assert os.path.samefile(wt.path / "node_modules", target)


def test_route_reports_sessions_archived_before_a_later_one_failed(repo, monkeypatch):
    wt = worktree.create(repo, "half")
    manager = _Manager([
        _Session("first", str(wt.path), "killed"),
        _Session("second", str(wt.path), "running"),
    ])

    async def refuse(name, force=False):
        raise RuntimeError("will not stop")

    manager.stop_and_archive = refuse
    status, body = _call(
        manager, {"paths": [str(wt.path)], "archive": True}, monkeypatch, repo
    )
    assert status == 200 and body["removed"] == []
    assert body["archived"] == ["first"]
    assert body["failed"][0]["sessions"] == ["second"]
    assert "second" in body["failed"][0]["error"]
    assert wt.path.is_dir()


# --------------------------------------------------------------------------- #
# claunch worktree prune: which checkouts it may remove (cli_worktree.select)
# --------------------------------------------------------------------------- #
def _wt_row(**over):
    row = {
        "name": "s1-old", "path": "/tmp/x/s1-old", "root": "/tmp/x", "branch": "s1-old",
        "head": "0" * 12, "detached": False, "is_trunk": False, "locked": False,
        "missing": False, "sessions": [], "state": inv.STATE_UNLINKED,
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    row.update(over)
    return row


def _select(row, **over):
    from claude_launcher import cli_worktree

    kwargs = {"older_than_days": 7.0, "age_days": 30.0, "dirty": False,
              "reachable": True, "cwd": "/tmp/elsewhere"}
    kwargs.update(over)
    return cli_worktree.select(row, **kwargs)


def test_prune_selects_the_finished_old_clean_checkout():
    from claude_launcher import cli_worktree

    # No session at all (the 308 under the session scratch trees), or only
    # sessions that have ended for good: removable when the rest holds.
    assert _select(_wt_row()) == []
    for category in ("archived", "killed", "failed"):
        row = _wt_row(sessions=[{"name": "s9", "category": category}])
        assert _select(row) == [], category
    assert cli_worktree.RUNNING_CATEGORIES == {"running", "hold", "delayed", "paused"}


def test_prune_keeps_what_can_come_back():
    from claude_launcher import cli_worktree

    # The four partitions that read as Running on both surfaces. hold and
    # paused are the two the operator called out (claunch-25bvn): a held
    # session is asleep and a message wakes it into the same directory.
    for category in sorted(cli_worktree.RUNNING_CATEGORIES):
        row = _wt_row(sessions=[{"name": "s9", "category": category}])
        reasons = _select(row)
        assert any("s9" in r and category in r for r in reasons), (category, reasons)
    # An unreadable category is not a licence to delete.
    assert _select(_wt_row(sessions=[{"name": "s9", "category": ""}]))

    assert _select(_wt_row(), dirty=True)
    assert _select(_wt_row(), dirty=None)  # git status failed
    assert _select(_wt_row(), reachable=False)
    assert _select(_wt_row(), age_days=6.9)
    assert _select(_wt_row(), age_days=None)
    assert _select(_wt_row(locked=True))
    assert _select(_wt_row(is_trunk=True))
    # ...and never the checkout this command is standing in.
    assert _select(_wt_row(), cwd="/tmp/x/s1-old")


def test_prune_reads_age_from_the_directory_not_the_creation_stamp(repo):
    """A checkout made long ago but written to today is recent: the mtime is
    the question --older-than asks, and the creation stamp is the fallback."""
    from claude_launcher import cli_worktree

    wt = worktree.create(repo, "touched")
    row = {"path": str(wt.path), "created_at": "2020-01-01T00:00:00+00:00"}
    now = cli_worktree.time_now()
    assert cli_worktree._age_days(row, now) < 1.0
    assert cli_worktree._age_days({"path": str(repo / "gone"),
                                   "created_at": row["created_at"]}, now) > 365


def test_listing_can_widen_its_bases_to_the_session_scratch_tree(repo, tmp_path):
    """The page lists only <repo>/.claude/worktrees; a checkout cut into a
    session's scratch tree is a worktree of the same repository and is listed
    when that directory is passed as a base (claunch-rc6z8)."""
    scratch = tmp_path / "sessions" / "s1" / "scratch"
    scratch.mkdir(parents=True)
    cut = scratch / "preview"
    git("worktree", "add", "-q", str(cut), "-b", "preview-branch", cwd=repo)

    assert "s1/scratch/preview" not in [
        w["name"] for w in inv.list_repo(str(repo), [])["worktrees"]
    ]
    bases = inv.default_bases(str(repo)) + [str(tmp_path / "sessions")]
    names = [w["name"] for w in inv.list_repo(str(repo), [], bases=bases)["worktrees"]]
    assert "s1/scratch/preview" in names
    assert "preview" not in names  # the name is the base it was cut into
