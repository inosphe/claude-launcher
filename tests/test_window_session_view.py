"""One session's view of the measurement window (claunch-bwzkt).

The terminal header's window strip draws from
``WindowManager.session_view`` through ``GET /api/sessions/{name}/window``.
What it must get right: the grant the session holds with how long it has
held and what the kept history says such a run usually takes (the label's
runs first, the class's when the label has too few, nothing below three),
the place a waiting request has in the queue with the rule that keeps it
out, and the session's last finished run.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from claude_launcher.daemon import window as window_mod
from claude_launcher.daemon.window import WindowManager

LABEL = "pytest tests/test_a.py"


def _make(tmp_path, caps=(1, 3), **limits) -> WindowManager:
    shape = dict(window_mod.DEFAULT_LIMITS, **limits)
    return WindowManager(
        None,
        caps=lambda: caps,
        limits=lambda: shape,
        state_path=tmp_path / "window.json",
        cores=32,
    )


def _seed(tmp_path, runs) -> None:
    """Finished runs in the kept log: (cls, label, held_seconds, end)."""
    now = datetime.now(timezone.utc)
    lines = []
    for i, (cls, label, held, end) in enumerate(runs):
        ended = now - timedelta(minutes=10 + i)
        lines.append(
            {
                "grant_id": f"g{i}",
                "cls": cls,
                "session": "old",
                "label": label,
                "acquired_at": (ended - timedelta(seconds=held)).isoformat(),
                "ended_at": ended.isoformat(),
                "held_seconds": held,
                "end": end,
            }
        )
    path = tmp_path / "window-history.jsonl"
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")


def test_a_held_grant_carries_elapsed_and_the_label_median(tmp_path):
    _seed(tmp_path, [("targeted", LABEL, s, "released") for s in (100, 120, 140)])
    w = _make(tmp_path)

    async def run():
        await w.acquire("targeted", session="a", pid=os.getpid(), label=LABEL, workers=2)

    asyncio.run(run())
    view = w.session_view("a", now=time.time() + 30)
    assert view["session"] == "a"
    assert view["waiting"] == []
    (held,) = view["holding"]
    assert held["cls"] == "targeted" and held["label"] == LABEL and held["workers"] == 2
    assert held["estimate"] == {"basis": "label", "samples": 3, "p50": 120.0, "p90": 140.0}
    assert 29 <= held["elapsed_seconds"] <= 32
    assert 88 <= held["remaining_seconds"] <= 91
    # The machine line sees the same holder.
    assert [h["session"] for h in view["window"]["holders"]] == ["a"]
    assert view["window"]["caps"] == {"sweep": 1, "targeted": 3}
    assert view["window"]["workers_in_use"] == 2
    assert view["window"]["worker_budget"] == 12
    # A run past its median reports a negative remainder, not a clamp.
    late = w.session_view("a", now=time.time() + 200)
    assert late["holding"][0]["remaining_seconds"] < 0


def test_the_estimate_falls_back_to_the_class_and_needs_three_runs(tmp_path):
    _seed(
        tmp_path,
        [
            ("targeted", LABEL, 50, "released"),
            ("targeted", "other", 60, "released"),
            ("targeted", "other", 70, "released"),
            # A run whose client left is no measure of a run's length.
            ("targeted", "other", 9999, "disconnected"),
            ("sweep", "full", 600, "released"),
        ],
    )
    w = _make(tmp_path)
    est = w.estimate("targeted", LABEL)
    assert est["basis"] == "class" and est["samples"] == 3 and est["p50"] == 60.0
    assert w.estimate("sweep", "full") is None


def test_a_waiting_request_says_where_it_stands_and_why(tmp_path):
    w = _make(tmp_path)

    async def run():
        await w.acquire("sweep", session="x", pid=os.getpid(), label="full")
        waiter = asyncio.create_task(
            w.acquire("targeted", session="a", pid=os.getpid(), label=LABEL, wait=5)
        )
        await asyncio.sleep(0.05)
        view = w.session_view("a")
        w.release_session("x")
        assert (await waiter)["granted"]
        return view

    view = asyncio.run(run())
    assert view["holding"] == []
    (wait,) = view["waiting"]
    assert wait["cls"] == "targeted" and wait["label"] == LABEL
    assert wait["position"] == 1 and wait["ahead"] == 0
    assert wait["reason"] == "a sweep holds the window"
    assert wait["waited_seconds"] is not None and wait["waited_seconds"] >= 0
    assert view["window"]["queued"] == 1
    assert view["window"]["sweep_queued"] is False
    assert [h["session"] for h in view["window"]["holders"]] == ["x"]


def test_the_queue_position_counts_the_requests_ahead(tmp_path):
    w = _make(tmp_path)

    async def run():
        await w.acquire("targeted", session="h", pid=os.getpid(), workers=1)
        first = asyncio.create_task(
            w.acquire("sweep", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(
            w.acquire("targeted", session="a", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        view = w.session_view("a")
        w.cancel_session("b")
        w.cancel_session("a")
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        return view

    view = asyncio.run(run())
    (wait,) = view["waiting"]
    assert wait["position"] == 2 and wait["ahead"] == 1
    assert wait["reason"] == "a sweep is queued ahead (writer preference)"
    assert view["window"]["sweep_queued"] is True


def test_last_is_the_sessions_latest_finished_run(tmp_path):
    w = _make(tmp_path)

    async def run():
        g = await w.acquire("targeted", session="a", pid=os.getpid(), label=LABEL)
        w.report_result(g["grant_id"], {"outcome": "failed", "failed": 2, "passed": 9})
        w.release(g["grant_id"])

    asyncio.run(run())
    view = w.session_view("a")
    assert view["holding"] == [] and view["waiting"] == []
    assert view["last"]["end"] == "released"
    assert view["last"]["result"] == {"outcome": "failed", "failed": 2, "passed": 9}
    assert w.session_view("nobody")["last"] is None


def test_session_window_api(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        known = {"a": SimpleNamespace(sdef=SimpleNamespace(name="a"))}
        real_get = manager.get
        manager.get = lambda name: known[name] if name in known else real_get(name)
        app = build_app(
            manager, "secret", started_at=time.monotonic(), window=_make(tmp_path)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            assert (await client.get("/api/sessions/a/window")).status == 401
            await app["window"].acquire("targeted", session="a", pid=os.getpid(), label=LABEL)
            resp = await client.get("/api/sessions/a/window", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["session"] == "a"
            assert body["holding"][0]["label"] == LABEL
            assert body["holding"][0]["estimate"] is None
            missing = await client.get("/api/sessions/zz/window", headers=headers)
            # The daemon's answer for an unknown session on every
            # /api/sessions/{name}/... route (the manager's KeyError).
            assert missing.status == 400
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())
