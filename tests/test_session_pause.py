"""Session pause: the kill that files a resumable record apart from the killed.

A pause ends the program exactly as a kill does — the record reads ``exited``,
stays respawnable, and its mesh row outlives the terminal. What differs is
the ``paused_at`` marker on the record: the rail files it under *Paused*
rather than *Killed*, and the bulk resume brings back exactly the paused set.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import db, paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.session import DeadSession
from test_daemon_e2e import _manager, _register_py_harness, _wait_screen

PAUSED_AT = "2026-09-02T12:00:00+00:00"
ARCHIVED_AT = "2026-09-02T13:00:00+00:00"


def add_dead(mgr, name: str, *, paused_at=None, archived_at=None) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd="C:/work"),
        exit_code=0,
        paused_at=paused_at,
        archived_at=archived_at,
    )
    mgr._sessions[name] = record
    return record


def test_paused_marker_survives_a_restart(home):
    mgr = _manager()
    add_dead(mgr, "held", paused_at=PAUSED_AT)
    add_dead(mgr, "dead")
    mgr.persist()

    restarted = _manager()
    assert restarted.restore_all() == []
    held = restarted.get("held")
    assert held.exited and held.paused_at == PAUSED_AT
    assert restarted.get("dead").paused_at is None


def test_pause_api_partitions_the_records_and_resumes_only_the_paused(home, tmp_path):
    """The HTTP surface end to end on a rail of two live sessions and one
    killed record: pause one, pause the rest, read the partitions the rail
    filters draw, and check that each bulk verb reaches exactly its set."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}

        async def names(state: str) -> list:
            response = await client.get(
                f"/api/sessions?view=rail&state={state}", headers=headers
            )
            assert response.status == 200
            return sorted(row["name"] for row in (await response.json())["sessions"])

        try:
            for name in ("a0", "a1"):
                s = mgr.create(SessionDef(name=name, harness="py", cwd=str(tmp_path)))
                await _wait_screen(s, "READY")
            add_dead(mgr, "k0")

            # --- one session, paused alone ---------------------------------
            response = await client.post("/api/sessions/a0/pause", headers=headers)
            assert response.status == 200
            body = await response.json()
            assert body["paused_at"] and "already_exited" not in body
            await mgr.get("a0").wait_for("exited", timeout=10.0, threshold=0.5)
            assert [e["kind"] for e in mgr.events.rows(mgr.get("a0"))] == ["create", "pause", "exit"]

            # A second pause says so and keeps the marker; a pause of a
            # killed record does not turn it into a paused one.
            body = await (await client.post("/api/sessions/a0/pause", headers=headers)).json()
            assert body["already_exited"] is True and body["paused_at"]
            body = await (await client.post("/api/sessions/k0/pause", headers=headers)).json()
            assert body["already_exited"] is True and body["paused_at"] is None

            # --- the rest of the fleet, paused at once ----------------------
            body = await (await client.post("/api/sessions/pause", headers=headers)).json()
            assert body == {"paused": ["a1"], "failed": []}
            await mgr.get("a1").wait_for("exited", timeout=10.0, threshold=0.5)
            body = await (await client.post("/api/sessions/pause", headers=headers)).json()
            assert body == {"paused": [], "failed": []}

            # --- the partitions the rail draws ------------------------------
            assert await names("paused") == ["a0", "a1"]
            assert await names("killed") == ["k0"]
            assert await names("active") == []
            assert await names("current") == ["a0", "a1", "k0"]
            response = await client.get(
                "/api/sessions?view=rail&state=paused", headers=headers
            )
            rows = (await response.json())["sessions"]
            assert all(row["paused_at"] and row["status"] == "exited" for row in rows)
            response = await client.get("/api/sessions?state=halted", headers=headers)
            assert response.status == 400

            # --- the killed-only verbs leave the paused alone ---------------
            body = await (await client.post(
                "/api/sessions/archive?paused=0", headers=headers
            )).json()
            assert body == {"archived": ["k0"], "failed": []}
            body = await (await client.post(
                "/api/sessions/respawn?archived=0&paused=0", headers=headers
            )).json()
            assert body == {"respawned": [], "failed": []}
            assert await names("paused") == ["a0", "a1"]

            # --- resume: exactly the paused set, markers cleared ------------
            body = await (await client.post("/api/sessions/resume", headers=headers)).json()
            assert body == {"resumed": ["a0", "a1"], "failed": []}
            for name in ("a0", "a1"):
                revived = mgr.get(name)
                assert not revived.exited and revived.paused_at is None
                await _wait_screen(revived, "READY")
            assert await names("paused") == []
            assert await names("active") == ["a0", "a1"]
            body = await (await client.post("/api/sessions/resume", headers=headers)).json()
            assert body == {"resumed": [], "failed": []}
        finally:
            for s in list(mgr.list()):
                await s.shutdown()
            await client.close()

    asyncio.run(run())
