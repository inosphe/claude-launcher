"""The workspace an issue records (``daemon/beads.py`` + the dashboard route).

An issue says what to do; it did not say *where*, so a session created from
one had its directory picked by hand every time and a wrong pick is reported
by nothing downstream. These tests pin the three halves of the answer: the
mint records the workspace it was created in, the dashboard can change it,
and a name nobody registered is refused rather than stored.

``br`` never runs here — the board is :class:`tests.test_beads_daemon.FakeBr`,
answering the argv the daemon composes.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_launcher import beads_meta, workspaces
from claude_launcher.cli_beads import BeadsError
from claude_launcher.daemon import beads as beads_mod

from test_beads_daemon import FakeBr, _board, _sdef, _Sess, repo  # noqa: F401


@pytest.fixture
def registered(tmp_path, monkeypatch):
    """Two registered workspaces, and nothing else on the machine."""
    one = tmp_path / "trees" / "alpha"
    two = tmp_path / "trees" / "beta"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    rows = {
        "alpha": workspaces.Workspace(name="alpha", path=str(one)),
        "beta": workspaces.Workspace(name="beta", path=str(two)),
    }
    monkeypatch.setattr(workspaces, "list_all", lambda doc=None: list(rows.values()))
    monkeypatch.setattr(workspaces, "get", lambda name, doc=None: rows.get(name))

    def owning(path, doc=None):
        for row in rows.values():
            if str(path).replace("\\", "/").startswith(row.path.replace("\\", "/")):
                return row
        return None

    monkeypatch.setattr(workspaces, "owning", owning)
    return rows


# --------------------------------------------------------------------------- #
# the mint records where the session was created
# --------------------------------------------------------------------------- #
def test_compose_description_records_the_workspace():
    text = beads_mod.compose_description(
        "무언가를 한다", name="s1", parent=None, workspace="alpha"
    )
    meta, body = beads_meta.parse(text)
    assert meta == {"workspace": "alpha"}
    assert body.startswith("## 목표")


def test_compose_description_without_a_workspace_writes_no_block():
    text = beads_mod.compose_description("무언가를 한다", name="s1", parent=None)
    assert text.startswith("## 목표")
    assert beads_meta.parse(text) == ({}, text)


def test_workspace_for_reads_the_registry(registered, tmp_path):
    inside = tmp_path / "trees" / "alpha" / ".claude" / "worktrees" / "s9"
    assert beads_mod.workspace_for(str(inside)) == "alpha"
    assert beads_mod.workspace_for(str(tmp_path / "elsewhere")) == ""
    assert beads_mod.workspace_for("") == ""
    assert beads_mod.workspace_for(None) == ""


def test_workspace_for_survives_an_unreadable_registry(monkeypatch):
    def boom(path, doc=None):
        raise RuntimeError("config is a directory")

    monkeypatch.setattr(workspaces, "owning", boom)
    # A mint must not fail because the workspace registry cannot be read.
    assert beads_mod.workspace_for("F:/works/x") == ""


def test_created_issue_carries_the_workspace(registered, tmp_path, repo):
    br = FakeBr()
    board = _board(br, repo)
    cwd = tmp_path / "trees" / "beta"
    session = _Sess(_sdef("s1", cwd, task="무언가를 한다"))
    made = asyncio.run(board.create_for(session, title="무언가를 한다"))
    row = br.issues[made["issue"]]
    assert beads_meta.workspace_of(row) == "beta"


# --------------------------------------------------------------------------- #
# the dashboard's write
# --------------------------------------------------------------------------- #
def _seed(br: FakeBr, description: str = "## 목표\n무언가를 한다\n") -> str:
    br.issues["claunch-1"] = {
        "id": "claunch-1",
        "title": "무언가를 한다",
        "status": "open",
        "priority": 2,
        "assignee": "",
        "description": description,
    }
    return "claunch-1"


def test_set_workspace_writes_front_matter(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    issue_id = _seed(br)
    out = asyncio.run(board.set_workspace(repo, issue_id, "alpha"))
    assert out["changed"] is True
    assert out["workspace"] == "alpha"
    assert out["was"] == ""
    assert beads_meta.workspace_of(br.issues[issue_id]) == "alpha"
    # The prose is not disturbed by the write.
    _, body = beads_meta.parse(br.issues[issue_id]["description"])
    assert body == "## 목표\n무언가를 한다\n"


def test_set_workspace_replaces_a_recorded_one(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    issue_id = _seed(br, "---\nworkspace: alpha\n---\n## 목표\n무언가\n")
    out = asyncio.run(board.set_workspace(repo, issue_id, "beta"))
    assert (out["was"], out["workspace"]) == ("alpha", "beta")
    assert beads_meta.workspace_of(br.issues[issue_id]) == "beta"


def test_set_workspace_clears_with_an_empty_name(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    issue_id = _seed(br, "---\nworkspace: alpha\n---\n## 목표\n무언가\n")
    out = asyncio.run(board.set_workspace(repo, issue_id, ""))
    assert out["workspace"] == ""
    assert beads_meta.workspace_of(br.issues[issue_id]) == ""


def test_set_workspace_to_the_same_name_writes_nothing(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    issue_id = _seed(br, "---\nworkspace: alpha\n---\n## 목표\n무언가\n")
    before = len(br.calls)
    out = asyncio.run(board.set_workspace(repo, issue_id, "alpha"))
    assert out["changed"] is False
    # One read (show) and no update: an unchanged value is not a write.
    assert not [c for c in br.calls[before:] if "update" in c]


def test_unregistered_workspace_is_refused(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    issue_id = _seed(br)
    with pytest.raises(BeadsError) as exc:
        asyncio.run(board.set_workspace(repo, issue_id, "nowhere"))
    assert "no workspace" in str(exc.value)
    # Nothing was stored: the issue still records none.
    assert beads_meta.workspace_of(br.issues[issue_id]) == ""


def test_set_workspace_on_a_missing_issue_raises(registered, repo):
    br = FakeBr()
    board = _board(br, repo)
    with pytest.raises(BeadsError):
        asyncio.run(board.set_workspace(repo, "claunch-nope", "alpha"))
