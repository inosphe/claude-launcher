"""A worktree the daemon cuts without a name is named after its session
(claunch-2yiij.3).

``worktree.default_name`` names a checkout after the pane or session that
asked -- ``HERDR_PANE_ID``, else ``CLAUNCH_SESSION``, else ``wt`` -- read from
the calling process. That is the right answer in a person's shell. In the
daemon it read the daemon's own environment: while the daemon carried s609's
variables, every session the web form created with an empty worktree name got
a ``w3-p1-<stamp>`` checkout (97 of them in this repository on 2026-10-15),
and once claunch-2yiij removed those variables they would all be
``wt-<stamp>``. Neither says whose checkout it is.

The daemon knows the answer: the session the checkout is for. The web create
route settles the session's name first -- the way a spawn already does for
its ``<parent>-<child>-<stamp>`` -- and migrate names it after the session
being moved.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import worktree
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

_CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)

STAMP = r"\d{8}-\d{6}"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _build(root: Path) -> None:
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)


@pytest.fixture
def repo(tmp_path, repo_template, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return repo_template("worktree-name", _build, tmp_path / "repo")


@pytest.fixture
def daemon_env(monkeypatch):
    """What a daemon spawned from s609's Herdr pane used to carry: the
    name must not come from here."""
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv(worktree.herdr.PANE_ID_ENV, "w3:p1")
    monkeypatch.setenv(worktree.SESSION_ENV, "s609")


def _launching(scenario):
    from claude_launcher import lineage, profile, store

    store.update(lambda doc: doc.update(
        {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", _CHILD]}}}))
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await scenario(client, mgr)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_a_web_created_session_gets_a_worktree_named_after_it(home, repo, daemon_env):
    async def scenario(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "profile": "py", "cwd": str(repo), "beads": False, "worktree": "",
        })
        assert resp.status == 201, await resp.text()
        name = (await resp.json())["name"]
        cwd = Path(mgr.get(name).sdef.cwd)
        assert cwd.parent == worktree.worktrees_dir(repo)
        assert re.fullmatch(rf"{name}-{STAMP}", cwd.name), cwd.name

    _launching(scenario)


def test_a_named_web_session_gets_its_own_name_on_the_worktree(home, repo, daemon_env):
    async def scenario(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "reviewer", "profile": "py", "cwd": str(repo),
            "beads": False, "worktree": "",
        })
        assert resp.status == 201, await resp.text()
        cwd = Path(mgr.get("reviewer").sdef.cwd)
        assert re.fullmatch(rf"reviewer-{STAMP}", cwd.name), cwd.name

    _launching(scenario)


def test_a_web_session_keeps_the_worktree_name_it_was_given(home, repo, daemon_env):
    async def scenario(client, mgr):
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "profile": "py", "cwd": str(repo), "beads": False, "worktree": "mine",
        })
        assert resp.status == 201, await resp.text()
        name = (await resp.json())["name"]
        assert Path(mgr.get(name).sdef.cwd).name == "mine"

    _launching(scenario)


def test_a_taken_name_is_refused_before_a_checkout_is_cut(home, repo, daemon_env):
    """Settling the name first means a duplicate is refused with nothing
    left on disk, the rule a spawn already keeps."""
    async def scenario(client, mgr):
        mgr.create(SessionDef(name="dup", harness="py", cwd=str(repo)))
        resp = await client.post("/api/sessions", headers=BEARER, json={
            "name": "dup", "profile": "py", "cwd": str(repo),
            "beads": False, "worktree": "",
        })
        assert resp.status == 409, await resp.text()
        trees = worktree.worktrees_dir(repo)
        assert not trees.exists() or not any(trees.iterdir())

    _launching(scenario)


def test_migrate_names_the_new_worktree_after_the_session_moved(home, repo, daemon_env):
    async def scenario(client, mgr):
        mgr.create(SessionDef(name="s1", harness="py", cwd=str(repo)))
        resp = await client.post(
            "/api/sessions/s1/migrate", json={"worktree": ""}, headers=BEARER,
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert re.fullmatch(rf"s1-{STAMP}", body["worktree"]["name"])

    _launching(scenario)


def test_a_person_still_gets_their_pane_name(daemon_env):
    """Outside the daemon nothing changes: the CLI's default is the pane."""
    assert re.fullmatch(rf"w3-p1-{STAMP}", worktree.default_name())


def test_default_name_takes_the_session_it_is_for():
    assert re.fullmatch(rf"s12-{STAMP}", worktree.default_name(token="s12"))
