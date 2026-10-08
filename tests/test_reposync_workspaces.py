"""The Workspaces page's Sync panel routes (claunch-tqm2e).

One workspace's status (remotes, peers sharing an address, the plan), the
sync the person confirmed (refused when the plan moved underneath it), and
publishing / receiving a work branch -- all against real git clones of one
bare remote. Peers come from a stand-in relay: an unreadable peer narrows the
answer and never stops the plan.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from claude_launcher import workspaces
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _clone(remote: Path, where: Path, remote_name: str) -> Path:
    subprocess.run(["git", "clone", "-q", "-o", remote_name, str(remote), str(where)], check=True)
    _git(where, "config", "user.email", "t@example.com")
    _git(where, "config", "user.name", "t")
    _git(where, "config", "commit.gpgsign", "false")
    return where


@pytest.fixture
def world(home, tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "master")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    _git(seed, "config", "commit.gpgsign", "false")
    _commit(seed, "base.txt")
    remote = tmp_path / "shared.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    a = _clone(remote, tmp_path / "a", "origin")
    b = _clone(remote, tmp_path / "b", "origin")
    workspaces.add(str(a), "proj")
    return {"remote": remote, "a": a, "b": b}


def _with_app(fn, *, peers=None, me="pc-a"):
    """``peers``: machine -> repos list, or an Exception the stand-in relay
    raises for that machine."""
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        relay_state=lambda: {"name": me})
        if peers is not None:
            mm = app["mesh"]

            async def lister():
                return [me, *peers]

            async def transport(machine, path, body):
                assert path == "/peer/repos"
                got = peers[machine]
                if isinstance(got, Exception):
                    raise got
                return {"repos": got}

            mm.peer_lister, mm.peer_transport = lister, transport
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await fn(client)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_status_plans_without_any_peer(world):
    a, b = world["a"], world["b"]
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status", headers=AUTH)
        assert resp.status == 200
        doc = await resp.json()
        assert doc["workspace"] == "proj" and doc["machine"] == "pc-a"
        assert doc["peers"] == []
        assert doc["problems"] == ["relay uplink is not running -- peers not consulted"]
        assert doc["plan"]["action"] == "ff" and doc["plan"]["behind"] == 1
        assert "master" in doc["branches"]

    _with_app(body)


def test_status_lists_peers_sharing_the_address_and_survives_an_unreadable_one(world):
    _git(world["a"], "remote", "add", "gh", "https://git.example/Org/Proj.git")
    peers = {
        "pc-b": [{"workspace": "proj", "remotes": [{"name": "up", "key": "git.example/org/proj"}],
                  "branches": {"master": "c" * 40}}],
        "pc-c": [{"workspace": "other", "remotes": [{"name": "o", "key": "git.example/org/x"}]}],
        "pc-old": RuntimeError("404"),
    }

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status?remote=origin", headers=AUTH)
        doc = await resp.json()
        assert [(r["machine"], r["local_remote"], r["peer_remote"]) for r in doc["peers"]] == [
            ("pc-b", "gh", "up")]
        assert len(doc["problems"]) == 1 and doc["problems"][0].startswith("pc-old:")
        # the plan still stands on the remote that was named
        assert doc["plan"]["remote"] == "origin"

    _with_app(body, peers=peers)


def test_status_of_an_unknown_workspace_is_404(world):
    async def body(client):
        resp = await client.get("/api/reposync/workspaces/nope/status?peers=0", headers=AUTH)
        assert resp.status == 404

    _with_app(body)


def test_sync_runs_the_confirmed_plan(world):
    a, b = world["a"], world["b"]
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    mine = _commit(a, "mine.txt")

    async def body(client):
        doc = await (await client.get("/api/reposync/workspaces/proj/status?peers=0",
                                      headers=AUTH)).json()
        plan = doc["plan"]
        assert plan["action"] == "rebase" and plan["local"] == mine
        resp = await client.post("/api/reposync/workspaces/proj/sync", json={
            k: plan[k] for k in ("remote", "branch", "local", "upstream")}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        done = await resp.json()
        assert done["pushed"] is True
        assert _git(world["remote"], "rev-parse", "master") == done["new"]
        assert _git(a, "config", "--get", "claunch.reposync.remote") == "origin"

    _with_app(body)


def test_sync_refuses_a_plan_that_moved_since_it_was_shown(world):
    a, b = world["a"], world["b"]
    _commit(a, "mine.txt")

    async def body(client):
        plan = (await (await client.get("/api/reposync/workspaces/proj/status?peers=0",
                                         headers=AUTH)).json())["plan"]
        assert plan["action"] == "push"
        # someone else pushes before the person presses the button
        _commit(b, "theirs.txt")
        _git(b, "push", "-q", "origin", "master")
        resp = await client.post("/api/reposync/workspaces/proj/sync", json={
            k: plan[k] for k in ("remote", "branch", "local", "upstream")}, headers=AUTH)
        assert resp.status == 409
        doc = await resp.json()
        assert doc["stale"] is True and doc["plan"]["action"] == "rebase"
        assert _git(world["remote"], "rev-parse", "master") == _git(b, "rev-parse", "master")

    _with_app(body)


def test_branch_push_then_pull_on_the_other_machine(world):
    a, b = world["a"], world["b"]
    _git(a, "switch", "-q", "-c", "s1-work")
    tip = _commit(a, "work.txt")
    workspaces.add(str(b), "proj-b")

    async def push(client):
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work"}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert (doc["ref"], doc["sha"]) == ("pc-a/s1-work", tip)

    async def pull(client):
        resp = await client.post("/api/reposync/workspaces/proj-b/branch-pull",
                                 json={"machine": "pc-a", "branch": "s1-work"}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert (doc["branch"], doc["sha"]) == ("pc-a/s1-work", tip)
        resp = await client.post("/api/reposync/workspaces/proj-b/branch-pull",
                                 json={"machine": "pc-a"}, headers=AUTH)
        assert resp.status == 400

    _with_app(push, me="pc-a")
    _with_app(pull, me="pc-b")


def test_routes_need_the_token(world):
    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status")
        assert resp.status == 401
        resp = await client.post("/api/reposync/workspaces/proj/sync", json={})
        assert resp.status == 401

    _with_app(body)
