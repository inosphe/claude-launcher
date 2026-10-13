"""A session's temporary instructions (claunch-ozyje).

A person writes them from the detail panel for one session; every session
reminder the daemon delivers into that session carries them as its last
section until they are cleared. They start no reminder of their own.
"""

from __future__ import annotations

import asyncio
import time

from claude_launcher import profile
from claude_launcher.daemon import session_reminder
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import MAX_NOTE, SessionManager

from test_reminder_clock import (  # noqa: F401 - proj is a fixture
    BEARER,
    _FakeMesh,
    _FakeMeshMgr,
    _FakeSession,
    _KinManager,
    proj,
)


def test_the_section_rides_last_and_keeps_its_lines():
    block = session_reminder.reminder_block(
        "w1",
        roles=[],
        situation=["-- around you right now --", "children: c1"],
        instructions="  always run the targeted tests\nnever push  ",
    )
    rows = block.splitlines()
    assert "## Temporary instructions" in rows
    head = rows.index("## Temporary instructions")
    assert head > rows.index("## Situation")
    assert rows[-3:] == ["always run the targeted tests", "never push", "---"]


def test_no_instructions_no_section():
    for empty in ("", "   \n  "):
        block = session_reminder.reminder_block("w1", roles=[], instructions=empty)
        assert "Temporary instructions" not in block


def test_the_definition_keeps_it_and_writes_no_key_without_it():
    bare = SessionDef(name="w1", cwd="/x")
    assert "reminder_note" not in bare.to_dict()
    noted = SessionDef(name="w1", cwd="/x", reminder_note="keep the tests green")
    data = noted.to_dict()
    assert data["reminder_note"] == "keep the tests green"
    assert SessionDef.from_dict(data).reminder_note == "keep the tests green"
    assert SessionDef.from_dict({**data, "reminder_note": "  "}).reminder_note is None


def test_every_delivered_reminder_carries_it_and_a_clear_stops_it(proj):
    sess = _FakeSession("w1", str(proj))
    sess.sdef = SessionDef(
        name="w1", cwd=str(proj), reminder_note="answer in Korean"
    )
    mgr = _KinManager({"w1": sess})
    service = session_reminder.SessionReminderService(
        mgr, _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    )

    asyncio.run(service.tick(1000.0))       # first sight arms the role source
    assert sess.delivered == []             # the instructions start nothing
    asyncio.run(service.tick(1601.0))
    assert len(sess.delivered) == 1
    first = sess.delivered[0]
    assert first.index("## Role") < first.index("## Temporary instructions")
    assert "answer in Korean" in first

    sess.sdef = SessionDef(name="w1", cwd=str(proj))
    sess.activity = "moved"                 # a working session earns the repeat
    service._roles["w1"]["at"] = 0.0
    asyncio.run(service.tick(2202.0))
    assert len(sess.delivered) == 2
    assert "Temporary instructions" not in sess.delivered[1]


def test_the_door_sets_trims_lists_and_clears(proj):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    profile.create("work")

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        session = mgr.stage(SessionDef(name="w1", profile="work", cwd=str(proj)))
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        url = "/api/sessions/w1/reminder/note"
        try:
            resp = await client.post(
                url, headers=BEARER, json={"note": "  run sweep before landing \n"}
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["reminder_note"] == "run sweep before landing"
            assert session.sdef.reminder_note == "run sweep before landing"
            # the user's own note is a different field and stays untouched
            assert session.sdef.note is None

            listed = await (await client.get("/api/sessions", headers=BEARER)).json()
            row = next(s for s in listed["sessions"] if s["name"] == "w1")
            assert row["reminder_note"] == "run sweep before landing"

            resp = await client.post(url, headers=BEARER, json={"note": 3})
            assert resp.status == 400
            resp = await client.post(
                url, headers=BEARER, json={"note": "x" * (MAX_NOTE + 1)}
            )
            assert resp.status == 400
            assert session.sdef.reminder_note == "run sweep before landing"

            resp = await client.post(url, headers=BEARER, json={"note": None})
            body = await resp.json()
            assert resp.status == 200, body
            assert "reminder_note" not in body
            assert session.sdef.reminder_note is None
        finally:
            await client.close()
            mgr.discard("w1")

    asyncio.run(scenario())
