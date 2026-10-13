"""Configurable agent-reported Y/N status checks."""

from __future__ import annotations

import asyncio
import sys
import time

from claude_launcher import harnesses, status_checks_mcp, store
from claude_launcher.daemon import status_checks
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager


BEARER = {"Authorization": "Bearer sekrit"}


async def _serve(mgr):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(build_app(
        mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr)
    )))
    await client.start_server()
    return client


def _register_py_harness() -> None:
    store.update(lambda doc: doc.update({
        "harnesses": {"py": {"command": [sys.executable, "-u", "-c", "import time; time.sleep(60)"]}}
    }))


def test_status_check_storage_keeps_reports_per_session(home):
    rows = status_checks.set_entries([
        {"name": "Tests", "question": "Tests passed?"},
        {"name": "Merged", "question": "Merged?", "enabled": False},
    ])
    assert len(rows) == 2 and rows[0]["enabled"] is True

    reported = status_checks.report("s1", [{"id": rows[0]["id"], "answer": "yes"}])
    assert reported[0]["report"]["answer"] == "yes"
    assert reported[0]["report"]["source"] == "agent"
    assert status_checks.session_entries("s2", enabled_only=True) == [
        {"id": rows[0]["id"], "name": "Tests", "question": "Tests passed?", "enabled": True}
    ]
    assert status_checks.digests(["s1"])["s1"][0]["answer"] == "yes"


def test_status_checks_upgrade_sentence_only_entries_to_named_entries(home):
    rows = status_checks.set_entries([{"question": "Tests passed?"}])

    assert rows[0]["name"] == "Tests passed?"


def test_status_checks_api_reports_and_force_refresh(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            mgr.create(SessionDef(name="s1", harness="py", cwd=str(tmp_path)))
            created = await client.post(
                "/api/status-checks", json={"name": "Tests", "question": "Tests passed?"}, headers=BEARER
            )
            assert created.status == 201
            check = (await created.json())["check"]

            got = await client.get("/api/sessions/s1/status-checks", headers=BEARER)
            assert (await got.json())["checks"][0]["name"] == "Tests"
            report = await client.post(
                "/api/sessions/s1/status-checks/reports",
                json={"answers": [{"id": check["id"], "answer": "no"}]}, headers=BEARER,
            )
            assert report.status == 200
            assert (await report.json())["checks"][0]["report"]["answer"] == "no"

            refreshed = await client.post(
                "/api/sessions/s1/status-checks/refresh", headers=BEARER
            )
            assert refreshed.status == 200
            assert (await refreshed.json())["delivered"] is True
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_refresh_wakes_a_held_session_and_queues_the_request(home, tmp_path):
    """claunch-t1r9j: a held session is asleep, not gone. The checks button
    wakes it and the refresh block is typed into the woken session; a killed
    record is still refused."""
    from claude_launcher.daemon.session import DeadSession

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = await _serve(mgr)
        try:
            status_checks.set_entries([{"name": "Tests", "question": "Tests passed?"}])
            for name, held_at in (("asleep", "2026-10-13T08:00:00+00:00"), ("dead", None)):
                mgr._sessions[name] = DeadSession(
                    SessionDef(name=name, harness="claude", cwd=str(tmp_path)),
                    exit_code=0, held_at=held_at,
                )
            woken = []
            mgr.wake_soon = lambda name, **kw: woken.append((name, kw)) or True

            response = await client.post(
                "/api/sessions/asleep/status-checks/refresh", headers=BEARER
            )
            assert response.status == 200
            body = await response.json()
            assert body["delivered"] is True and body["woken"] is True
            assert [c["name"] for c in body["checks"]] == ["Tests"]
            assert woken == [("asleep", {"by": "status-checks",
                                         "deliver": status_checks.REFRESH_PROMPT})]

            response = await client.post(
                "/api/sessions/dead/status-checks/refresh", headers=BEARER
            )
            assert response.status == 409
            assert len(woken) == 1

            # Nothing configured: nothing to ask, so nothing is woken.
            status_checks.set_entries([])
            response = await client.post(
                "/api/sessions/asleep/status-checks/refresh", headers=BEARER
            )
            assert (await response.json())["delivered"] is False
            assert len(woken) == 1
        finally:
            await mgr.shutdown_all()
            await client.close()

    asyncio.run(run())


def test_status_checks_mcp_reads_and_reports_only_its_session(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(("get", path, None))
            return {"checks": []}

        def post(self, path, body):
            self.calls.append(("post", path, body))
            return {"checks": body["answers"]}

    client = Client()
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")
    monkeypatch.setattr(status_checks_mcp, "_client", lambda: client)
    assert status_checks_mcp.call_tool("status_checks", {}) == {"checks": []}
    assert status_checks_mcp.call_tool(
        "report_status_checks", {"answers": [{"id": "c1", "answer": "yes"}]}
    ) == {"checks": [{"id": "c1", "answer": "yes"}]}
    assert client.calls == [
        ("get", "/api/sessions/s1/status-checks", None),
        ("post", "/api/sessions/s1/status-checks/reports", {"answers": [{"id": "c1", "answer": "yes"}]}),
    ]
