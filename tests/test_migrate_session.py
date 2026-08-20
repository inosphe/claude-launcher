"""Migrating a session: carry the transcript, move the cwd, relaunch.

Claude keeps transcripts per working directory, so "move this session to a
worktree" is three moves that must hold together: the conversation's jsonl is
re-filed under the new directory's slug (:mod:`claude_launcher.transcripts`),
the definition's cwd changes, and the harness is relaunched with ``--resume``
of the same pinned id. These tests pin each piece and the seams: the slug
rule, the move, the refusals that must fire *before* anything is touched, the
rollback when the relaunch fails, and the children that follow (or don't).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from claude_launcher import profile as profile_mod
from claude_launcher import store, transcripts
from claude_launcher.daemon import harness as harness_mod
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.mesh import MeshManager

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

BEARER = {"Authorization": "Bearer sekrit"}


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _serve(mgr, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mm = MeshManager(mgr, root=tmp_path / "mesh")
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --------------------------------------------------------------------------- #
# the slug and the move
# --------------------------------------------------------------------------- #
def test_project_slug_replaces_everything_but_alnum(tmp_path):
    slug = transcripts.project_slug(str(tmp_path / "a b" / "c.d"))
    assert slug == "".join(
        ch if ch.isalnum() else "-" for ch in os.path.abspath(str(tmp_path / "a b" / "c.d"))
    )
    # the property resume depends on: one directory, one slug, always
    assert transcripts.project_slug(str(tmp_path)) == transcripts.project_slug(
        str(tmp_path)
    )


def test_relocate_moves_the_one_file(tmp_path):
    config = tmp_path / "config"
    a, b = tmp_path / "dirA", tmp_path / "dirB"
    src_dir = transcripts.project_dir(config, str(a))
    src_dir.mkdir(parents=True)
    (src_dir / "cid-1.jsonl").write_text("{}", encoding="utf-8")
    (src_dir / "cid-other.jsonl").write_text("{}", encoding="utf-8")

    dest = transcripts.relocate(config, "cid-1", str(a), str(b))
    assert dest == transcripts.project_dir(config, str(b)) / "cid-1.jsonl"
    assert dest.is_file()
    assert not (src_dir / "cid-1.jsonl").exists()
    # a sibling conversation of the same directory is not dragged along
    assert (src_dir / "cid-other.jsonl").is_file()


def test_relocate_finds_a_transcript_filed_under_an_unexpected_slug(tmp_path):
    """When claude's spelling of the old cwd disagrees with ours, the id is
    still unique across the config dir — the search finds it and the move
    still lands under the slug of the *destination* we compute."""
    config = tmp_path / "config"
    odd = config / "projects" / "some-unexpected-slug"
    odd.mkdir(parents=True)
    (odd / "cid-2.jsonl").write_text("{}", encoding="utf-8")

    dest = transcripts.relocate(
        config, "cid-2", str(tmp_path / "dirA"), str(tmp_path / "dirB")
    )
    assert dest == transcripts.project_dir(config, str(tmp_path / "dirB")) / "cid-2.jsonl"
    assert dest.is_file()
    assert not (odd / "cid-2.jsonl").exists()


def test_relocate_without_a_transcript_moves_nothing_and_says_so(tmp_path):
    assert (
        transcripts.relocate(
            tmp_path / "config", "cid-none", str(tmp_path / "a"), str(tmp_path / "b")
        )
        is None
    )


# --------------------------------------------------------------------------- #
# the manager's move
# --------------------------------------------------------------------------- #
def test_migrate_moves_a_live_session_and_relaunches_it(home, tmp_path):
    _register_py_harness()
    a, b = tmp_path / "dirA", tmp_path / "dirB"
    a.mkdir(), b.mkdir()

    async def run():
        mgr = _manager()
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(a)))
        session, carried = await mgr.migrate("s1", str(b))
        assert session.sdef.cwd == str(b)
        assert not session.exited  # relaunched, not just re-filed
        assert carried is False  # nothing to carry for a non-claude harness
        assert mgr.get("s1") is session
        await mgr.shutdown_all()

    asyncio.run(run())


def test_migrate_refusals_touch_nothing(home, tmp_path):
    _register_py_harness()
    a = tmp_path / "dirA"
    a.mkdir()

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(name="s1", harness="py", cwd=str(a)))
        with pytest.raises(ManagerError, match="does not exist"):
            await mgr.migrate("s1", str(tmp_path / "nowhere"))
        with pytest.raises(ManagerError, match="already in"):
            await mgr.migrate("s1", str(a))
        # both refusals fired before the stop: the session never went down
        assert mgr.get("s1") is session and not session.exited
        await mgr.shutdown_all()

    asyncio.run(run())


def test_migrate_refuses_a_claude_session_with_no_pinned_conversation(home, tmp_path):
    """No pin means the transcript cannot be identified — moving the session
    would silently lose the conversation, which is the one outcome this
    feature exists to prevent."""
    profile_mod.create("p1")
    a, b = tmp_path / "dirA", tmp_path / "dirB"
    a.mkdir(), b.mkdir()

    async def run():
        mgr = _manager()
        # A definition predating the pin (or started with --continue): the
        # restore path these come from is _retire, mimicked here.
        mgr._retire(
            SessionDef(
                name="old", harness="claude", profile="p1", cwd=str(a),
                args=("--continue",),
            ),
            {},
        )
        with pytest.raises(ManagerError, match="no pinned conversation"):
            await mgr.migrate("old", str(b))

    asyncio.run(run())


def _fake_claude_build_command(sdef, *, restoring=False, opening=""):
    """Stand in for the claude launch: a real child process, no real claude."""
    return [sys.executable, "-u", "-c", CHILD], {"CLAUNCH_SESSION": sdef.name}, sdef.cwd


def test_migrate_carries_the_claude_transcript(home, tmp_path, monkeypatch):
    profile = profile_mod.create("p1")
    a, b = tmp_path / "dirA", tmp_path / "dirB"
    a.mkdir(), b.mkdir()
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(a))
        )
        cid = session.sdef.conversation_id
        assert cid  # pinned at creation
        src = transcripts.project_dir(profile.config_dir, str(a)) / f"{cid}.jsonl"
        src.parent.mkdir(parents=True)
        src.write_text('{"type":"user"}\n', encoding="utf-8")

        migrated, carried = await mgr.migrate("s1", str(b))
        assert carried is True
        assert migrated.sdef.cwd == str(b)
        assert migrated.sdef.conversation_id == cid  # same conversation
        dest = transcripts.project_dir(profile.config_dir, str(b)) / f"{cid}.jsonl"
        assert dest.is_file() and not src.exists()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_migrate_rolls_back_when_the_relaunch_fails(home, tmp_path, monkeypatch):
    """A failed relaunch must leave the world as it was: transcript back under
    the old slug, record still registered under its old definition."""
    profile = profile_mod.create("p1")
    a, b = tmp_path / "dirA", tmp_path / "dirB"
    a.mkdir(), b.mkdir()
    monkeypatch.setattr(harness_mod, "build_command", _fake_claude_build_command)

    async def run():
        mgr = _manager()
        session = mgr.create(
            SessionDef(name="s1", harness="claude", profile="p1", cwd=str(a))
        )
        cid = session.sdef.conversation_id
        src = transcripts.project_dir(profile.config_dir, str(a)) / f"{cid}.jsonl"
        src.parent.mkdir(parents=True)
        src.write_text("{}", encoding="utf-8")

        def refuse(sdef, *, restoring=False, opening=""):
            raise harness_mod.HarnessError("no relaunch today")

        monkeypatch.setattr(harness_mod, "build_command", refuse)
        with pytest.raises(harness_mod.HarnessError):
            await mgr.migrate("s1", str(b))
        assert src.is_file()  # the transcript came back
        kept = mgr.get("s1")
        assert kept.sdef.cwd == str(a)
        assert kept.exited  # it was stopped; the record says so honestly

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the API: destination choices and the children that follow
# --------------------------------------------------------------------------- #
def test_api_migrate_moves_same_directory_children_only(home, tmp_path):
    _register_py_harness()
    a, b, elsewhere = tmp_path / "dirA", tmp_path / "dirB", tmp_path / "own"
    a.mkdir(), b.mkdir(), elsewhere.mkdir()

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(a)))
            mgr.create(SessionDef(name="c1", harness="py", cwd=str(a), parent="s1"))
            mgr.create(
                SessionDef(name="c2", harness="py", cwd=str(elsewhere), parent="s1")
            )

            resp = await client.post(
                "/api/sessions/s1/migrate",
                json={"cwd": str(b), "children": True},
                headers=BEARER,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["cwd"] == str(b)
            assert body["worktree"] is None
            assert [(c["name"], c["ok"]) for c in body["children"]] == [("c1", True)]
            assert mgr.get("c1").sdef.cwd == str(b)
            # a child already somewhere of its own is exactly where someone
            # put it, and stays
            assert mgr.get("c2").sdef.cwd == str(elsewhere)
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


def test_api_migrate_requires_exactly_one_destination(home, tmp_path):
    _register_py_harness()
    a = tmp_path / "dirA"
    a.mkdir()

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(a)))
            for body in ({}, {"cwd": str(a), "worktree": "x"}):
                resp = await client.post(
                    "/api/sessions/s1/migrate", json=body, headers=BEARER
                )
                assert resp.status == 400
                assert "exactly one" in (await resp.json())["error"]
            # a worktree of a directory in no repository is a refusal, not a 500
            resp = await client.post(
                "/api/sessions/s1/migrate", json={"worktree": "x"}, headers=BEARER
            )
            assert resp.status == 400
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.worktree
def test_api_migrate_into_a_worktree_of_the_sessions_repo(home, tmp_path, monkeypatch):
    """``worktree: NAME`` cuts (or reuses) a checkout of the session's own
    repository and moves the session into it."""
    import subprocess

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _register_py_harness()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")

    async def run():
        mgr = _manager()
        client = await _serve(mgr, tmp_path)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
            resp = await client.post(
                "/api/sessions/s1/migrate",
                json={"worktree": "review"},
                headers=BEARER,
            )
            assert resp.status == 200
            body = await resp.json()
            wt = body["worktree"]
            assert wt["name"] == "review" and wt["created"] is True
            expected = repo / ".claude" / "worktrees" / "review"
            assert Path(body["cwd"]) == expected
            assert mgr.get("s1").sdef.cwd == str(expected)
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
