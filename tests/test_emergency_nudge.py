"""emergency_nudge: a card for a run that waits on no person (claunch-8myyt).

``flow_nudge`` is refused unless the run's files say it waits on a person; an
auto-mode classifier denial stops the agent inside a step and never shows
there. The emergency card is the same dashboard card without that check.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import cli_nudge, daemon_client, emergency_nudge_mcp, mcp_server
from claude_launcher.daemon import api as api_mod, flow_nudge
from claude_launcher.daemon.manager import SessionManager


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _board(clock=None, where=None, alive=None):
    where = where if where is not None else {}
    return flow_nudge.NudgeBoard(
        read_owed=lambda cwd, scope: {"version": 2, "run": "r1", "workflow": "w", "items": []},
        read_position=lambda cwd, scope: where.get((cwd, scope)),
        clock=clock or Clock(), alive=alive), where


def _call(board, message="classifier denied the merge; approve? (recommend yes)", **kw):
    return board.emergency(session="s1", cwd="/p", scope="s1", message=message, **kw)


def test_it_raises_for_a_run_that_waits_on_no_person():
    board, _ = _board()
    with pytest.raises(flow_nudge.NudgeRefused, match="waits on no person"):
        board.submit(session="s1", cwd="/p", scope="s1", message="m")
    got = _call(board)
    assert got["raised"] is True
    assert got["request"]["items"] == [flow_nudge.EMERGENCY]
    assert got["request"]["kind"] == "emergency"
    assert [r["status"] for r in board.list()] == ["pending"]


def test_the_same_key_updates_one_record_and_adds_no_history_row():
    clock = Clock()
    board, _ = _board(clock)
    first = _call(board, "first")
    clock.now += flow_nudge.EMERGENCY_MIN_INTERVAL + 1
    again = _call(board, "second")
    assert again["raised"] is False and again["request"]["id"] == first["request"]["id"]
    rows = board.list()
    assert len(rows) == 1
    assert rows[0]["message"] == "second" and rows[0]["updated"] > rows[0]["opened"]
    assert rows[0]["asked"] == 2
    # a later call after twenty minutes is still the same record
    clock.now += 1200
    assert _call(board, "third")["request"]["id"] == first["request"]["id"]
    assert len(board.list()) == 1


def test_a_session_is_limited_to_one_call_a_minute_but_may_withdraw():
    clock = Clock()
    board, _ = _board(clock)
    _call(board)
    clock.now += 10
    with pytest.raises(flow_nudge.NudgeRefused) as exc:
        _call(board, "again")
    assert exc.value.status == 429
    gone = _call(board, cancel=True)           # a withdrawal is not limited
    assert gone["request"]["status"] == "settled"
    assert board.list()[0]["status"] == "settled"
    clock.now += flow_nudge.EMERGENCY_MIN_INTERVAL
    assert _call(board, "new")["raised"] is True


def test_it_is_apart_from_the_flow_nudge_card_and_needs_a_message():
    board, _ = _board()
    board._read_owed = lambda cwd, scope: {"version": 2, "run": "r1", "workflow": "w",
                                           "items": ["choose: x"]}
    flow = board.submit(session="s1", cwd="/p", scope="s1", message="pick")
    emergency = _call(board)
    assert flow["request"]["id"] != emergency["request"]["id"]
    assert {r["status"] for r in board.list()} == {"pending"}
    with pytest.raises(flow_nudge.NudgeRefused) as exc:
        _call(board, "  ")
    assert exc.value.status == 400


def test_it_settles_when_the_run_moves_a_person_resolves_or_the_session_ends():
    where = {("/p", "s1"): ("v2", "r1", "running", ("a@1",), (), ()),
             ("/p", "s2"): ("v2", "r1", "running", ("a@1",), (), ()),
             ("/p", "s3"): None}
    alive = {"s1": True, "s2": True, "s3": True, "s4": True}
    board, _ = _board(where=where, alive=lambda name: alive[name])
    ids = {}
    for name in ("s1", "s2", "s3", "s4"):
        ids[name] = board.emergency(session=name, cwd="/p", scope=name,
                                    message="m")["request"]["id"]
    assert {r["status"] for r in board.list()} == {"pending"}

    where[("/p", "s1")] = ("v2", "r1", "running", ("b@2",), (), ())   # the run moved
    board.resolve(ids["s2"])                                          # a person answered
    alive["s4"] = False                                               # the driver went away
    got = {r["session"]: r["status"] for r in board.list()}
    assert got == {"s1": "settled", "s2": "settled", "s3": "pending", "s4": "session ended"}
    with pytest.raises(flow_nudge.NudgeRefused) as exc:
        board.resolve(ids["s2"])
    assert exc.value.status == 409
    with pytest.raises(flow_nudge.NudgeRefused) as exc:
        board.resolve("nudge-nope")
    assert exc.value.status == 404


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.posts = []

    def post(self, path, body=None, **kw):
        self.posts.append((path, body))
        return self.answer


def test_the_mcp_tool_is_registered_and_posts_an_emergency(monkeypatch, tmp_path):
    assert "emergency_nudge" in {t["name"] for t in mcp_server.TOOLS}
    client = FakeClient({"raised": True, "note": "raised",
                         "request": {"id": "nudge-1", "items": ["emergency"]}})
    monkeypatch.setattr(daemon_client, "connect_with_diagnosis", lambda **kw: (client, {}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    out = emergency_nudge_mcp.call_tool("emergency_nudge", {"message": "approve the merge?"})
    assert out == {"raised": True, "request": "nudge-1", "owed": ["emergency"], "note": "raised"}
    path, body = client.posts[0]
    assert path == "/api/flow-nudges"
    assert body == {"session": "s7", "cwd": str(tmp_path), "scope": "s7",
                    "message": "approve the merge?", "emergency": True}
    emergency_nudge_mcp.call_tool("emergency_nudge", {"cancel": True})
    assert client.posts[1][1]["cancel"] is True
    with pytest.raises(emergency_nudge_mcp.EmergencyNudgeError, match="message"):
        emergency_nudge_mcp.call_tool("emergency_nudge", {})
    monkeypatch.delenv("CLAUNCH_SESSION")
    with pytest.raises(emergency_nudge_mcp.EmergencyNudgeError, match="CLAUNCH_SESSION"):
        emergency_nudge_mcp.call_tool("emergency_nudge", {"message": "m"})


def test_the_cli_posts_and_refuses_resolve_inside_a_session(monkeypatch, capsys):
    from types import SimpleNamespace

    client = FakeClient({"note": "raised"})
    monkeypatch.setattr(daemon_client, "connect_with_diagnosis", lambda **kw: (client, {}))
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    assert cli_nudge._cmd_emergency(SimpleNamespace(message=["stuck,", "approve?"],
                                                    cancel=False)) == 0
    assert client.posts[0][1]["message"] == "stuck, approve?"
    assert client.posts[0][1]["emergency"] is True
    assert cli_nudge._cmd_emergency(SimpleNamespace(message=[], cancel=False)) == 1
    assert cli_nudge._cmd_resolve(SimpleNamespace(id="nudge-1")) == 1
    assert "managed session" in capsys.readouterr().err
    assert len(client.posts) == 1


def test_the_routes_file_and_resolve(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        board, _ = _board()
        app["flow_nudge"] = board
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            bad = await client.post("/api/flow-nudges", headers=bearer,
                                    json={"session": "ghost", "message": "m", "emergency": True})
            missing = await client.post("/api/flow-nudges/nudge-0/resolve", headers=bearer)
            return bad.status, missing.status
        finally:
            await client.close()

    bad, missing = asyncio.run(run())
    assert bad == 404 and missing == 404


def test_resolve_is_a_persons_answer_an_agent_actor_is_refused(home):
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        board, _ = _board()
        app["flow_nudge"] = board
        rec_id = _call(board)["request"]["id"]
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        agent = {**bearer, "X-Claunch-Actor": "agent:s1"}
        try:
            refused = await client.post(f"/api/flow-nudges/{rec_id}/resolve", headers=agent)
            still = [r["status"] for r in board.list()]
            ok = await client.post(f"/api/flow-nudges/{rec_id}/resolve", headers=bearer)
            return refused.status, still, ok.status, [r["status"] for r in board.list()]
        finally:
            await client.close()

    refused, still, ok, after = asyncio.run(run())
    assert refused == 403 and still == ["pending"]
    assert ok == 200 and after == ["settled"]
