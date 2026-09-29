"""The urgent one-shot send: the exception to the member graph.

Built by hand like test_mesh_wire.py (no PTYs). What each test pins is a
requirement from the issue: recorded in both meshes, no change to the graph,
no wire request, no broadcast, and an authority check that does not read any
caller-supplied ``external`` claim.
"""

from __future__ import annotations

import pytest

from claude_launcher.daemon import mesh as mesh_mod
from claude_launcher.daemon.mesh import MeshError, MeshManager, Member

from test_mesh_wire import _Manager, _connect, _member


def _two_meshes():
    mgr = _Manager()
    for n in ("lead", "s1", "far"):
        mgr.add(n)
    mm = MeshManager(mgr, settle=0.01)
    mm.create("home")
    mm.create("other")
    home, other = mm.get("home"), mm.get("other")
    _member(home, "lead", "lead", role="leader")
    _member(home, "w1", "s1", role="worker")
    _connect(home, "lead", "w1")
    _member(other, "qf1", "s26-qf1", role="worker")
    _member(other, "boss", "boss", role="leader")
    return mm, home, other


def test_leader_reaches_unconnected_member_in_same_mesh():
    mm, home, _ = _two_meshes()
    _member(home, "w2", "s2", role="worker")          # wired, no edge to lead
    assert not home.connected("lead", "w2")
    edges_before = dict(home.member_edges)

    res = mm.urgent_send("home", "lead", "w2", "stop pushing", reason="release freeze now")

    assert res["urgent"]["authority"] == "leader"
    msg = home.messages[-1]
    assert msg["type"] == "fyi" and msg["to"] == "w2"
    assert msg["ref"]["urgent"]["reason"] == "release freeze now"
    assert "URGENT" in msg["body"] and "stop pushing" in msg["body"]
    assert home.addressed_to(msg, "w2")                # delivery will re-derive it
    assert home.member_edges == edges_before           # graph untouched
    assert not home.connected("lead", "w2")
    assert mm.wire_request_rows("home") == []          # no wire request
    assert res["audited_in"] == ["home"]


def test_cross_mesh_delivers_and_audits_in_both_meshes():
    mm, home, other = _two_meshes()

    res = mm.urgent_send("home", "lead", "s26-qf1", "call me", reason="no shared mesh exists")

    delivered = other.messages[-1]
    assert delivered["to"] == "qf1" and other.addressed_to(delivered, "qf1")
    assert delivered["ref"]["urgent"]["from_mesh"] == "home"
    audit = home.messages[-1]
    assert audit["to"] == []                            # recorded, delivered to nobody
    assert audit["ref"]["urgent"]["delivered_id"] == delivered["id"]
    assert not any(home.addressed_to(audit, h) for h in home.members)
    assert set(res["audited_in"]) == {"home", "other"}
    assert "qf1" not in [h for h in home.members]      # nobody was enrolled
    assert not other.member_edges and not mm.wire_request_rows("other")


def test_worker_is_refused():
    mm, home, other = _two_meshes()
    with pytest.raises(MeshError, match="only a leader or the operator"):
        mm.urgent_send("home", "s1", "s26-qf1", "hi", reason="worker wants to try")
    assert not other.messages and not home.messages


def test_operator_needs_no_session_and_is_not_rate_limited():
    mm, _, other = _two_meshes()
    for i in range(5):
        mm.urgent_send("home", "", "s26-qf1", f"m{i}", reason="operator override")
    assert len(other.messages) == 5
    assert other.messages[-1]["ref"]["urgent"]["authority"] == "operator"


def test_single_recipient_only():
    mm, *_ = _two_meshes()
    for bad in ("*", "@in_review", "", "  "):
        with pytest.raises(MeshError):
            mm.urgent_send("home", "lead", bad, "x", reason="a valid long reason")


def test_reason_and_body_required():
    mm, *_ = _two_meshes()
    with pytest.raises(MeshError, match="reason"):
        mm.urgent_send("home", "lead", "s26-qf1", "x", reason="short")
    with pytest.raises(MeshError, match="empty"):
        mm.urgent_send("home", "lead", "s26-qf1", "  ", reason="a valid long reason")


def test_rate_limits_per_sender_and_per_pair(monkeypatch):
    mm, home, other = _two_meshes()
    _member(other, "q2", "s27", role="worker")
    _member(other, "q3", "s28", role="worker")
    _member(other, "q4", "s29x", role="worker")
    mm.urgent_send("home", "lead", "s26-qf1", "1", reason="first urgent send")
    with pytest.raises(MeshError, match="one per pair"):
        mm.urgent_send("home", "lead", "s26-qf1", "again", reason="second same target")
    mm.urgent_send("home", "lead", "s27", "2", reason="second urgent send")
    mm.urgent_send("home", "lead", "s28", "3", reason="third urgent send")
    with pytest.raises(MeshError, match="3 per hour"):
        mm.urgent_send("home", "lead", "s29x", "4", reason="fourth urgent send")


def test_remote_target_is_refused_with_reason():
    mm, home, other = _two_meshes()
    _member(other, "rem", "s-remote", role="worker", machine="elsewhere")
    with pytest.raises(MeshError, match="another machine"):
        mm.urgent_send("home", "lead", "s-remote", "x", reason="a valid long reason")


def test_unknown_target_and_self_and_ambiguity():
    mm, home, other = _two_meshes()
    with pytest.raises(MeshError, match="no member named"):
        mm.urgent_send("home", "lead", "ghost", "x", reason="a valid long reason")
    with pytest.raises(MeshError, match="yourself"):
        mm.urgent_send("home", "lead", "lead", "x", reason="a valid long reason")
    _member(other, "dup", "s-shared", role="worker")
    _mesh3 = mm.create("third")
    _member(mm.get("third"), "d3", "s-shared", role="worker")
    with pytest.raises(MeshError, match="name one"):
        mm.urgent_send("home", "lead", "s-shared", "x", reason="a valid long reason")
    res = mm.urgent_send(
        "home", "lead", "s-shared", "x", reason="a valid long reason", target_mesh="third"
    )
    assert res["urgent"]["to_mesh"] == "third"


def test_http_route_ignores_external_and_maps_refusals(home):
    """`external: true` in the body buys a worker nothing on this route."""
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app

    async def run():
        mm, home_mesh, other = _two_meshes()
        app = build_app(mm.manager, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        auth = {"Authorization": "Bearer sekrit"}
        try:
            resp = await client.post(
                "/api/mesh/home/urgent", headers=auth,
                json={"from": "s1", "to": "s26-qf1", "body": "x",
                      "reason": "worker forging external", "external": True},
            )
            assert resp.status >= 400 and not other.messages
            resp = await client.post(
                "/api/mesh/home/urgent", headers=auth,
                json={"from": "lead", "to": "s26-qf1", "body": "hello",
                      "reason": "leader with a real need"},
            )
            data = await resp.json()
            assert resp.status == 200, data
            assert data["audited_in"] == ["other", "home"]
            assert other.messages[-1]["ref"]["urgent"]["from"] == "lead"
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_exposes_urgent_send_with_required_reason():
    from claude_launcher import mesh_mcp

    tool = next(t for t in mesh_mcp.TOOLS if t["name"] == "urgent_send")
    assert "reason" in tool["inputSchema"]["required"]


def test_operator_authority_is_written_into_both_audit_records():
    mm, home, other = _two_meshes()
    mm.urgent_send("home", "", "s26-qf1", "x", reason="operator override")
    assert other.messages[-1]["ref"]["urgent"]["authority"] == "operator"
    assert home.messages[-1]["ref"]["urgent"]["authority"] == "operator"


def test_cli_sends_the_managed_session_as_from(monkeypatch):
    """Inside a session the CLI never sends an empty `from` (= operator)."""
    import argparse

    from claude_launcher import cli_mesh

    sent = {}

    class FakeClient:
        def post(self, path, body, **kw):
            sent.update(path=path, body=body)
            return {"id": "m1", "urgent": {}, "audited_in": []}

    monkeypatch.setattr(cli_mesh.daemon_client, "ensure_running", lambda: FakeClient())
    monkeypatch.setenv("CLAUNCH_SESSION", "s29")
    args = argparse.Namespace(
        mesh="home", to="x", text=["hi"], reason="a valid long reason",
        target_mesh=None, session="",
    )
    assert cli_mesh._cmd_urgent(args) == 0
    assert sent["body"]["from"] == "s29"
    monkeypatch.delenv("CLAUNCH_SESSION")
    assert cli_mesh._cmd_urgent(args) == 0
    assert sent["body"]["from"] == ""
