"""An agent's request to resume an exited session, gated on a person
(claunch-yjhgx).

The user's spec: give agents an MCP tool that brings back an exited session,
but make it ask for permission through the notification the way a daemon
restart does, and wait for the answer with no time limit.

What these pin:

* filing relaunches nothing, and the request has no deadline -- it is still
  pending after any amount of time, and survives a daemon restart (a new gate
  reads it back from disk);
* only an exited session can be the target (running, held, unknown, oneself
  are refused), and one pending request per target;
* approve respawns through the manager, reject does not; a failed respawn is
  reported as ``failed`` with the error; every settlement is typed into the
  asking session;
* a target that came back some other way closes the request as ``moot``;
* only the asker withdraws;
* the HTTP routes and the MCP tool carry all of it under the calling session.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from claude_launcher import mesh_mcp
from claude_launcher.daemon import resume_gate
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

AUTH = {"Authorization": "Bearer sekrit"}


class _FakeSession:
    def __init__(self, name: str, *, exited: bool = False, held: bool = False,
                 made: int = 0):
        self.sdef = SessionDef(name=name, harness="py", cwd=f"/w/{name}")
        self.created_at = "2020-01-01T00:00:%02d+00:00" % made
        self.exited = exited
        self.exit_code = 0 if exited else None
        self.pid = None
        self.last_output_at = None
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = "2026-10-15T00:00:00+00:00" if exited else None
        self.archived_at = None
        self.held_at = "2026-10-15T00:00:00+00:00" if held else None
        self.delivered = []

    def status(self):
        return "exited" if self.exited else "idle"

    def delivery_held(self):
        return False

    def queue_delivery(self, message):
        self.delivered.append(message)
        return True

    async def shutdown(self):
        self.exited = True


def _manager(*sessions) -> SessionManager:
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    for i, session in enumerate(sessions):
        session.created_at = "2020-01-01T00:00:%02d+00:00" % i
        mgr._sessions[session.sdef.name] = session
    return mgr


def _fake_respawn(mgr, *, fail: str = ""):
    """Stand in for the manager's relaunch: flip the record back to running
    (or raise), and count the calls."""
    calls = []

    async def respawn_async(name):
        calls.append(name)
        if fail:
            raise RuntimeError(fail)
        mgr._sessions[name].exited = False
        return mgr._sessions[name]

    mgr.respawn_async = respawn_async
    return calls


def _gate(mgr, tmp_path, notes=None):
    notes = notes if notes is not None else []

    def notify(name, message):
        notes.append((name, message))
        return True

    return resume_gate.ResumeGate(
        manager=mgr, path=tmp_path / resume_gate.FILE, notify=notify
    )


# --------------------------------------------------------------------------- #
# the ask
# --------------------------------------------------------------------------- #
def test_filing_relaunches_nothing_and_has_no_deadline(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    calls = _fake_respawn(mgr)
    gate = _gate(mgr, tmp_path)

    record = gate.submit(session="lead", target="rev", reason="need its review notes")
    assert record["status"] == "pending"
    assert "deadline" not in record
    assert record["target_cwd"] == "/w/rev"
    assert calls == []
    assert mgr._sessions["rev"].exited is True
    assert [r["id"] for r in gate.list()] == [record["id"]]


@pytest.mark.parametrize(
    "target, reason, status, match",
    [
        ("", "x", 400, "target"),
        ("rev", "", 400, "reason is required"),
        ("lead", "x", 400, "itself"),
        ("ghost", "x", 400, "no session named"),
        ("live", "x", 409, "running"),
        ("sleepy", "x", 409, "held"),
    ],
)
def test_only_an_exited_session_can_be_asked_for(home, tmp_path, target, reason,
                                                  status, match):
    mgr = _manager(
        _FakeSession("lead"),
        _FakeSession("rev", exited=True),
        _FakeSession("live"),
        _FakeSession("sleepy", exited=True, held=True),
    )
    gate = _gate(mgr, tmp_path)
    with pytest.raises(resume_gate.GateRefused, match=match) as info:
        gate.submit(session="lead", target=target, reason=reason)
    assert info.value.status == status
    assert gate.list() == []


def test_one_pending_request_per_target(home, tmp_path):
    mgr = _manager(_FakeSession("a"), _FakeSession("b"), _FakeSession("rev", exited=True))
    gate = _gate(mgr, tmp_path)
    first = gate.submit(session="a", target="rev", reason="one")
    with pytest.raises(resume_gate.GateBusy, match=first["id"]):
        gate.submit(session="b", target="rev", reason="two")
    gate.reject(first["id"])
    second = gate.submit(session="b", target="rev", reason="two")
    assert second["status"] == "pending"


def test_a_pending_request_outlives_the_daemon(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    gate = _gate(mgr, tmp_path)
    record = gate.submit(session="lead", target="rev", reason="need it")
    # A respawn that was in flight when the daemon died goes back to pending.
    gate.records[record["id"]]["status"] = resume_gate.RESUMING
    gate._save()

    again = _gate(mgr, tmp_path)
    reread = again.get(record["id"])
    assert reread["status"] == "pending"
    assert reread["reason"] == "need it"
    on_disk = json.loads((tmp_path / resume_gate.FILE).read_text(encoding="utf-8"))
    assert [r["id"] for r in on_disk] == [record["id"]]


# --------------------------------------------------------------------------- #
# the settlement
# --------------------------------------------------------------------------- #
def test_approve_respawns_and_tells_the_asker(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    calls = _fake_respawn(mgr)
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    record = asyncio.run(gate.approve(rid, decided_by="web"))
    assert record["status"] == "approved"
    assert record["decided_by"] == "web"
    assert calls == ["rev"]
    assert notes and notes[0][0] == "lead"
    assert f"[claunch resume-request {rid}] approved" in notes[0][1]
    # Settled: a second press finds nothing pending and respawns nothing.
    assert asyncio.run(gate.approve(rid)) is None
    assert calls == ["rev"]


def test_a_failed_respawn_is_reported_as_failed(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    _fake_respawn(mgr, fail="no conversation to resume")
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    record = asyncio.run(gate.approve(rid))
    assert record["status"] == "failed"
    assert record["error"] == "no conversation to resume"
    assert "failed: no conversation to resume" in notes[0][1]


def test_reject_relaunches_nothing_and_tells_the_asker(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    calls = _fake_respawn(mgr)
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    record = gate.reject(rid)
    assert record["status"] == "rejected"
    assert calls == []
    assert "rejected: session rev stays exited" in notes[0][1]
    assert gate.reject(rid) is None


def test_only_the_asker_withdraws(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("other"),
                   _FakeSession("rev", exited=True))
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    with pytest.raises(resume_gate.GateRefused) as info:
        gate.withdraw(rid, session="other")
    assert info.value.status == 403
    record = gate.withdraw(rid, session="lead")
    assert record["status"] == "withdrawn"
    assert notes == []          # the asker knows; nothing is typed back


def test_a_target_back_by_other_means_closes_the_request(home, tmp_path):
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    notes = []
    gate = _gate(mgr, tmp_path, notes)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]

    mgr._sessions["rev"].exited = False      # the operator pressed Respawn
    assert gate.get(rid)["status"] == "moot"
    assert asyncio.run(gate.approve(rid)) is None
    assert "closed: session rev is running again" in notes[0][1]


def test_an_unknown_target_after_a_restart_stays_pending(home, tmp_path):
    """Right after a daemon start the restore may not have put the record
    back yet; the question must not be closed on that."""
    mgr = _manager(_FakeSession("lead"), _FakeSession("rev", exited=True))
    gate = _gate(mgr, tmp_path)
    rid = gate.submit(session="lead", target="rev", reason="r")["id"]
    del mgr._sessions["rev"]
    assert gate.get(rid)["status"] == "pending"


# --------------------------------------------------------------------------- #
# the HTTP surface
# --------------------------------------------------------------------------- #
async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_the_routes_file_list_and_approve(home, tmp_path):
    async def run():
        lead = _FakeSession("lead")
        mgr = _manager(lead, _FakeSession("rev", exited=True))
        calls = _fake_respawn(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = await _client(app)
        try:
            assert (await client.get("/api/resume-requests")).status == 401
            assert (
                await client.post("/api/resume-requests/x/approve")
            ).status == 401

            resp = await client.post(
                "/api/resume-requests",
                json={"session": "lead", "target": "rev", "reason": "need it"},
                headers=AUTH,
            )
            assert resp.status == 200, await resp.text()
            rid = (await resp.json())["request"]["id"]

            resp = await client.post(
                "/api/resume-requests",
                json={"session": "lead", "target": "rev", "reason": "again"},
                headers=AUTH,
            )
            assert resp.status == 409

            resp = await client.post(
                "/api/resume-requests",
                json={"session": "lead", "target": "rev"},
                headers=AUTH,
            )
            assert resp.status == 400

            rows = (await (await client.get(
                "/api/resume-requests", headers=AUTH)).json())["requests"]
            assert [(r["id"], r["status"]) for r in rows] == [(rid, "pending")]

            resp = await client.post(
                f"/api/resume-requests/{rid}/withdraw",
                json={"session": "someone"}, headers=AUTH,
            )
            assert resp.status == 403

            resp = await client.post(
                f"/api/resume-requests/{rid}/approve", headers=AUTH
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["request"]["status"] == "approved"
            assert calls == ["rev"]
            assert any(f"resume-request {rid}" in m for m in lead.delivered)

            resp = await client.post(
                f"/api/resume-requests/{rid}/reject", headers=AUTH
            )
            assert resp.status == 409
            resp = await client.get(f"/api/resume-requests/{rid}", headers=AUTH)
            assert (await resp.json())["request"]["status"] == "approved"
            assert (await client.get(
                "/api/resume-requests/nope", headers=AUTH)).status == 404
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
class _FakeDaemonClient:
    def __init__(self):
        self.calls = []

    def post(self, path, body=None, **kw):
        self.calls.append(("POST", path, body))
        return {"ok": True, "request": {"id": "r1", "status": "pending"}}

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        return {"request": {"id": "r1", "status": "pending"}}


def test_the_mcp_tool_files_under_the_calling_session(home, monkeypatch):
    fake = _FakeDaemonClient()
    monkeypatch.setattr(mesh_mcp, "_client", lambda: fake)
    monkeypatch.setenv("CLAUNCH_SESSION", "lead")

    reply = mesh_mcp.call_tool(
        "resume_session", {"session": "rev", "reason": "need its notes"}
    )
    assert fake.calls[-1] == (
        "POST", "/api/resume-requests",
        {"session": "lead", "target": "rev", "reason": "need its notes"},
    )
    assert "no deadline" in reply["next"]

    mesh_mcp.call_tool("resume_session", {"request": "r1"})
    assert fake.calls[-1] == ("GET", "/api/resume-requests/r1", None)

    mesh_mcp.call_tool("resume_session", {"request": "r1", "withdraw": True})
    assert fake.calls[-1] == (
        "POST", "/api/resume-requests/r1/withdraw", {"session": "lead"},
    )

    with pytest.raises(mesh_mcp.MeshMcpError, match="reason"):
        mesh_mcp.call_tool("resume_session", {"session": "rev"})
    with pytest.raises(mesh_mcp.MeshMcpError, match="session"):
        mesh_mcp.call_tool("resume_session", {"reason": "x"})
    with pytest.raises(mesh_mcp.MeshMcpError, match="request"):
        mesh_mcp.call_tool("resume_session", {"withdraw": True})
