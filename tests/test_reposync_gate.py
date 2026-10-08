"""reposync's approval and peer surfaces (claunch-dh73y).

The gate a session's push waits behind, the daemon routes around it and the
read-only /peer/repos description, the binding of an approval to the exact
plan that was shown, the MCP tool's two-call flow, and the guard that keeps
an agent session from switching the approval off.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from claude_launcher import reposync, reposync_flow as flow, reposync_mcp, store, workspaces
from claude_launcher.daemon import reposync_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}


def _git(cwd, *args):
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _card(**over):
    card = {
        "repo": "/r", "remote": "origin", "branch": "master", "action": "push",
        "local": "a" * 40, "upstream": "b" * 40, "summary": "push 1 commit(s)",
    }
    card.update(over)
    return card


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_gate_settles_once():
    gate = reposync_gate.ReposyncGate()
    rec = gate.submit(session="s1", plan=_card())
    assert rec["status"] == "pending" and rec["plan"]["local"] == "a" * 40
    assert gate.approve(rec["id"])["status"] == "approved"
    assert gate.reject(rec["id"]) is None  # already settled


def test_gate_unanswered_expires_as_a_refusal():
    gate = reposync_gate.ReposyncGate(timeout=0)
    rec = gate.submit(session="s1", plan=_card())
    time.sleep(1.1)
    got = gate.get(rec["id"])
    assert got["status"] == "expired" and got["decided_by"] == "timeout"
    assert gate.approve(rec["id"]) is None


def test_gate_keeps_only_the_card_fields():
    gate = reposync_gate.ReposyncGate()
    rec = gate.submit(session="s1", plan={**_card(), "secret_url": "https://u:tok@h/o/r"})
    assert "secret_url" not in rec["plan"]


def test_gate_refuses_a_plan_without_an_action():
    with pytest.raises(ValueError):
        reposync_gate.ReposyncGate().submit(session="s1", plan={})


# --------------------------------------------------------------------------- #
# the daemon routes
# --------------------------------------------------------------------------- #
def _with_app(fn):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await fn(client)
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_request_routes_round_trip(home):
    async def body(client):
        resp = await client.post("/api/reposync/requests",
                                 json={"session": "s1", "plan": _card()}, headers=AUTH)
        assert resp.status == 200
        rid = (await resp.json())["request"]["id"]
        listed = await (await client.get("/api/reposync/requests", headers=AUTH)).json()
        assert [r["id"] for r in listed["requests"]] == [rid]
        resp = await client.post(f"/api/reposync/requests/{rid}/reject", headers=AUTH)
        assert (await resp.json())["request"]["status"] == "rejected"
        resp = await client.post(f"/api/reposync/requests/{rid}/approve", headers=AUTH)
        assert resp.status == 409
        resp = await client.get("/api/reposync/requests/nope", headers=AUTH)
        assert resp.status == 404

    _with_app(body)


def test_request_routes_need_the_token(home):
    async def body(client):
        resp = await client.post("/api/reposync/requests", json={"plan": _card()})
        assert resp.status == 401

    _with_app(body)


def test_peer_repos_describes_registered_workspaces_by_key(home, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "base")
    _git(repo, "remote", "add", "origin", "https://user:tok@git.example/Org/Proj.git")
    workspaces.add(str(repo), "proj")

    async def body(client):
        resp = await client.post("/peer/repos", json={})
        assert resp.status == 200
        doc = await resp.json()
        [row] = doc["repos"]
        assert row["workspace"] == "proj"
        assert row["remotes"] == [{"name": "origin", "key": "git.example/org/proj"}]
        assert "master" in row["branches"]
        assert "tok" not in str(doc)

    _with_app(body)


# --------------------------------------------------------------------------- #
# matching across peers
# --------------------------------------------------------------------------- #
def test_matches_pairs_remotes_by_address_not_name(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "github.ncsoft.com", "https://github.ncsoft.com/RnIAgentDev/claunch.git")
    _git(repo, "remote", "add", "origin", "https://github.com/inosphe/claude-launcher.git")
    peers = {"shared-daemon-main": [
        {"workspace": "claunch", "remotes": [{"name": "origin", "key": "github.ncsoft.com/rniagentdev/claunch"}],
         "branches": {}},
        {"workspace": "gds6", "remotes": [{"name": "origin", "key": "github.ncsoft.com/rniagentdev/gds6"}],
         "branches": {}},
    ]}
    [row] = flow.matches(str(repo), peers)
    assert (row["machine"], row["workspace"], row["local_remote"], row["peer_remote"]) == (
        "shared-daemon-main", "claunch", "github.ncsoft.com", "origin")


# --------------------------------------------------------------------------- #
# an approval is bound to the plan it was shown
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", flow.BOUND)
def test_approval_does_not_cover_a_moved_plan(field):
    record = {"status": "approved", "plan": _card()}
    assert flow.approval_matches(record, _card())
    assert not flow.approval_matches(record, _card(**{field: "moved"}))


def test_pending_or_rejected_is_not_an_approval():
    for status in ("pending", "rejected", "expired"):
        assert not flow.approval_matches({"status": status, "plan": _card()}, _card())


class _Client:
    def __init__(self):
        self.records = {}

    def post(self, path, body=None, **kw):
        assert path == "/api/reposync/requests"
        rec = reposync_gate.ReposyncGate().submit(session=body["session"], plan=body["plan"])
        self.records[rec["id"]] = rec
        return {"request": rec}

    def get(self, path, **kw):
        if path.startswith("/api/reposync/requests/"):
            return {"request": self.records[path.rsplit("/", 1)[1]]}
        raise AssertionError(path)


def test_authorize_files_then_waits_then_goes(monkeypatch):
    monkeypatch.setattr(flow, "approval_mode", lambda: flow.ASK)
    c = _Client()
    first = flow.authorize(c, _card(), None)
    assert first["go"] is False and first["status"] == "waiting_approval"
    rid = first["request"]
    assert flow.authorize(c, _card(), rid)["status"] == "waiting_approval"
    c.records[rid]["status"] = "approved"
    assert flow.authorize(c, _card(), rid)["go"] is True
    stale = flow.authorize(c, _card(local="c" * 40), rid)
    assert stale["status"] == "stale_approval"


def test_authorize_auto_needs_nobody(monkeypatch):
    monkeypatch.setattr(flow, "approval_mode", lambda: flow.AUTO)
    assert flow.authorize(None, _card(), None)["go"] is True


# --------------------------------------------------------------------------- #
# the MCP tool's two calls, on real git
# --------------------------------------------------------------------------- #
def _world(tmp_path: Path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "master")
    _git(seed, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    remote = tmp_path / "shared.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    a = tmp_path / "a"
    subprocess.run(["git", "clone", "-q", str(remote), str(a)], check=True)
    _git(a, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "mine")
    return remote, a


def test_mcp_sync_waits_for_the_person_then_pushes(monkeypatch, tmp_path):
    remote, a = _world(tmp_path)
    c = _Client()
    monkeypatch.setattr(flow, "client", lambda: c)
    monkeypatch.setattr(flow, "approval_mode", lambda: flow.ASK)
    monkeypatch.setattr(flow, "discover", lambda repo, c, only=(): ([], []))
    monkeypatch.setattr(flow, "scratch", lambda: str(tmp_path / "scratch"))
    args = {"action": "sync", "workspace": str(a)}

    first = reposync_mcp.call_tool("repo_sync", args)
    assert first["status"] == "waiting_approval"
    assert _git(remote, "rev-parse", "master") != _git(a, "rev-parse", "master")

    c.records[first["request"]]["status"] = "approved"
    second = reposync_mcp.call_tool("repo_sync", {**args, "request": first["request"]})
    assert second["status"] == "done" and second["pushed"]
    assert _git(remote, "rev-parse", "master") == _git(a, "rev-parse", "master")


def test_mcp_rejected_request_pushes_nothing(monkeypatch, tmp_path):
    remote, a = _world(tmp_path)
    c = _Client()
    monkeypatch.setattr(flow, "client", lambda: c)
    monkeypatch.setattr(flow, "approval_mode", lambda: flow.ASK)
    monkeypatch.setattr(flow, "discover", lambda repo, c, only=(): ([], []))
    before = _git(remote, "rev-parse", "master")
    first = reposync_mcp.call_tool("repo_sync", {"action": "sync", "workspace": str(a)})
    c.records[first["request"]]["status"] = "rejected"
    second = reposync_mcp.call_tool(
        "repo_sync", {"action": "sync", "workspace": str(a), "request": first["request"]})
    assert second["status"] == "rejected"
    assert _git(remote, "rev-parse", "master") == before


# --------------------------------------------------------------------------- #
# the setting is the person's
# --------------------------------------------------------------------------- #
def test_agent_session_cannot_switch_approval_off(home, monkeypatch, capsys):
    from claude_launcher import cli_sessions

    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    code = cli_sessions._cmd_daemon_config(argparse.Namespace(key="reposync_push", value="auto"))
    assert code == 1 and "agent session" in capsys.readouterr().err
    assert store.daemon_config()["reposync_push"] == "ask"


def test_person_can_switch_approval_off(home, monkeypatch):
    from claude_launcher import cli_sessions

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.setattr(cli_sessions.daemon_client, "connect", lambda: None)
    assert cli_sessions._cmd_daemon_config(argparse.Namespace(key="reposync_push", value="auto")) == 0
    assert flow.approval_mode() == flow.AUTO
