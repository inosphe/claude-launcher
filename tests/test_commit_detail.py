"""The commit page's reading (claunch-tb145): one commit, read whole.

Two surfaces, one group each:

* :mod:`claude_launcher.commit_detail` -- the metadata, the file list with
  its counts, the diff split into numbered lines, the caps and the refusals.
* ``GET /api/commit`` -- which repository a request may name: a session's,
  or a directory of a repository the fleet already works in, and nothing
  else.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from claude_launcher import commit_detail
from claude_launcher.daemon import api


def git(*args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout


def _build_repo(r: Path) -> None:
    git("init", "-q", "-b", "master", cwd=r)
    git("config", "user.email", "t@example.com", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "keep.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    (r / "gone.txt").write_text("bye\n", encoding="utf-8")
    (r / "old name.txt").write_text("".join(f"line {i}\n" for i in range(20)),
                                    encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-qm", "root commit", cwd=r)


@pytest.fixture
def repo(tmp_path, monkeypatch, repo_template):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return repo_template("commit-detail", _build_repo, tmp_path / "repo")


def head(repo) -> str:
    return git("rev-parse", "HEAD", cwd=repo).strip()


def change_everything(repo) -> str:
    """One commit with each kind of change the page draws."""
    (repo / "keep.txt").write_text("one\nTWO\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    (repo / "gone.txt").unlink()
    git("mv", "old name.txt", "new name.txt", cwd=repo)
    (repo / "added.txt").write_text("fresh", encoding="utf-8")  # no final newline
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "change everything\n\nWhy it changed.\n\n"
        "Claunch-Session: s833\nCo-Authored-By: X <x@example.com>", cwd=repo)
    return head(repo)


def by_path(detail):
    return {f["path"]: f for f in detail["files"]}


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #
def test_metadata_message_and_trailers(repo):
    sha = change_everything(repo)
    d = commit_detail.read(repo, sha[:8])
    assert d["sha"] == sha
    assert d["subject"] == "change everything"
    assert d["body"].startswith("Why it changed.")
    assert d["author"]["name"] == "t" and d["author"]["email"] == "t@example.com"
    assert d["parents"] == [git("rev-parse", "HEAD^", cwd=repo).strip()]
    assert d["merge"] is False
    assert {"key": "Claunch-Session", "value": "s833"} in d["trailers"]
    assert {"key": "Co-Authored-By", "value": "X <x@example.com>"} in d["trailers"]


def test_every_kind_of_file_change_is_listed_with_its_counts(repo):
    d = commit_detail.read(repo, change_everything(repo))
    files = by_path(d)
    assert files["keep.txt"]["status"] == "modified"
    assert (files["keep.txt"]["additions"], files["keep.txt"]["deletions"]) == (2, 1)
    assert files["gone.txt"]["status"] == "deleted"
    assert files["new name.txt"]["status"] == "renamed"
    assert files["new name.txt"]["old_path"] == "old name.txt"
    assert files["new name.txt"]["hunks"] == []  # a pure rename has no lines
    assert files["added.txt"]["status"] == "added"
    assert files["blob.bin"]["binary"] is True
    assert files["blob.bin"]["hunks"] == []
    assert d["stats"] == {"files": 5, "additions": 3, "deletions": 2}
    assert d["truncated"] is False


def test_lines_carry_their_old_and_new_numbers(repo):
    d = commit_detail.read(repo, change_everything(repo))
    (hunk,) = by_path(d)["keep.txt"]["hunks"]
    assert hunk["old_start"] == 1 and hunk["new_start"] == 1
    assert hunk["lines"] == [
        [" ", 1, 1, "one"],
        ["-", 2, None, "two"],
        ["+", None, 2, "TWO"],
        [" ", 3, 3, "three"],
        [" ", 4, 4, "four"],
        [" ", 5, 5, "five"],
        ["+", None, 6, "six"],
    ]
    added = by_path(d)["added.txt"]["hunks"][0]["lines"]
    assert added[0] == ["+", None, 1, "fresh"]
    assert added[1][0] == "\\"  # "No newline at end of file"


def test_a_root_commit_is_diffed_against_the_empty_tree(repo):
    d = commit_detail.read(repo, head(repo))
    assert d["parents"] == [] and d["base"] is None
    assert {f["status"] for f in d["files"]} == {"added"}
    assert d["stats"]["additions"] == 5 + 1 + 20


def test_a_merge_is_diffed_against_its_first_parent(repo):
    git("switch", "-qc", "side", cwd=repo)
    (repo / "side.txt").write_text("from side\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "side work", cwd=repo)
    git("switch", "-q", "master", cwd=repo)
    (repo / "main.txt").write_text("from master\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "master work", cwd=repo)
    git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=repo)
    d = commit_detail.read(repo, head(repo))
    assert d["merge"] is True and len(d["parents"]) == 2
    assert d["base"] == d["parents"][0]
    assert [f["path"] for f in d["files"]] == ["side.txt"]


def test_a_long_file_is_cut_and_says_so(repo, monkeypatch):
    monkeypatch.setattr(commit_detail, "MAX_FILE_LINES", 5)
    d = commit_detail.read(repo, head(repo))
    big = by_path(d)["old name.txt"]
    assert sum(len(h["lines"]) for h in big["hunks"]) == 5
    assert big["truncated"] is True and d["truncated"] is True
    # The counts come from numstat, so they stay whole when the lines are cut.
    assert big["additions"] == 20


def test_the_byte_cap_kills_git_and_lists_the_rest_without_lines(repo, monkeypatch):
    monkeypatch.setattr(commit_detail, "MAX_PATCH_BYTES", 300)
    d = commit_detail.read(repo, head(repo))
    assert d["truncated"] is True
    assert len(d["files"]) == 3  # every file is still listed
    omitted = [f for f in d["files"] if f["omitted"]]
    assert omitted and all(f["hunks"] == [] and f["truncated"] for f in omitted)


@pytest.mark.parametrize("sha", ["", "abc", "HEAD", "HEAD~1", "--output=x", "g" * 8,
                                 "a" * 65])
def test_anything_but_hex_is_refused_before_git(repo, sha):
    with pytest.raises(commit_detail.CommitError) as exc:
        commit_detail.read(repo, sha)
    assert exc.value.status == 400


def test_an_unknown_commit_is_404_and_not_a_repository_is_400(repo, tmp_path):
    with pytest.raises(commit_detail.CommitError) as exc:
        commit_detail.read(repo, "deadbeef" * 5)
    assert exc.value.status == 404
    blob = git("rev-parse", "HEAD:keep.txt", cwd=repo).strip()
    with pytest.raises(commit_detail.CommitError) as exc:
        commit_detail.read(repo, blob)  # an object, but not a commit
    assert exc.value.status == 404
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(commit_detail.CommitError) as exc:
        commit_detail.read(plain, "abcd1234")
    assert exc.value.status == 400


def test_parse_patch_keeps_a_diff_git_line_inside_content_as_content():
    text = (
        "diff --git a/x b/x\n"
        "index 1..2 100644\n--- a/x\n+++ b/x\n"
        "@@ -1,2 +1,2 @@ def f():\n"
        "-diff --git a/y b/y\n"
        "+kept\n"
        " ctx\n"
    )
    (f,) = commit_detail.parse_patch(text)
    (h,) = f["hunks"]
    assert h["context"] == "def f():"
    assert h["lines"] == [["-", 1, None, "diff --git a/y b/y"],
                          ["+", None, 1, "kept"], [" ", 2, 2, "ctx"]]


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, name, cwd):
        self.sdef = type("D", (), {"name": name, "cwd": cwd})()
        self.created_at = "2026-01-01T00:00:00+00:00"
        self.exited = False

    def status(self):
        return "idle"


class _Manager:
    def __init__(self, sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        if name not in self.sessions:
            raise api.ManagerError(f"no session named {name!r}")
        return self.sessions[name]


class _Request:
    def __init__(self, manager, query):
        self.app = {"manager": manager}
        self.query = query


def call(manager, **query):
    response = asyncio.run(api.h_commit(_Request(manager, query)))
    return response.status, json.loads(response.text)


def test_route_reads_a_commit_by_session(repo, monkeypatch):
    sha = change_everything(repo)
    monkeypatch.setattr(api, "_worktree_roots", lambda sessions: [str(repo)])
    status, body = call(_Manager([_Session("s1", str(repo))]), sha=sha[:7], session="s1")
    assert status == 200
    assert body["sha"] == sha and body["session"] == "s1"
    assert body["stats"]["files"] == 5
    status, body = call(_Manager([]), sha=sha, session="nobody")
    assert status == 404


def test_route_by_cwd_only_inside_a_known_repository(repo, tmp_path, monkeypatch):
    sha = head(repo)
    manager = _Manager([])
    monkeypatch.setattr(api, "_worktree_roots", lambda sessions: [str(repo)])
    status, body = call(manager, sha=sha, cwd=str(repo))
    assert status == 200 and body["sha"] == sha
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", cwd=other)
    status, _ = call(manager, sha=sha, cwd=str(other))
    assert status == 404
    status, _ = call(manager, sha=sha)
    assert status == 400
    status, body = call(manager, sha="HEAD", cwd=str(repo))
    assert status == 400 and "hex" in body["error"]
