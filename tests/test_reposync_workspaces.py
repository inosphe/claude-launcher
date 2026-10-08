"""The Workspaces page's Sync panel routes (claunch-tqm2e).

One workspace's status (remotes, peers sharing an address, the plan), the
sync of the plan that was shown (refused when it moved underneath), and
publishing / receiving a work branch -- all against real git clones of one
bare remote. Every push waits behind the reposync approval gate: a POST
without an approved card, which any holder of the daemon token could send,
files the card and pushes nothing. Peers come from a stand-in relay: an
unreadable peer narrows the answer and never stops the plan.
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


def _shown(plan, **extra):
    return {**{k: plan[k] for k in ("remote", "branch", "local", "upstream")}, **extra}


async def _status_plan(client):
    doc = await (await client.get("/api/reposync/workspaces/proj/status?peers=0",
                                  headers=AUTH)).json()
    return doc["plan"]


def test_a_push_without_an_approval_files_a_card_and_pushes_nothing(world):
    """s469's decline of d641c8fe: a direct POST with the daemon token -- what
    any session could send -- must not push. It files the approval card."""
    a, b = world["a"], world["b"]
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    _commit(a, "mine.txt")
    before = _git(world["remote"], "rev-parse", "master")

    async def body(client):
        plan = await _status_plan(client)
        assert plan["action"] == "rebase"
        resp = await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan),
                                 headers=AUTH)
        assert resp.status == 202, await resp.text()
        doc = await resp.json()
        assert doc["status"] == "waiting_approval" and doc["request"]
        assert _git(world["remote"], "rev-parse", "master") == before
        assert _git(a, "rev-parse", "master") == plan["local"], "nothing moved locally either"
        # the card is on the gate, bound to the tips that were shown
        listed = await (await client.get("/api/reposync/requests", headers=AUTH)).json()
        [card] = listed["requests"]
        assert card["id"] == doc["request"] and card["status"] == "pending"
        assert (card["plan"]["local"], card["plan"]["upstream"]) == (plan["local"], plan["upstream"])
        # asking again with the pending id still pushes nothing
        resp = await client.post("/api/reposync/workspaces/proj/sync",
                                 json=_shown(plan, request=doc["request"]), headers=AUTH)
        assert resp.status == 202
        assert _git(world["remote"], "rev-parse", "master") == before

    _with_app(body)


def test_sync_runs_once_a_person_approved_the_card(world):
    a, b = world["a"], world["b"]
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    mine = _commit(a, "mine.txt")

    async def body(client):
        plan = await _status_plan(client)
        assert plan["action"] == "rebase" and plan["local"] == mine
        rid = (await (await client.post("/api/reposync/workspaces/proj/sync",
                                        json=_shown(plan), headers=AUTH)).json())["request"]
        resp = await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        assert resp.status == 200
        resp = await client.post("/api/reposync/workspaces/proj/sync",
                                 json=_shown(plan, request=rid), headers=AUTH)
        assert resp.status == 200, await resp.text()
        done = await resp.json()
        assert done["pushed"] is True
        assert _git(world["remote"], "rev-parse", "master") == done["new"]
        assert _git(a, "config", "--get", "claunch.reposync.remote") == "origin"

    _with_app(body)


@pytest.mark.parametrize("answer", ["reject", "expire"])
def test_a_refused_or_expired_card_pushes_nothing(world, answer):
    a = world["a"]
    _commit(a, "mine.txt")
    before = _git(world["remote"], "rev-parse", "master")

    async def body(client):
        plan = await _status_plan(client)
        assert plan["action"] == "push"
        rid = (await (await client.post("/api/reposync/workspaces/proj/sync",
                                        json=_shown(plan), headers=AUTH)).json())["request"]
        if answer == "reject":
            await client.post(f"/api/reposync/requests/{rid}/reject", headers=AUTH)
        else:
            client.app["reposync_gate"]._requests[rid]["deadline"] = "2000-01-01T00:00:00+00:00"
        resp = await client.post("/api/reposync/workspaces/proj/sync",
                                 json=_shown(plan, request=rid), headers=AUTH)
        assert resp.status == 409
        doc = await resp.json()
        assert doc["status"] == ("rejected" if answer == "reject" else "expired")
        assert _git(world["remote"], "rev-parse", "master") == before

    _with_app(body)


def test_an_approval_does_not_cover_a_plan_that_moved_after_it(world):
    a, b = world["a"], world["b"]
    _commit(a, "mine.txt")

    async def body(client):
        plan = await _status_plan(client)
        rid = (await (await client.post("/api/reposync/workspaces/proj/sync",
                                        json=_shown(plan), headers=AUTH)).json())["request"]
        await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        # the remote moves after the approval; the caller re-reads the plan
        # and sends its new tips with the old id
        _commit(b, "theirs.txt")
        _git(b, "push", "-q", "origin", "master")
        moved = await _status_plan(client)
        resp = await client.post("/api/reposync/workspaces/proj/sync",
                                 json=_shown(moved, request=rid), headers=AUTH)
        assert resp.status == 409
        assert (await resp.json())["status"] == "stale_approval"
        assert _git(world["remote"], "rev-parse", "master") == _git(b, "rev-parse", "master")

    _with_app(body)


def test_a_fast_forward_pushes_nothing_and_needs_no_card(world):
    a, b = world["a"], world["b"]
    tip = _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")

    async def body(client):
        plan = await _status_plan(client)
        assert plan["action"] == "ff"
        resp = await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan),
                                 headers=AUTH)
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["pushed"] is False
        assert _git(a, "rev-parse", "master") == tip
        listed = await (await client.get("/api/reposync/requests", headers=AUTH)).json()
        assert listed["requests"] == []

    _with_app(body)


def test_auto_mode_lifts_the_card(world, monkeypatch):
    from claude_launcher import reposync_flow
    monkeypatch.setattr(reposync_flow, "approval_mode", lambda: reposync_flow.AUTO)
    a = world["a"]
    mine = _commit(a, "mine.txt")

    async def body(client):
        plan = await _status_plan(client)
        resp = await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan),
                                 headers=AUTH)
        assert resp.status == 200, await resp.text()
        assert _git(world["remote"], "rev-parse", "master") == mine

    _with_app(body)


def test_sync_refuses_a_plan_that_moved_since_it_was_shown(world):
    a, b = world["a"], world["b"]
    _commit(a, "mine.txt")

    async def body(client):
        plan = await _status_plan(client)
        assert plan["action"] == "push"
        # someone else pushes before the button is pressed
        _commit(b, "theirs.txt")
        _git(b, "push", "-q", "origin", "master")
        resp = await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan),
                                 headers=AUTH)
        assert resp.status == 409
        doc = await resp.json()
        assert doc["stale"] is True and doc["plan"]["action"] == "rebase"
        assert _git(world["remote"], "rev-parse", "master") == _git(b, "rev-parse", "master")
        listed = await (await client.get("/api/reposync/requests", headers=AUTH)).json()
        assert listed["requests"] == [], "a stale plan files no card"

    _with_app(body)


def test_branch_push_waits_for_the_card_then_pull_on_the_other_machine(world):
    a, b = world["a"], world["b"]
    _git(a, "switch", "-q", "-c", "s1-work")
    tip = _commit(a, "work.txt")
    workspaces.add(str(b), "proj-b")

    def published():
        return _git(world["remote"], "for-each-ref", "--format=%(objectname)",
                    "refs/heads/pc-a/s1-work")

    async def push(client):
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work"}, headers=AUTH)
        assert resp.status == 202, await resp.text()
        rid = (await resp.json())["request"]
        assert published() == "", "no approval, no branch on the remote"
        await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        # an approval of a plain push does not cover a force-with-lease
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work", "force_with_lease": True,
                                       "request": rid}, headers=AUTH)
        assert resp.status == 409 and (await resp.json())["status"] == "stale_approval"
        assert published() == ""
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work", "request": rid}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        assert (doc["ref"], doc["sha"]) == ("pc-a/s1-work", tip)
        assert published() == tip

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


# --- Row badges (claunch-gax44) -------------------------------------------


def test_summary_reads_branch_ahead_behind_and_changed_files(world, tmp_path):
    a, b = world["a"], world["b"]
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    _git(a, "fetch", "-q", "origin")          # the summary itself never fetches
    _commit(a, "mine1.txt")
    _commit(a, "mine2.txt")
    (a / "base.txt").write_text("edited", encoding="utf-8")
    (a / "untracked.txt").write_text("new", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    workspaces.add(str(plain), "plain")
    gone = tmp_path / "gone"
    gone.mkdir()
    workspaces.add(str(gone), "gone")
    gone.rmdir()                              # a removable drive that is unplugged

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/summary", headers=AUTH)
        assert resp.status == 200
        docs = (await resp.json())["workspaces"]
        proj = docs["proj"]
        assert proj["branch"] == "master" and proj["detached"] is False
        assert proj["upstream"] == "origin/master"
        assert (proj["ahead"], proj["behind"]) == (2, 1)
        assert proj["changed"] == 1               # untracked.txt is not counted
        assert "not a git repository" in docs["plain"]["error"]
        assert "gone" not in docs                 # a missing workspace is left out

    _with_app(body)


def test_summary_without_upstream_and_detached(world):
    a = world["a"]
    _git(a, "switch", "-q", "-c", "local-only")
    from claude_launcher import reposync_flow

    doc = reposync_flow.git_summary(str(a))
    assert doc["branch"] == "local-only" and doc["upstream"] is None
    assert doc["ahead"] is None and doc["behind"] is None and doc["changed"] == 0

    _git(a, "switch", "-q", "--detach", "HEAD")
    doc = reposync_flow.git_summary(str(a))
    assert doc["detached"] is True and doc["branch"] is None


def test_summary_needs_the_token(world):
    async def body(client):
        resp = await client.get("/api/reposync/workspaces/summary")
        assert resp.status == 401

    _with_app(body)


# --- Shared branches (claunch-tamel) --------------------------------------


def test_status_lists_branches_other_machines_published(world):
    a, b = world["a"], world["b"]
    base = _git(a, "rev-parse", "HEAD")
    _git(b, "switch", "-q", "-c", "s2-y")
    theirs = _commit(b, "theirs.txt")
    for ref in ("pc-b/s2-y", "pc-b/old", "pc-b/div", "Feature/z"):
        _git(b, "push", "-q", "origin", f"{theirs}:refs/heads/{ref}")
    _git(b, "push", "-q", "origin", f"{base}:refs/heads/pc-b/same")
    _git(b, "push", "-q", "origin", f"{theirs}:refs/heads/pc-a/mine")   # this machine's own
    _git(a, "branch", "pc-b/same", base)
    _git(a, "branch", "pc-b/old", base)
    _git(a, "switch", "-q", "-c", "pc-b/div")
    _commit(a, "mine.txt")
    _git(a, "switch", "-q", "master")
    _git(a, "remote", "add", "gh", "https://git.example/Org/Proj.git")
    peers = {"pc-b": [{"workspace": "proj", "remotes": [{"name": "up", "key": "git.example/org/proj"}]}]}

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status?remote=origin", headers=AUTH)
        assert resp.status == 200, await resp.text()
        shared = {r["ref"]: r for r in (await resp.json())["shared"]}
        assert set(shared) == {"pc-b/s2-y", "pc-b/old", "pc-b/div", "pc-b/same", "Feature/z"}
        assert {k: r["state"] for k, r in shared.items()} == {
            "pc-b/s2-y": "new", "pc-b/old": "behind", "pc-b/div": "diverged",
            "pc-b/same": "same", "Feature/z": "new"}
        row = shared["pc-b/s2-y"]
        assert (row["machine"], row["branch"], row["sha"]) == ("pc-b", "s2-y", theirs)
        assert row["subject"] == "add theirs.txt" and row["local"] is None
        assert row["peer"] is True and shared["Feature/z"]["peer"] is False

        # the row's Pull is the existing branch pull, fast-forward only
        resp = await client.post("/api/reposync/workspaces/proj/branch-pull",
                                 json={"machine": "pc-b", "branch": "old", "remote": "origin"},
                                 headers=AUTH)
        assert resp.status == 200, await resp.text()
        resp = await client.get("/api/reposync/workspaces/proj/status?remote=origin&fetch=0",
                                headers=AUTH)
        shared = {r["ref"]: r for r in (await resp.json())["shared"]}
        assert shared["pc-b/old"]["state"] == "same"

    _with_app(body, peers=peers, me="pc-a")


# --- Branches waiting to land (claunch-6p7ri) -----------------------------


def test_status_lists_local_branches_the_remote_master_does_not_hold(world):
    a = world["a"]
    _git(a, "switch", "-q", "-c", "s1-landed")
    _commit(a, "landed.txt")
    _git(a, "push", "-q", "origin", "s1-landed:master")       # landed on the shared master
    _git(a, "switch", "-q", "-c", "s1-open", "master")
    (a / "open.txt").write_text("open", encoding="utf-8")
    _git(a, "add", "open.txt")
    _git(a, "commit", "-q", "-m", "open work\n\nClaunch-Session: s42\nClaunch-Worktree: wt-x")
    _commit(a, "open2.txt")
    _git(a, "commit", "-q", "--allow-empty", "-m", "tip\n\nClaunch-Session: s43")
    _git(a, "switch", "-q", "-c", "plain", "master")
    plain = _commit(a, "plain.txt")
    _git(a, "switch", "-q", "master")

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status", headers=AUTH)
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        rows = {r["branch"]: r for r in doc["unmerged"]}
        # s1-landed is on the remote's master though the local master is behind
        assert set(rows) == {"s1-open", "plain"}
        assert (rows["s1-open"]["ahead"], rows["s1-open"]["session"]) == (3, "s43")
        assert (rows["plain"]["ahead"], rows["plain"]["session"]) == (1, None)
        assert rows["plain"]["sha"] == plain and rows["plain"]["subject"] == "add plain.txt"
        assert rows["plain"]["date"]

    _with_app(body)


# --- Peer comparison table (claunch-gjjdp) --------------------------------


def test_status_measures_each_peer_master_against_ours(world):
    a, b = world["a"], world["b"]
    ours = _git(a, "rev-parse", "master")
    theirs = _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")      # reaches this clone with the status fetch
    _git(a, "remote", "add", "gh", "https://git.example/Org/Proj.git")
    shared = [{"name": "up", "key": "git.example/org/proj"}]

    def peer(sha):
        return [{"workspace": "proj", "remotes": shared,
                 "branches": {"master": {"sha": sha, "tracking": []}}}]

    peers = {
        "pc-same": peer(ours),
        "pc-ahead": peer(theirs),
        "pc-far": peer("f" * 40),                  # never reached the shared remote
        "pc-legacy": [{"workspace": "proj", "remotes": shared, "branches": {"master": ours}}],
        "pc-old": RuntimeError("404"),
    }

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/proj/status?remote=origin", headers=AUTH)
        assert resp.status == 200, await resp.text()
        doc = await resp.json()
        got = {r["machine"]: r["distance"] for r in doc["peers"]}
        assert got["pc-same"] == {"state": "same", "theirs_only": 0, "ours_only": 0}
        assert got["pc-ahead"] == {"state": "ahead", "theirs_only": 1, "ours_only": 0}
        assert got["pc-far"]["state"] == "unknown"
        assert got["pc-legacy"]["state"] == "same"
        assert doc["unreadable"] == ["pc-old"]

    _with_app(body, peers=peers)


def test_peer_distance_behind_and_diverged(world):
    from claude_launcher import reposync_flow

    a = world["a"]
    base = _git(a, "rev-parse", "HEAD")
    mine = _commit(a, "mine.txt")
    _git(a, "switch", "-q", "-c", "side", base)
    side = _commit(a, "side.txt")
    assert reposync_flow.peer_distance(str(a), mine, base) == {
        "state": "behind", "theirs_only": 0, "ours_only": 1}
    assert reposync_flow.peer_distance(str(a), mine, side) == {
        "state": "diverged", "theirs_only": 1, "ours_only": 1}
    assert reposync_flow.peer_distance(str(a), mine, None)["state"] == "unknown"
