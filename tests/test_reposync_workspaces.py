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


# --- The same facts from the CLI and the MCP status (claunch-1cvq4) -------


def _status_world(world, monkeypatch):
    """pc-b pushed a master commit and published pc-b/s2-y; this clone has an
    unlanded, stamped branch. The relay is stood in for at the flow's seams."""
    from claude_launcher import reposync_flow

    a, b = world["a"], world["b"]
    theirs = _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    _git(b, "switch", "-q", "-c", "s2-y")
    pub = _commit(b, "pub.txt")
    _git(b, "push", "-q", "origin", "s2-y:refs/heads/pc-b/s2-y")
    _git(a, "switch", "-q", "-c", "s1-open")
    _git(a, "commit", "-q", "--allow-empty", "-m", "open work\n\nClaunch-Session: s42")
    _git(a, "switch", "-q", "master")
    _git(a, "remote", "add", "gh", "https://git.example/Org/Proj.git")
    shared = [{"name": "up", "key": "git.example/org/proj"}]
    peers = {"pc-b": [{"workspace": "proj", "remotes": shared,
                       "branches": {"master": {"sha": theirs, "tracking": []}}}]}
    monkeypatch.setattr(reposync_flow, "client", lambda: object())
    monkeypatch.setattr(reposync_flow, "relay_identity", lambda c: ("pc-a", ["pc-b"]))
    monkeypatch.setattr(reposync_flow, "peer_repos", lambda c, machine: peers[machine])
    return theirs, pub


def _status_args(**kw):
    import argparse
    base = dict(workspace="proj", remote="origin", branch=None, peer=None,
                no_fetch=False, json=False)
    return argparse.Namespace(**{**base, **kw})


def test_cli_status_reports_peer_distance_shared_and_unmerged(world, monkeypatch, capsys):
    import json
    from claude_launcher import cli_reposync

    theirs, pub = _status_world(world, monkeypatch)
    assert cli_reposync._cmd_status(_status_args(json=True)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["fetched"] is True and doc["plan"]["action"] == "ff"
    [peer] = doc["peers"]
    assert peer["distance"] == {"state": "ahead", "theirs_only": 1, "ours_only": 0}
    [row] = doc["shared"]
    assert (row["ref"], row["sha"], row["state"], row["peer"]) == ("pc-b/s2-y", pub, "new", True)
    [open_] = doc["unmerged"]
    assert (open_["branch"], open_["ahead"], open_["session"]) == ("s1-open", 1, "s42")

    assert cli_reposync._cmd_status(_status_args()) == 0
    text = capsys.readouterr().out
    assert "peer pc-b master: ahead (+1 theirs, +0 ours)" in text
    assert "shared branches on origin (1):" in text
    assert "pc-b/s2-y" in text and pub[:8] in text
    assert "local branches origin/master does not hold (1):" in text
    assert "s1-open" in text and "[s42]" in text


def test_cli_status_without_fetch_plans_on_the_refs_as_they_are(world, monkeypatch, capsys):
    import json
    from claude_launcher import cli_reposync

    _status_world(world, monkeypatch)
    assert cli_reposync._cmd_status(_status_args(no_fetch=True, json=True)) == 0
    doc = json.loads(capsys.readouterr().out)
    # nothing fetched: the push and the published branch are not here yet
    assert doc["fetched"] is False and doc["plan"]["action"] == "up-to-date"
    assert doc["peers"][0]["distance"]["state"] == "unknown"
    assert doc["shared"] == []
    assert [r["branch"] for r in doc["unmerged"]] == ["s1-open"]
    assert cli_reposync._cmd_status(_status_args(no_fetch=True)) == 0
    assert "(as of the last fetch)" in capsys.readouterr().out


def test_mcp_status_carries_the_same_facts_without_fetching(world, monkeypatch):
    from claude_launcher import reposync_mcp

    theirs, pub = _status_world(world, monkeypatch)
    _git(world["a"], "fetch", "-q", "origin")        # status itself does not fetch
    doc = reposync_mcp.call_tool("repo_sync", {"action": "status", "workspace": "proj",
                                               "remote": "origin"})
    assert doc["fetched"] is False and doc["plan"]["action"] == "ff"
    assert doc["peers"][0]["distance"]["state"] == "ahead"
    assert [r["ref"] for r in doc["shared"]] == ["pc-b/s2-y"]
    assert [(r["branch"], r["session"]) for r in doc["unmerged"]] == [("s1-open", "s42")]


# --- Worktrees per workspace (claunch-kjjta) ------------------------------


def test_summary_lists_the_linked_worktrees_but_not_the_workspace(world, tmp_path):
    import os

    a = world["a"]
    on_branch = tmp_path / "wt-feature"
    detached = tmp_path / "wt-detached"
    _git(a, "worktree", "add", "-q", "-b", "s7-feature", str(on_branch))
    _git(a, "worktree", "add", "-q", "--detach", str(detached), "HEAD")
    workspaces.add(str(on_branch), "proj-wt")    # a workspace that is itself a worktree

    def norm(p):
        return os.path.normcase(os.path.normpath(str(p)))

    async def body(client):
        resp = await client.get("/api/reposync/workspaces/summary", headers=AUTH)
        assert resp.status == 200
        docs = (await resp.json())["workspaces"]
        rows = {norm(r["path"]): r for r in docs["proj"]["worktrees"]}
        assert set(rows) == {norm(on_branch), norm(detached)}
        assert rows[norm(on_branch)]["branch"] == "s7-feature"
        assert rows[norm(on_branch)]["detached"] is False
        assert rows[norm(detached)]["detached"] is True and rows[norm(detached)]["branch"] is None
        assert rows[norm(detached)]["head"] == _git(a, "rev-parse", "HEAD")
        assert not any(r["main"] for r in rows.values())
        # registered at a linked worktree: the others, never itself -- and the
        # others include the main checkout, marked as such (claunch-k9ddh)
        own = {norm(r["path"]): r for r in docs["proj-wt"]["worktrees"]}
        assert set(own) == {norm(a), norm(detached)}
        assert own[norm(a)]["main"] is True and own[norm(a)]["branch"] == "master"
        assert own[norm(detached)]["main"] is False

    _with_app(body)


# --------------------------------------------------------------------------- #
# history (claunch-iaihd)
# --------------------------------------------------------------------------- #
async def _history(client, name="proj"):
    doc = await (await client.get(f"/api/reposync/workspaces/{name}/status?peers=0",
                                  headers=AUTH)).json()
    return doc["history"]


def test_history_records_what_ran_and_under_whose_approval(world):
    """The approval card is in memory and says nothing about whether the
    push ran; the history keeps one line per operation that did, with the
    card a person answered. A refused card ran nothing and adds nothing."""
    a, b = world["a"], world["b"]
    mine = _commit(a, "mine.txt")

    async def body(client):
        assert await _history(client) == []
        plan = await _status_plan(client)
        rid = (await (await client.post("/api/reposync/workspaces/proj/sync",
                                        json=_shown(plan), headers=AUTH)).json())["request"]
        await client.post(f"/api/reposync/requests/{rid}/reject", headers=AUTH)
        await client.post("/api/reposync/workspaces/proj/sync",
                          json=_shown(plan, request=rid), headers=AUTH)
        assert await _history(client) == [], "a refused card ran nothing"

        rid = (await (await client.post("/api/reposync/workspaces/proj/sync",
                                        json=_shown(plan), headers=AUTH)).json())["request"]
        await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        resp = await client.post("/api/reposync/workspaces/proj/sync",
                                 json=_shown(plan, request=rid), headers=AUTH)
        assert resp.status == 200, await resp.text()
        [row] = await _history(client)
        assert row["kind"] == "master" and row["workspace"] == "proj" and row["ok"] is True
        assert (row["action"], row["pushed"], row["before"], row["after"]) == (
            "push", True, mine, mine)
        assert row["by"] == "web" and row["remote"] == "origin" and row["branch"] == "master"
        assert row["approval"]["mode"] == "card" and row["approval"]["request"] == rid
        assert row["approval"]["decided_by"] == "web" and row["approval"]["decided_at"]
        assert row["at"]

        # a fast-forward needs no card and says so
        _git(b, "pull", "-q", "origin", "master")
        tip = _commit(b, "theirs.txt")
        _git(b, "push", "-q", "origin", "master")
        plan = await _status_plan(client)
        await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan), headers=AUTH)
        newest, older = await _history(client)
        assert (newest["action"], newest["pushed"], newest["after"]) == ("ff", False, tip)
        assert newest["approval"] == {"mode": "none"}
        assert older["approval"]["request"] == rid

    _with_app(body)


def test_history_records_a_branch_push_and_the_pull_on_the_other_side(world):
    a, b = world["a"], world["b"]
    _git(a, "switch", "-q", "-c", "s1-work")
    tip = _commit(a, "work.txt")
    workspaces.add(str(b), "proj-b")

    async def push(client):
        rid = (await (await client.post("/api/reposync/workspaces/proj/branch-push",
                                        json={"branch": "s1-work"}, headers=AUTH)).json())["request"]
        await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work", "request": rid}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        [row] = await _history(client)
        assert (row["kind"], row["branch"], row["ref"], row["after"]) == (
            "publish", "s1-work", "pc-a/s1-work", tip)
        assert row["pushed"] is True and row["force"] is False
        assert row["approval"]["request"] == rid

    async def pull(client):
        resp = await client.post("/api/reposync/workspaces/proj-b/branch-pull",
                                 json={"machine": "pc-a", "branch": "s1-work"}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        [row] = await _history(client, "proj-b")
        assert (row["kind"], row["ref"], row["after"], row["pushed"]) == (
            "pull", "pc-a/s1-work", tip, False)
        assert row["approval"] == {"mode": "none"}
        # a failed pull is a line too, with git's refusal
        resp = await client.post("/api/reposync/workspaces/proj-b/branch-pull",
                                 json={"machine": "pc-a", "branch": "nope"}, headers=AUTH)
        assert resp.status == 409
        failed, _ = await _history(client, "proj-b")
        assert failed["ok"] is False and failed["error"]
        # each repository has its own history: proj-b's lines are not proj's
        assert all(r["workspace"] == "proj" for r in await _history(client, "proj"))

    _with_app(push, me="pc-a")
    _with_app(pull, me="pc-b")


def test_history_from_the_cli_names_the_session_and_its_card(world, monkeypatch):
    """The CLI and the MCP tool run on this machine and write the same file;
    a session's push carries the card it waited on."""
    from claude_launcher import cli_reposync, reposync_flow

    a = world["a"]
    mine = _commit(a, "mine.txt")
    monkeypatch.setenv("CLAUNCH_SESSION", "s9")
    monkeypatch.setattr(reposync_flow, "client", lambda: None)
    monkeypatch.setattr(reposync_flow, "file_request",
                        lambda c, card: {"id": "r1", "deadline": "later"})
    monkeypatch.setattr(reposync_flow, "wait_request", lambda c, rid: {
        "id": rid, "status": "approved", "decided_by": "web",
        "decided_at": "2026-10-08T10:00:00+00:00"})
    import argparse
    args = argparse.Namespace(workspace="proj", remote="origin", branch=None, peer=None,
                              dry_run=False, yes=False, json=True)
    assert cli_reposync._cmd_master(args) == 0
    [row] = reposync_flow.read_history(str(a))
    assert (row["by"], row["pushed"], row["after"]) == ("s9", True, mine)
    assert row["approval"] == {"mode": "card", "request": "r1", "decided_by": "web",
                               "decided_at": "2026-10-08T10:00:00+00:00"}


def test_history_is_shared_by_the_worktrees_and_trimmed(world, tmp_path, monkeypatch):
    from claude_launcher import reposync_flow

    a = world["a"]
    wt = tmp_path / "wt-x"
    _git(a, "worktree", "add", "-q", "-b", "s2-x", str(wt))
    entry = reposync_flow.history_entry(str(a), kind="pull", workspace="proj", remote="origin",
                                        branch="x", by="terminal", approval={"mode": "none"})
    reposync_flow.record_history({**entry, "ok": True})
    assert len(reposync_flow.read_history(str(wt))) == 1, "one repository, any of its worktrees"
    assert reposync_flow.read_history(str(world["b"])) == []
    # past the size bound the file keeps its newest lines
    # (a one-byte bound trims on every write, whatever the line length)
    monkeypatch.setattr(reposync_flow, "HISTORY_TRIM_BYTES", 1)
    monkeypatch.setattr(reposync_flow, "HISTORY_KEEP", 3)
    for n in range(10):
        reposync_flow.record_history({**entry, "ok": True, "n": n})
    rows = reposync_flow.read_history(str(a))
    assert [r.get("n") for r in rows] == [9, 8, 7]


_HISTORY_WRITER = r"""
import sys
from claude_launcher import reposync_flow
path, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
reposync_flow.history_path = lambda: path
# a one-byte bound trims on every write; KEEP holds every line both writers add
reposync_flow.HISTORY_TRIM_BYTES = 1
reposync_flow.HISTORY_KEEP = 100000
for n in range(count):
    reposync_flow.record_history({"kind": "pull", "repo_id": "r", "w": tag, "n": n})
"""


def test_history_keeps_every_line_two_processes_append_while_trimming(tmp_path):
    """claunch-iaihd.1: the daemon, the CLI and the MCP tool are separate
    processes on one history file, and a trim rewrites the file -- a line
    another process appended between the trim's read and its write was lost.
    Two processes that trim on every write must leave every line behind."""
    import json
    import sys

    path = str(tmp_path / "reposync-history.jsonl")
    count = 150
    procs = [subprocess.Popen([sys.executable, "-c", _HISTORY_WRITER, path, tag, str(count)])
             for tag in ("a", "b")]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh.read().splitlines()]
    for tag in ("a", "b"):
        assert [r["n"] for r in rows if r["w"] == tag] == list(range(count)), tag


def test_history_gives_up_the_line_when_the_lock_is_held(tmp_path, monkeypatch):
    """A writer that cannot get the lock loses its line; the operation the line
    describes does not fail and does not hang."""
    from claude_launcher import reposync_flow

    path = str(tmp_path / "reposync-history.jsonl")
    monkeypatch.setattr(reposync_flow, "history_path", lambda: path)
    monkeypatch.setattr(reposync_flow, "HISTORY_LOCK_TIMEOUT", 0.1)
    with reposync_flow._history_lock(path):
        started = time.monotonic()
        # another handle on the lock file: the same process still waits (Windows
        # byte-range locks and flock are both per open file, not per process)
        reposync_flow.record_history({"kind": "pull", "repo_id": "r"})
        assert time.monotonic() - started < 5
    assert not (tmp_path / "reposync-history.jsonl").exists()
    reposync_flow.record_history({"kind": "pull", "repo_id": "r"})
    assert (tmp_path / "reposync-history.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_history_entry_is_built_off_the_event_loop(world, monkeypatch):
    """claunch-iaihd.2: history_entry runs git (repo_identity); the Sync
    panel's handlers build it inside the thread they hand to to_thread, so
    no git call blocks the daemon's event loop."""
    import threading

    from claude_launcher import reposync_flow

    a, b = world["a"], world["b"]
    real = reposync_flow.history_entry
    threads = []

    def spy(*args, **kw):
        threads.append(threading.get_ident())
        return real(*args, **kw)

    monkeypatch.setattr(reposync_flow, "history_entry", spy)
    _git(b, "pull", "-q", "origin", "master")
    _commit(b, "theirs.txt")
    _git(b, "push", "-q", "origin", "master")
    _git(a, "branch", "s1-work")
    workspaces.add(str(b), "proj-b")
    loop_threads = []

    async def body(client):
        loop_threads.append(threading.get_ident())
        # a fast-forward: no card
        plan = await _status_plan(client)
        resp = await client.post("/api/reposync/workspaces/proj/sync", json=_shown(plan),
                                 headers=AUTH)
        assert resp.status == 200, await resp.text()
        rid = (await (await client.post("/api/reposync/workspaces/proj/branch-push",
                                        json={"branch": "s1-work"}, headers=AUTH)).json())["request"]
        await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        resp = await client.post("/api/reposync/workspaces/proj/branch-push",
                                 json={"branch": "s1-work", "request": rid}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        resp = await client.post("/api/reposync/workspaces/proj-b/branch-pull",
                                 json={"machine": "pc-a", "branch": "s1-work"}, headers=AUTH)
        assert resp.status == 200, await resp.text()
        assert [r["kind"] for r in await _history(client)] == ["publish", "master"]

    _with_app(body)
    assert len(threads) == 3, threads
    assert loop_threads[0] not in threads


# --------------------------------------------------------------------------- #
# disk usage (claunch-2oait)
# --------------------------------------------------------------------------- #
def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_usage_counts_each_checkout_once(world, tmp_path):
    """The workspace and every linked worktree get a line; a worktree under
    the workspace's directory is not counted in the workspace's line too, so
    the lines add up to the total."""
    import os

    from claude_launcher import workspace_usage

    a = world["a"]
    inner = a / ".claude" / "worktrees" / "wt-in"
    outer = tmp_path / "wt-out"
    _git(a, "worktree", "add", "-q", "-b", "s1-in", str(inner))
    _git(a, "worktree", "add", "-q", "-b", "s1-out", str(outer))
    _write(inner / "big.bin", 100_000)
    _write(outer / "mid.bin", 30_000)
    _write(a / "small.bin", 5_000)

    doc = workspace_usage.measure(str(a))
    by = {os.path.normcase(p["path"]): p for p in doc["parts"]}
    assert set(by) == {os.path.normcase(str(x)) for x in (a, inner, outer)}
    assert by[os.path.normcase(str(a))]["workspace"] is True
    assert by[os.path.normcase(str(inner))]["bytes"] >= 100_000
    assert by[os.path.normcase(str(outer))]["bytes"] >= 30_000
    # the inner worktree's 100 KB is not in the workspace's own line
    assert 5_000 <= by[os.path.normcase(str(a))]["bytes"] < 100_000
    assert doc["bytes"] == sum(p["bytes"] for p in doc["parts"])
    assert doc["files"] == sum(p["files"] for p in doc["parts"])

    # from the linked worktree, the main checkout is one of the lines, and
    # the worktree is still counted once
    doc2 = workspace_usage.measure(str(inner))
    assert doc2["parts"][0]["path"] == str(inner) and doc2["parts"][0]["workspace"]
    assert doc2["bytes"] == doc["bytes"]


def test_usage_does_not_follow_links(tmp_path):
    import os

    from claude_launcher import workspace_usage

    root = tmp_path / "r"
    _write(root / "f.bin", 1_000)
    far = tmp_path / "far"
    _write(far / "huge.bin", 500_000)
    made = []
    try:
        os.symlink(str(far), str(root / "link"), target_is_directory=True)
        made.append("link")
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        # a junction needs no privilege, and is what Windows checkouts carry
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junction"), str(far)],
                              capture_output=True)
        if done.returncode == 0:
            made.append("junction")
    if not made:
        pytest.skip("this machine can create neither a directory symlink nor a junction")
    size, files = workspace_usage.walk_size(str(root))
    assert size < 500_000 and files == 1 + len(made)


def _usage(client):
    async def get():
        return (await (await client.get("/api/workspaces/usage", headers=AUTH)).json())["workspaces"]
    return get()


async def _usage_settled(client, name="proj"):
    for _ in range(200):
        row = (await _usage(client)).get(name) or {}
        if row and not row.get("running"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("the measurement did not finish")


def test_usage_measures_in_the_background_and_keeps_the_answer(world, monkeypatch):
    import threading

    from claude_launcher import workspace_usage

    real = workspace_usage.measure
    gate = threading.Event()
    runs = []

    def slow(path):
        runs.append(path)
        gate.wait(10)
        return real(path)

    monkeypatch.setattr(workspace_usage, "measure", slow)

    async def body(client):
        assert await _usage(client) == {}
        resp = await client.post("/api/workspaces/proj/usage", headers=AUTH)
        assert resp.status == 202
        assert (await _usage(client))["proj"] == {"running": True}
        # a second press while it runs starts nothing
        resp = await client.post("/api/workspaces/proj/usage", headers=AUTH)
        assert resp.status == 202
        gate.set()
        row = await _usage_settled(client)
        assert len(runs) == 1
        assert row["bytes"] > 0 and row["files"] > 0 and row["at"]
        assert row["parts"][0]["workspace"] is True
        assert (await client.post("/api/workspaces/nope/usage", headers=AUTH)).status == 404

    _with_app(body)

    # the cache outlives the daemon: a new app reads the same answer
    async def again(client):
        row = (await _usage(client))["proj"]
        assert row["bytes"] > 0 and "running" not in row

    _with_app(again)


def test_usage_failure_keeps_the_last_number(world, monkeypatch):
    from claude_launcher import workspace_usage

    async def first(client):
        await client.post("/api/workspaces/proj/usage", headers=AUTH)
        return await _usage_settled(client)

    rows = []

    async def run_first(client):
        rows.append(await first(client))

    _with_app(run_first)

    def broken(path):
        raise OSError("access denied")

    monkeypatch.setattr(workspace_usage, "measure", broken)

    async def second(client):
        await client.post("/api/workspaces/proj/usage", headers=AUTH)
        row = await _usage_settled(client)
        while "error" not in row:
            await asyncio.sleep(0.05)
            row = (await _usage(client))["proj"]
        assert row["error"] == "access denied" and row["failed_at"]
        assert (row["bytes"], row["at"]) == (rows[0]["bytes"], rows[0]["at"])

    _with_app(second)
