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

GATE_CHART = """
nodes:
  a: {prompt: 'A task'}
body:
  seq:
    - choose:
        prompt: which way?
        chooser: user
        options:
          left: {then: a}
"""

LONG_CHART = """
nodes:
  n1: {prompt: 'one'}
  n2: {prompt: 'two'}
  n3: {prompt: 'three'}
  n4: {prompt: 'four'}
  n5: {prompt: 'five'}
  n6: {prompt: 'six'}
body: {seq: [n1, n2, n3, n4, n5, n6]}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    for name in ("CLAUNCH_SESSION", "CLAUNCH_SCRATCH", "CLAUNCH_DAEMON",
                 "CLAUNCH_INSTANCE_BASE"):
        monkeypatch.delenv(name, raising=False)


def _start(tmp_path, scope="kid", chart_text=None):
    chart = frontend.compile_text("cflow: 2\nname: t\n"
                                  + textwrap.dedent(chart_text or CHART))
    runtime.start(chart, cwd=str(tmp_path), scope=scope, params={}, caps={}, now=T0)
    return v2_store.qualify(scope)


def _report(cwd, scope, text="done here"):
    st = runtime.status(cwd=str(cwd), scope=scope, tick=False)
    instance = st["tasks"][0]["instance"]
    runtime.report(cwd=str(cwd), scope=scope, node=instance, outcome="ok",
                   value={}, text=text, now=T0 + 1)
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


def test_a_gate_is_delivered_by_default_and_a_pacing_wait_is_not(tmp_path):
    """v2 emits one ``waiting`` event; only choose/ask are gates."""
    scope = _start(tmp_path, chart_text=GATE_CHART)
    found = cflow_watch.reading(str(tmp_path), scope, "")
    names = [(e["event"], e["detail"]) for e in found["events"]]
    assert any(name == "waiting" and "kind=choose" in detail for name, detail in names), names
    assert found["owed"], "a user-chooser gate is owed to a person"

    # An explicit request for ``waiting`` hears every pacing pause too.
    explicit = cflow_watch.reading(str(tmp_path), scope, "", kinds=["waiting"])
    assert explicit["events"], explicit


def _hold(scope, cwd, *approvals):
    """Write approvals onto a started run (the shape ``store``/``runtime`` hold)."""
    st = v2_store.RunStore(str(cwd), scope)
    with st.lock():
        state = st.load_state()
        state["approvals"] = list(approvals)
        st.commit(state, [])
    return st.load_state().get("approvals") or []


def test_a_storage_recovery_hold_is_not_owed_to_a_person(tmp_path):
    """A run the store took back from corruption holds itself; that is not a gate.

    The hold kind is written as ``monitor_violation`` with
    ``monitor="storage-recovery"`` (``store.RunStore._recover``,
    ``store.RECOVERY_MONITOR``). The owed list must skip exactly that one and
    still carry the others, and reading the slot must not raise: the constant
    lives in ``cflow2.store``, not in ``cflow2.runtime``.
    """
    scope = _start(tmp_path)
    written = _hold(scope, tmp_path,
                    {"kind": "monitor_violation", "monitor": "storage-recovery",
                     "instance": None},
                    # Another halted monitor is a person's to release, so it stays.
                    {"kind": "monitor_violation", "monitor": "gate-budget",
                     "instance": None},
                    {"kind": "loop_limit", "instance": "i1", "node": "a"})
    assert [a["kind"] for a in written] == ["monitor_violation", "monitor_violation",
                                            "loop_limit"]

    found = cflow_watch.slot(str(tmp_path), scope)
    assert found is not None
    assert found["owed"] == ["monitor_violation:", "loop_limit: a"], found["owed"]


def test_the_reading_token_changes_only_when_the_run_moves(tmp_path):
    scope = _start(tmp_path)
    first = cflow_watch.token(str(tmp_path), scope)
    assert first == cflow_watch.token(str(tmp_path), scope)
    _report(tmp_path, scope)
    assert cflow_watch.token(str(tmp_path), scope) != first


# -- the board ------------------------------------------------------------

class FakeSession:
    def __init__(self, name, cwd, exited=False, accept=True, delivered=True):
        self.sdef = SimpleNamespace(name=name, cwd=str(cwd), parent=None)
        self.exited = exited
        self.accept = accept
        self.delivered = delivered
        self.blocks = []

    def queue_delivery(self, text, on_done=None):
        if not self.accept:
            return False
        self.blocks.append(text)
        if on_done is not None:
            on_done(self.delivered)
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


def _board(tmp_path, extra=(), children=None):
    boss = FakeSession("boss", tmp_path)
    kid = FakeSession("kid", tmp_path)
    other = FakeSession("other", tmp_path)
    manager = FakeManager([boss, kid, other, *extra],
                          children=children or {"boss": ["kid"]})
    return board_mod.WatchBoard(manager, mesh=None), boss, kid, manager


def _tick(board):
    asyncio.run(board.tick())


def test_subscribe_requires_subtree_or_mesh(tmp_path):
    board, boss, kid, _ = _board(tmp_path)
    row = board.subscribe("boss", "kid")
    assert row["target"] == "kid"
    with pytest.raises(board_mod.WatchError) as caught:
        board.subscribe("boss", "other")
    assert caught.value.status == 403
    with pytest.raises(board_mod.WatchError):
        board.subscribe("boss", "boss")


def test_subscriptions_live_beside_the_loop_ledger(tmp_path):
    board, boss, kid, _ = _board(tmp_path)
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
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    before = [r for r in board.subscriptions("boss")][0]["cursor"]
    assert cflow_watch._parse_cursor(before)[0] == cflow_watch.slot(str(tmp_path), scope)["run"]

    _report(tmp_path, scope)
    _tick(board)
    assert len(boss.blocks) == 1
    block = boss.blocks[0]
    assert "machine-generated" in block
    assert "kid" in block and "reported" in block
    after = board.subscriptions("boss")[0]["cursor"]
    assert after != before

    _tick(board)
    assert len(boss.blocks) == 1, "nothing moved, so nothing is typed again"


def test_a_refused_delivery_keeps_the_cursor(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    before = board.subscriptions("boss")[0]["cursor"]
    _report(tmp_path, scope)

    boss.accept = False
    _tick(board)
    assert board.subscriptions("boss")[0]["cursor"] == before

    boss.accept = True
    _tick(board)
    assert len(boss.blocks) == 1
    assert board.subscriptions("boss")[0]["cursor"] != before


def test_an_accepted_but_undelivered_block_keeps_the_cursor(tmp_path):
    """Acceptance is not a delivery receipt (daemon/session.queue_delivery)."""
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    before = board.subscriptions("boss")[0]["cursor"]
    _report(tmp_path, scope)

    boss.delivered = False
    _tick(board)
    assert board.subscriptions("boss")[0]["cursor"] == before
    _tick(board)  # the next tick retries the same block
    assert len(boss.blocks) == 2
    assert board.subscriptions("boss")[0]["cursor"] == before

    boss.delivered = True
    _tick(board)
    assert board.subscriptions("boss")[0]["cursor"] != before
    _tick(board)
    assert len(boss.blocks) == 3, "the delivered block is not typed again"


def test_a_filtered_tick_still_advances_the_cursor(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid", kinds=["done"])
    before = board.subscriptions("boss")[0]["cursor"]
    _report(tmp_path, scope)

    _tick(board)
    assert boss.blocks == [], "a report is outside kinds=['done']"
    after = board.subscriptions("boss")[0]["cursor"]
    assert cflow_watch._parse_cursor(after)[1] > cflow_watch._parse_cursor(before)[1]

    _tick(board)
    assert board.subscriptions("boss")[0]["cursor"] == after, "the cursor is not re-read backwards"


def test_a_truncated_batch_leaves_the_rest_for_the_next_tick(tmp_path, monkeypatch):
    scope = _start(tmp_path, chart_text=LONG_CHART)
    monkeypatch.setattr(cflow_watch, "BATCH_LIMIT", 2)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    for i in range(5):
        _report(tmp_path, scope, text=f"r{i}")

    _tick(board)
    assert len(boss.blocks) == 1
    assert "more events pending" in boss.blocks[0]
    assert boss.blocks[0].count("reported") == 2

    _tick(board)
    assert len(boss.blocks) == 2
    assert boss.blocks[1].count("reported") == 2

    _tick(board)
    assert boss.blocks[2].count("reported") == 1


def test_a_run_end_is_announced_once(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    _report(tmp_path, scope)          # a
    _report(tmp_path, scope)          # b -> run done
    _tick(board)
    assert any("run ended" in line for line in boss.blocks[0].splitlines())

    _tick(board)
    assert len(boss.blocks) == 1, "an ended run is announced once"


def test_a_vanished_target_is_reported_once(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, manager = _board(tmp_path)
    board.subscribe("boss", "kid")
    manager._sessions.pop("kid")

    _tick(board)
    assert "is gone" in boss.blocks[0]
    assert board.subscriptions("boss")[0]["missing"] is True

    _tick(board)
    assert len(boss.blocks) == 1, "the disappearance is not repeated every tick"


def test_a_session_that_exited_is_announced_once(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    board.subscribe("boss", "kid")
    kid.exited = True

    _tick(board)
    assert "session exited" in boss.blocks[0]

    _tick(board)
    assert len(boss.blocks) == 1, "the exit is announced once"


def test_backfill_reads_what_the_next_delivery_carries(tmp_path):
    scope = _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
    row = board.subscribe("boss", "kid")
    _report(tmp_path, scope)

    runs = board.runs("boss", ["kid"], since=row["cursor"])
    assert [e["event"] for e in runs["runs"][0]["events"]] == ["reported"]
    assert runs["runs"][0]["cursor"] != row["cursor"]

    listed = board.subscriptions("boss", backfill=True)
    assert [e["event"] for e in listed[0]["events"]] == ["reported"]
    assert board.subscriptions("boss")[0]["cursor"] == row["cursor"], "backfill is a read"

    _tick(board)
    assert "reported" in boss.blocks[0]


def test_the_subscription_cap_is_enforced(tmp_path):
    extra = [FakeSession(f"kid{i}", tmp_path) for i in range(board_mod.MAX_SUBSCRIPTIONS)]
    board, boss, kid, manager = _board(tmp_path, extra=extra)
    manager._children["boss"] = ["kid"] + [s.sdef.name for s in extra]
    for i in range(board_mod.MAX_SUBSCRIPTIONS):
        board.subscribe("boss", f"kid{i}")
    with pytest.raises(board_mod.WatchError) as caught:
        board.subscribe("boss", "kid")
    assert caught.value.status == 409


def test_runs_refuses_what_it_may_not_read(tmp_path):
    _start(tmp_path)
    board, boss, kid, _ = _board(tmp_path)
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
        board, _, _, _ = _board(tmp_path)
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
            cursor = sub["subscription"]["cursor"]
            runs = await (await client.get(
                "/api/cflow-watch/runs?session=boss&targets=kid"
                f"&since={cursor}", headers=bearer)).json()
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
    assert "events" in runs["runs"][0]
    assert refused == 403 and unauth == 401


#: A harness whose command exists on this machine, so ``stage`` accepts the
#: definition without a profile and without launching anything.
def _register_py_harness():
    import sys

    from claude_launcher import store

    store.update(lambda doc: doc.update({
        "harnesses": {"py": {"command": [sys.executable, "-u", "-c",
                                         "import time; time.sleep(60)"]}}
    }))


def test_the_card_and_the_panel_read_the_same_subscriptions(home, tmp_path):
    """The rail row and the detail panel both say what a session watches.

    The record is the watcher's own ledger; what differs is the read — the
    card rides the /api/sessions poll every rail load makes, the panel has
    its own /meta request. Both have to carry the same rows, or the two
    places disagree about a fact one of them is believed for (the bug the
    context reading had, tests/test_ctxsize.py). The cursor stays out: it is
    the next delivery's bookkeeping, not something a reader acts on.
    """
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    since = "2026-10-21T11:02:39+00:00"
    loops.save_watch("boss", [{
        "target": "kid",
        "kinds": ["reported", "done"],
        "cursor": "20261021-110239-58e6b1:7",
        "since": since,
    }])

    async def run():
        _register_py_harness()
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic(),
                                mesh=MeshManager(mgr))
        mgr.stage(SessionDef(name="boss", harness="py", cwd=str(tmp_path)))
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            rail = await (await client.get(
                "/api/sessions?view=rail", headers=bearer)).json()
            meta = await (await client.get(
                "/api/sessions/boss/meta", headers=bearer)).json()
            return rail["sessions"][0], meta
        finally:
            await client.close()
            await mgr.shutdown_all()

    row, meta = asyncio.run(run())
    assert row["watch"] == [{"target": "kid", "kinds": ["reported", "done"],
                             "since": since}]
    assert "cursor" not in row["watch"][0]
    assert meta["watch"] == row["watch"], "the panel and the card agree"


def test_a_session_with_no_subscriptions_carries_an_empty_list(home, tmp_path):
    """Empty, not absent: the page has to tell "watches nothing" from "this
    daemon does not send the field at all" — the second is what a page older
    than the field draws a stale card from."""
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.harness import SessionDef
    from claude_launcher.daemon.manager import SessionManager
    from claude_launcher.daemon.mesh import MeshManager

    async def run():
        _register_py_harness()
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic(),
                                mesh=MeshManager(mgr))
        mgr.stage(SessionDef(name="quiet", harness="py", cwd=str(tmp_path)))
        client = TestClient(TestServer(app))
        await client.start_server()
        bearer = {"Authorization": "Bearer sekrit"}
        try:
            rail = await (await client.get(
                "/api/sessions?view=rail", headers=bearer)).json()
            meta = await (await client.get(
                "/api/sessions/quiet/meta", headers=bearer)).json()
            return rail["sessions"][0], meta
        finally:
            await client.close()
            await mgr.shutdown_all()

    row, meta = asyncio.run(run())
    assert row["watch"] == [] and meta["watch"] == []
