"""Retained session archive: lifecycle, persistence and HTTP surface."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import db, paths
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import ManagerError, SessionManager
from claude_launcher.daemon.session import DeadSession


def manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def add_dead(mgr: SessionManager, name: str, *, archived_at=None) -> DeadSession:
    record = DeadSession(
        SessionDef(name=name, harness="claude", cwd="C:/work"),
        exit_code=0,
        archived_at=archived_at,
    )
    mgr._sessions[name] = record
    return record


def test_archive_retains_the_record_across_restart_and_respawn_clears_it(
    home, monkeypatch
):
    mgr = manager()
    add_dead(mgr, "old")

    archived = mgr.archive("old")
    assert archived.archived_at
    assert archived.info()["archived_at"] == archived.archived_at
    first = archived.archived_at
    assert mgr.archive("old").archived_at == first  # idempotent

    saved = db.open_default().load_all()
    assert saved[0]["archived_at"] == first
    assert saved[0]["def"]["name"] == "old"

    restarted = manager()
    assert restarted.restore_all() == []
    record = restarted.get("old")
    assert record.exited and record.archived_at == first

    def fake_create(sdef, **kwargs):
        live = SimpleNamespace(sdef=sdef, exited=False, archived_at=None)
        restarted._sessions[sdef.name] = live
        return live

    monkeypatch.setattr(restarted, "create", fake_create)
    live = restarted.respawn("old")
    assert live.archived_at is None


def test_archived_records_are_not_swept_at_boot(home):
    # A restart retires every exited record it does not relaunch and owes each
    # one a board sweep — the release of any issue still claiming a dead
    # session as its worker. An archived record is exempt: it was filed away
    # by the operator and swept when it first exited, so re-sweeping it on
    # every boot is churn (and, at archive scale, a slow boot for nothing).
    mgr = manager()
    add_dead(mgr, "killed")                                   # plain exited
    add_dead(mgr, "filed", archived_at="2026-08-27T00:00:00+00:00")
    mgr.persist()

    restarted = manager()
    assert restarted.restore_all() == []
    swept = {d.sdef.name for d in restarted.take_retired_for_sweep()}
    assert swept == {"killed"}


def test_archive_refuses_a_running_session(home):
    mgr = manager()
    mgr._sessions["live"] = SimpleNamespace(
        sdef=SessionDef(name="live", harness="claude", cwd="C:/work"),
        exited=False,
    )
    with pytest.raises(ManagerError, match="kill it before archiving"):
        mgr.archive("live")


def test_archive_api_handles_one_or_every_unarchived_exited_record(home):
    async def run():
        mgr = manager()
        add_dead(mgr, "one")
        add_dead(mgr, "two")
        add_dead(mgr, "already", archived_at="2026-08-27T00:00:00+00:00")
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}
        try:
            response = await client.post("/api/sessions/one/archive", headers=headers)
            assert response.status == 200
            assert (await response.json())["archived_at"]

            response = await client.post("/api/sessions/archive", headers=headers)
            assert response.status == 200
            body = await response.json()
            assert body == {"archived": ["two"], "failed": []}

            response = await client.get("/api/sessions", headers=headers)
            rows = {row["name"]: row for row in (await response.json())["sessions"]}
            assert all(rows[name]["archived_at"] for name in ("one", "two", "already"))
        finally:
            await client.close()

    asyncio.run(run())
