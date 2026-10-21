"""Watching another session's cflow run (claunch-ijg51.1).

The read side is pure (:mod:`claude_launcher.cflow_watch`); the delivery side
is the daemon board (:mod:`claude_launcher.daemon.cflow_watch`). Nothing here
touches a real daemon: the board is given a fake manager and fake sessions.
"""

from __future__ import annotations

import asyncio
import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher import cflow_watch
from claude_launcher.cflow2 import frontend, runtime, store as v2_store
from claude_launcher.daemon import cflow_watch as board_mod
from claude_launcher.daemon import loops

T0 = 1_000_000.0

CHART = """
nodes:
  a: {prompt: 'A task'}
  b: {prompt: 'B task'}
body: {seq: [a, b]}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    for name in ("CLAUNCH_SESSION", "CLAUNCH_SCRATCH", "CLAUNCH_DAEMON",
                 "CLAUNCH_INSTANCE_BASE"):
        monkeypatch.delenv(name, raising=False)


def _start(tmp_path, scope="kid"):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(CHART))
    runtime.start(chart, cwd=str(tmp_path), scope=scope, params={}, caps={}, now=T0)
    return v2_store.qualify(scope)


def _report(cwd, scope):
    st = runtime.status(cwd=str(cwd), scope=scope, tick=False)
    instance = st["tasks"][0]["instance"]
    runtime.report(cwd=str(cwd), scope=scope, node=instance, outcome="ok",
                   value={}, text="done here", now=T0 + 1)
    return instance


# -- reading --------------------------------------------------------------

def test_slot_reports_a_v2_run_with_a_cursor(tmp_path):
    scope = _start(tmp_path)
    found = cflow_watch.slot(str(tmp_path), scope)
    assert found["version"] == 2
    assert found["status"] == "running"
    assert found["run"]
    assert found["current"], "a running run names the node it is in"
    run, count = cflow_watch._parse_cursor(found["cursor"])
    assert run == found["run"] and count > 0


def test_slot_is_none_for_an_empty_slot(tmp_path):
    assert cflow_watch.slot(str(tmp_path), v2_store.qualify("kid")) is None


def test_events_since_advances_and_does_not_repeat(tmp_path):
    scope = _start(tmp_path)
    before = cflow_watch.slot(str(tmp_path), scope)["cursor"]
    _report(tmp_path, scope)

    events, cursor = cflow_watch.events_since(str(tmp_path), scope, before)
    assert [e["event"] for e in events] == ["reported"]
    assert cursor != before

    again, cursor2 = cflow_watch.events_since(str(tmp_path), scope, cursor)
    assert again == []
    assert cursor2 == cursor


def test_events_since_starts_over_for_a_cursor_from_another_run(tmp_path):
    scope = _start(tmp_path)
    _report(tmp_path, scope)
    stale = "another-run:4"
    events, cursor = cflow_watch.events_since(str(tmp_path), scope, stale)
    assert events, "a cursor from another run must not hide this run's events"
    assert cflow_watch._parse_cursor(cursor)[0] != "another-run"


def test_events_since_filters_to_the_requested_kinds(tmp_path):
    scope = _start(tmp_path)
    before = cflow_watch.slot(str(tmp_path), scope)["cursor"]
    _report(tmp_path, scope)
    events, _ = cflow_watch.events_since(str(tmp_path), scope, before, kinds=["done"])
    assert events == []


# -- the board ------------------------------------------------------------

class FakeSession:
    def __init__(self, name, cwd, exited=False):
        self.sdef = SimpleNamespace(name=name, cwd=str(cwd), parent=None)
        self.exited = exited
        self.blocks = []

    def queue_delivery(self, text):
        self.blocks.append(text)
        return True


class FakeManager:
    def __init__(self, sessions, children=None):
        self._sessions = {s.sdef.name: s for s in sessions}
        self._children = children or {}

    def get(self, name):
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())

    def descendants(self, name):
        return list(self._children.get(name, []))


def _board(tmp_path, children=None):
    boss = FakeSession("boss", tmp_path)
    kid = FakeSession("kid", tmp_path)
    other = FakeSession("other", tmp_path)
    manager = FakeManager([boss, kid, other], children=children or {"boss": ["kid"]})
    return board_mod.WatchBoard(manager, mesh=None), boss, kid


def test_subscribe_requires_subtree_or_mesh(tmp_path):
    board, boss, kid = _board(tmp_path)
    row = board.subscribe("boss", "kid")
    assert row["target"] == "kid"
    with pytest.raises(board_mod.WatchError) as caught:
        board.subscribe("boss", "other")
    assert caught.value.status == 403
    with pytest.raises(board_mod.WatchError):
        board.subscribe("boss", "boss")


def test_subscriptions_live_beside_the_loop_ledger(tmp_path):
    board, boss, kid = _board(tmp_path)
    ledger = loops.ledger_file("boss")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"loops": [{"id": "l1", "what": "wait"}]}', encoding="utf-8")

    board.subscribe("boss", "kid")
    assert [r["id"] for r in loops._load("boss")] == ["l1"]
    assert [r["target"] for r in loops.watch_rows("boss")] == ["kid"]

    assert board.unsubscribe("boss", "kid") is True
    assert loops.watch_rows("boss") == []
    assert [r["id"] for r in loops._load("boss")] == ["l1"]
    assert board.unsubscribe("boss", "kid") is False


def test_tick_delivers_once_and_advances_the_cursor(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid = _board(tmp_path)
    board.subscribe("boss", "kid")
    before = [r for r in board.subscriptions("boss")][0]["cursor"]
    assert cflow_watch._parse_cursor(before)[0] == cflow_watch.slot(str(tmp_path), scope)["run"]

    _report(tmp_path, scope)
    asyncio.run(board.tick())
    assert len(boss.blocks) == 1
    block = boss.blocks[0]
    assert "machine-generated" in block
    assert "kid" in block and "reported" in block
    after = board.subscriptions("boss")[0]["cursor"]
    assert after != before

    asyncio.run(board.tick())
    assert len(boss.blocks) == 1, "nothing moved, so nothing is typed again"


def test_a_refused_delivery_keeps_the_cursor(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid = _board(tmp_path)
    board.subscribe("boss", "kid")
    before = board.subscriptions("boss")[0]["cursor"]
    _report(tmp_path, scope)

    boss.queue_delivery = lambda text: False  # type: ignore[assignment]
    asyncio.run(board.tick())
    assert board.subscriptions("boss")[0]["cursor"] == before

    boss.queue_delivery = lambda text: boss.blocks.append(text) or True  # type: ignore[assignment]
    asyncio.run(board.tick())
    assert len(boss.blocks) == 1
    assert board.subscriptions("boss")[0]["cursor"] != before


def test_runs_refuses_what_it_may_not_read(tmp_path):
    _start(tmp_path)
    board, boss, kid = _board(tmp_path)
    result = board.runs("boss", ["kid", "other"])
    assert [row["session"] for row in result["runs"]] == ["kid"]
    assert result["refused"][0]["session"] == "other"


def test_the_routes(tmp_path):
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import SessionManager

    _start(tmp_path)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        board, _, _ = _board(tmp_path)
        app["cflow_watch"] = board
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            sub = await (await client.post(
                "/api/cflow-watch/subscribe", headers=bearer,
                json={"session": "boss", "target": "kid"})).json()
            listed = await (await client.get(
                "/api/cflow-watch/subscriptions?session=boss", headers=bearer)).json()
            runs = await (await client.get(
                "/api/cflow-watch/runs?session=boss&targets=kid", headers=bearer)).json()
            refused = await client.post(
                "/api/cflow-watch/subscribe", headers=bearer,
                json={"session": "boss", "target": "other"})
            unauth = await client.get("/api/cflow-watch/subscriptions?session=boss")
            return sub, listed, runs, refused.status, unauth.status
        finally:
            await client.close()

    sub, listed, runs, refused, unauth = asyncio.run(run())
    assert sub["subscription"]["target"] == "kid"
    assert [r["target"] for r in listed["subscriptions"]] == ["kid"]
    assert runs["runs"][0]["version"] == 2
    assert refused == 403 and unauth == 401
