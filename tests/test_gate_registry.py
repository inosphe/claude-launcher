"""The daemon's gate registry (claunch-6vq2m.1, ``daemon/gate_registry.py``).

A v2 gate that waits on a person is a row in ``gates.db`` opened once and
closed once with a reason. The rail read compares a run with the registry
when its files change; an archived or removed session's gates close with the
record; the gates already open when the registry came in are recorded once
(the backfill); and an ended session's run is read only while it holds an
open gate.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.cflow2 import layers, runtime
from claude_launcher.daemon import api as api_mod, cflow2_clock, cflow2_web, gate_registry
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import DeadSession

PICK = """\
cflow: 2
name: pick
description: a user choice, then a task
nodes:
  a: {title: Step A, prompt: do a}
  b: {title: Step B, prompt: do b}
body:
  choose:
    prompt: which way?
    chooser: user
    options:
      left: {then: a}
      right: {then: b}
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v2 = d / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "pick.yaml").write_text(PICK, encoding="utf-8")
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_RAIL", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


@pytest.fixture
def reg(tmp_path):
    r = gate_registry.GateRegistry(tmp_path / "gates.db")
    gate_registry.install(r)
    return r


def _start(cwd, scope="s1"):
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope=scope, caps={})


def _select(cwd, scope="s1"):
    runtime.select(None, "left", cwd=cwd, scope=scope, caller=runtime.CALLER_USER)


def test_a_gate_opens_once_and_closes_resolved_when_answered(proj, reg):
    cwd = str(proj)
    _start(cwd)
    cflow2_web.rail_entries([("s1", cwd)])
    cflow2_web.rail_entries([("s1", cwd)])  # unchanged files: nothing new
    rows = reg.rows(state="all")
    assert len(rows) == 1
    g = rows[0]
    assert (g["session"], g["kind"], g["closed_at"]) == ("s1", "choose", None)
    assert g["scope"] == "s1@main" and g["workflow"] == "pick"
    assert g["card"]["options"][0]["name"] == "left"
    # The run's own stamp, not the moment the registry read it.
    assert g["opened_at"] == g["card"]["opened_at"]

    _select(cwd)
    cflow2_web.rail_entries([("s1", cwd)])
    g = reg.rows(state="all")[0]
    assert g["close_reason"] == gate_registry.CLOSE_RESOLVED and g["closed_at"]
    assert reg.open_sessions() == {}


def test_an_aborted_run_closes_its_gates_as_run_ended(proj, reg):
    cwd = str(proj)
    _start(cwd)
    cflow2_web.rail_entries([("s1", cwd)])
    runtime.abort("stop", cwd=cwd, scope="s1")
    cflow2_web.rail_entries([("s1", cwd)])
    assert reg.rows(state="closed")[0]["close_reason"] == gate_registry.CLOSE_RUN_ENDED


def test_archive_and_remove_close_the_sessions_gates_and_a_respawn_reopens(proj, reg):
    cwd = str(proj)
    _start(cwd, "s1")
    _start(cwd, "s2")
    mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
    mgr.filed_hooks.append(gate_registry.lifecycle_closer(reg))
    for name in ("s1", "s2"):
        mgr._sessions[name] = DeadSession(SessionDef(name=name, harness="claude", cwd=cwd),
                                          exit_code=0)
    cflow2_web.rail_entries([("s1", cwd), ("s2", cwd)])
    assert reg.open_sessions() == {"s1": 1, "s2": 1}

    mgr.archive("s1")
    mgr.archive("s1")  # idempotent: nothing more to close
    mgr.remove("s2")
    closed = {g["session"]: g["close_reason"] for g in reg.rows(state="closed")}
    assert closed == {"s1": gate_registry.CLOSE_ARCHIVED, "s2": gate_registry.CLOSE_REMOVED}

    # The archive dropped the cached rail entry: the session read again (a
    # respawn) finds its run still waiting and opens a new row; the closed
    # one stays as history.
    cflow2_web.rail_entries([("s1", cwd)])
    s1 = reg.rows(state="all", session="s1")
    assert [g["closed_at"] is None for g in s1] == [True, False]


def test_backfill_records_the_open_gates_once(proj, reg):
    cwd = str(proj)
    for name in ("live", "killed", "filed", "idle"):
        _start(cwd, name)
    _select(cwd, "idle")  # its run waits on nobody
    sessions = [
        SimpleNamespace(sdef=SimpleNamespace(name="live", cwd=cwd), exited=False),
        SimpleNamespace(sdef=SimpleNamespace(name="killed", cwd=cwd), exited=True),
        SimpleNamespace(sdef=SimpleNamespace(name="filed", cwd=cwd), exited=True,
                        archived_at="2026-10-29T00:00:00+00:00"),
        SimpleNamespace(sdef=SimpleNamespace(name="idle", cwd=cwd), exited=False),
        SimpleNamespace(sdef=SimpleNamespace(name="norun", cwd=str(proj / "x")),
                        exited=False),
    ]
    # A gate recorded for a session that is no longer registered.
    reg.sync("gone", cwd, "gone", {"run": "r0", "status": "waiting_approval",
                                    "answers": [{"kind": "halt", "label": "m"}]})

    out = gate_registry.backfill(reg, sessions, now=1000.0)
    assert out["open"] == 2 and out["sessions_with_open_gates"] == 2
    assert out["closed"] == 1 and out["open_before"] == 1
    assert set(reg.open_sessions()) == {"live", "killed"}
    assert reg.meta(gate_registry.META_BACKFILLED)["open"] == 2
    assert reg.rows(state="closed")[0]["close_reason"] == gate_registry.CLOSE_REMOVED

    # Done once; forced again it opens nothing new.
    assert gate_registry.backfill(reg, sessions) is None
    again = gate_registry.backfill(reg, sessions, force=True)
    assert again["open"] == 2 and len(reg.rows(state="all")) == 3


def test_the_clock_records_a_gate_without_a_dashboard(proj, reg):
    cwd = str(proj)
    _start(cwd)
    live = SimpleNamespace(sdef=SimpleNamespace(name="s1", cwd=cwd), exited=False)
    mgr = SimpleNamespace(list=lambda: [live], get=lambda n: live if n == "s1" else None)
    clock = cflow2_clock.Cflow2Clock(mgr, heartbeat=False, threaded=False,
                                     probe_runner=None)
    # The clock's own pass records it -- no rail poll, no direct call.
    clock.scan()
    assert reg.open_sessions() == {"s1": 1}


def test_a_sessions_exit_records_its_waiting_gate(proj, reg):
    """The exit hook compares the ended session once more: a gate it left
    standing is recorded though no clock pass drives that session now."""
    cwd = str(proj)
    _start(cwd)
    ended = SimpleNamespace(sdef=SimpleNamespace(name="s1", cwd=cwd), exited=True)
    mgr = SimpleNamespace(list=lambda: [ended], get=lambda n: ended if n == "s1" else None)
    clock = cflow2_clock.Cflow2Clock(mgr, heartbeat=False, threaded=False,
                                     probe_runner=None)
    clock.session_exited(ended)
    clock.end_sides()
    assert reg.open_sessions() == {"s1": 1}


def test_an_unreadable_run_leaves_its_gates_open(proj, reg):
    """A rail entry that failed to read (``status: error``) says nothing about
    the gates: they stay open until the run is read again."""
    cwd = str(proj)
    _start(cwd)
    cflow2_web.rail_entries([("s1", cwd)])
    assert reg.sync("s1", cwd, "s1", {"status": "error", "error": "boom"}) == (0, 0)
    assert reg.open_sessions() == {"s1": 1}


def _serve(mgr_sessions, monkeypatch, calls):
    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        monkeypatch.setattr(mgr, "list", lambda *a, **k: mgr_sessions)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        out = []
        try:
            for call in calls:
                out.append(await call(app, client))
        finally:
            await client.close()
        return out

    return asyncio.run(run())


AUTH = {"Authorization": "Bearer sekrit"}


def test_rail_reads_an_ended_session_only_while_it_holds_a_gate(proj, monkeypatch):
    """The rail poll no longer stats every registered session's run: an
    ended session with nothing open is read once and then served from the
    cache (its line still shows, with no answers). An archived one is served
    the same way -- its line seeds "Spawn same setup" -- but its waiting run
    opens no gate again and raises no card."""
    cwd = str(proj)
    for name in ("live", "killed", "done", "filed"):
        _start(cwd, name)
    _select(cwd, "done")
    sessions = [
        SimpleNamespace(sdef=SimpleNamespace(name=n, cwd=cwd), status=lambda: "idle",
                        exited=n != "live",
                        **({"archived_at": "2026-10-29T00:00:00+00:00"} if n == "filed" else {}))
        for n in ("live", "killed", "done", "filed")
    ]
    read = []
    real = cflow2_web._rail_stamp
    monkeypatch.setattr(cflow2_web, "_rail_stamp",
                        lambda c, s: read.append(s) or real(c, s))

    async def backfill(app, client):
        out = gate_registry.backfill(app["gates"], sessions)
        # A fresh daemon: nothing read yet.
        cflow2_web._RAIL.clear()
        return out

    async def poll(app, client):
        read.clear()
        resp = await client.get("/api/cflow2/rail", headers=AUTH)
        return sorted(read), await resp.json()

    async def answer_killed(app, client):
        _select(cwd, "killed")

    async def gates(app, client):
        resp = await client.get("/api/gates?state=all", headers=AUTH)
        return resp.status, await resp.json()

    async def bad(app, client):
        return (await client.get("/api/gates?state=nope", headers=AUTH)).status

    (filled, (read1, first), (read2, second), _, (read3, third), (read4, fourth),
     (status, body), bad_status) = _serve(
        sessions, monkeypatch,
        [backfill, poll, poll, answer_killed, poll, poll, gates, bad])
    assert filled["open"] == 2
    # First poll: the settled and the archived session are read once.
    assert read1 == ["done", "filed", "killed", "live"]
    assert set(first["rows"]) == {"live", "killed", "done", "filed"}
    assert first["rows"]["done"]["answers"] == []
    # The archived run still waits on its choice; its row keeps the workflow
    # and shows no answer, and the registry does not open it again.
    assert first["rows"]["filed"]["workflow"] == "pick"
    assert first["rows"]["filed"]["answers"] == []
    assert first["rows"]["filed"]["session_category"] == "archived"
    killed = first["rows"]["killed"]
    assert killed["session_category"] == "killed"
    assert isinstance(killed["answers"][0]["gate_id"], int)
    # Second poll: the settled session comes from the cache, unread.
    assert read2 == ["killed", "live"] and {"done", "filed"} <= set(second["rows"])

    # Answered (from the CLI, say): the poll still reads the killed session,
    # finds the gate gone and closes it; the poll after that no longer reads it.
    assert read3 == ["killed", "live"]
    assert third["rows"]["killed"]["answers"] == []
    assert read4 == ["live"] and set(fourth["rows"]) == {"live", "killed", "done", "filed"}

    assert status == 200 and bad_status == 400
    by = {g["session"]: g for g in body["gates"]}
    assert by["killed"]["close_reason"] == gate_registry.CLOSE_RESOLVED
    assert by["live"]["closed_at"] is None
    assert "filed" not in by
    assert body["backfill"]["open"] == 2
