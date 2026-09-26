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
from claude_launcher.daemon.mesh import Member, MeshManager
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


class FakeLive(DeadSession):
    """A record that reads as running until its shutdown lands.

    Built on DeadSession so the registry can persist it like any other
    record; what it adds is the one thing under test — a shutdown that ends
    asynchronously, which is why an archive cannot simply follow a kill.
    """

    def __init__(self, sdef) -> None:
        super().__init__(sdef, exit_code=None)
        self.exited = False
        self.graces: list = []

    def status(self, threshold=None) -> str:
        return "exited" if self.exited else "idle"

    def kill(self, *, force: bool = False) -> None:
        # What the real Session.kill does: signal, and leave `exited` to the
        # reader task. An archive pressed straight after one finds it running.
        self.graces.append(("kill", force))

    async def shutdown(self, grace: float = 5.0) -> None:
        self.graces.append(("shutdown", grace))
        self.exited = True
        self.exit_code = 0


def add_live(mgr: SessionManager, name: str) -> FakeLive:
    record = FakeLive(SessionDef(name=name, harness="claude", cwd="C:/work"))
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


def test_a_swept_ending_is_not_swept_again_at_boot(home):
    # claunch-fh8u1.2. Every boot used to sweep every exited record again,
    # and the one sweep write that changes no state -- the ORPHANED
    # FOLLOW-UP comment -- piled up once per restart (29 copies on one
    # issue). The sweep stamps ``swept_at``, persist carries it, and the
    # stamped record is not owed a sweep any more.
    mgr = manager()
    add_dead(mgr, "fresh")
    add_dead(mgr, "swept").swept_at = "2026-09-24T07:51:00+00:00"
    mgr.persist()

    restarted = manager()
    assert restarted.restore_all() == []
    assert restarted.get("swept").swept_at == "2026-09-24T07:51:00+00:00"
    swept = {d.sdef.name for d in restarted.take_retired_for_sweep()}
    assert swept == {"fresh"}


def test_archive_alone_refuses_a_running_session_and_names_the_verb(home):
    # The filing half on its own still refuses: a record written as archived
    # while its program runs would say something that is not true yet. The
    # refusal names stop_and_archive, which is what every operator route
    # calls — the refusal is only reachable from the bulk "archive the exited
    # ones" pass, where selecting nothing live is the point.
    mgr = manager()
    mgr._sessions["live"] = SimpleNamespace(
        sdef=SessionDef(name="live", harness="claude", cwd="C:/work"),
        exited=False,
    )
    with pytest.raises(ManagerError, match="stop_and_archive"):
        mgr.archive("live")


@pytest.mark.parametrize("force, grace", [(False, 5.0), (True, 0.0)])
def test_stop_and_archive_ends_a_running_session_in_the_same_call(
    home, force, grace
):
    # The point of the verb: archive is available while a session runs, and
    # the record it leaves is both ended and filed. The stop is shutdown()
    # rather than kill() because kill only signals — the exit lands later,
    # and an archive written in between would be refused.
    mgr = manager()
    live = add_live(mgr, "busy")

    archived = asyncio.run(mgr.stop_and_archive("busy", force=force))

    assert live.exited
    assert live.graces == [("shutdown", grace)]
    assert archived.archived_at
    assert archived is live

    saved = {row["def"]["name"]: row for row in db.open_default().load_all()}
    assert saved["busy"]["archived_at"] == live.archived_at
    assert saved["busy"]["was_running"] is False

    kinds = [row["kind"] for row in mgr.events.rows(live)]
    assert kinds == ["kill", "archive"]


def test_stop_and_archive_on_an_exited_record_is_plain_archive(home):
    # Nothing to end, so nothing is ended: the verb is the always-available
    # entry point, not a second way to kill something.
    mgr = manager()
    dead = add_dead(mgr, "old")

    archived = asyncio.run(mgr.stop_and_archive("old"))
    assert archived.archived_at
    assert [row["kind"] for row in mgr.events.rows(dead)] == ["archive"]

    first = archived.archived_at
    assert asyncio.run(mgr.stop_and_archive("old")).archived_at == first


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_join_briefing_excludes_archived_sessions(home, connected, restart):
    mgr = manager()
    add_dead(mgr, "old")
    mgr.archive("old")
    if restart:
        mgr = manager()
        mgr.restore_all()
    assert mgr.get("old").archived_at

    mm = MeshManager(mgr)
    mm.create("team")
    mesh = mm.get("team")
    me = Member("me", "self", role="worker", wired=True)
    mesh.members = {"me": me, "archived-peer": Member("archived-peer", "old")}
    mesh.member_edges[mesh.member_key("me", "archived-peer")] = connected

    block = mm.briefing_block(mesh, me)
    assert "members: (nobody else yet)\n" in block
    assert "archived-peer" not in block
    assert "other member(s)" not in block
    assert "archived-peer" in mesh.members


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


def test_archive_route_stops_a_running_session_and_files_it_in_one_call(home):
    # The operator-facing half of the same decision: the route answers for a
    # live session too, and the reply distinguishes the call that ended one
    # from the call that filed a record already dead. The bulk route keeps
    # its own meaning — it is labelled "archive the exited ones" and skips
    # the live session rather than ending it.
    async def run():
        mgr = manager()
        live = add_live(mgr, "busy")
        add_dead(mgr, "old")
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer sekrit"}
        try:
            # A wind-down standing for this session is dropped, not awaited:
            # archiving leaves the session no turn to settle anything in.
            app["beads"].winddowns["busy"] = object()

            response = await client.post("/api/sessions/busy/archive", headers=headers)
            assert response.status == 200
            body = await response.json()
            assert body["stopped"] is True
            assert body["archived_at"]
            assert live.exited and live.graces == [("shutdown", 5.0)]
            assert "busy" not in app["beads"].winddowns

            # An already-exited record is filed without claiming to have
            # stopped anything.
            response = await client.post("/api/sessions/old/archive", headers=headers)
            body = await response.json()
            assert body["archived_at"] and "stopped" not in body
        finally:
            await client.close()

    asyncio.run(run())


def test_bulk_archive_leaves_a_running_session_alone(home):
    async def run():
        mgr = manager()
        live = add_live(mgr, "busy")
        add_dead(mgr, "old")
        app = build_app(mgr, "sekrit", started_at=0.0)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/sessions/archive", headers={"Authorization": "Bearer sekrit"}
            )
            assert (await response.json()) == {"archived": ["old"], "failed": []}
            assert not live.exited and live.archived_at is None
        finally:
            await client.close()

    asyncio.run(run())
