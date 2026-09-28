"""The measurement window's arbiter semantics (``daemon/window.py``).

What is pinned here is the part the chat protocol could not say: who holds
the machine's test window, decided by one process, with death releasing
instead of blocking. Board ``claunch-8y5j`` carries the design and the four
measured holes it closes (``claunch-rwq``'s blind spot, s286's stale slot,
``claunch-fnhu``'s out-of-mesh runner, the queue-in-chat).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import window as window_mod
from claude_launcher.daemon.window import WindowManager


class _Manager:
    """The slice of SessionManager the window consults: get(name) -> session
    with an ``exited`` flag, KeyError for a session the daemon never had."""

    def __init__(self):
        self.states = {}

    def set(self, name: str, exited: bool) -> None:
        self.states[name] = exited

    def get(self, name: str):
        if name not in self.states:
            raise KeyError(name)
        return SimpleNamespace(exited=self.states[name])


class _MessageSink:
    """A mesh-shaped dependency that records any attempted state broadcast."""

    def __init__(self):
        self.messages = []

    def list(self):
        return [SimpleNamespace(name="observers")]

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _ReminderSession:
    def __init__(self) -> None:
        self.exited = False
        self.delivered = []

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True


class _ReminderManager(_Manager):
    def __init__(self, session) -> None:
        super().__init__()
        self.session = session

    def get(self, name: str):
        if name != "holder":
            raise KeyError(name)
        return self.session


def _make(tmp_path, manager=None, caps=(1, 5), cores=32, **limits) -> WindowManager:
    shape = dict(window_mod.DEFAULT_LIMITS, **limits)
    return WindowManager(
        manager,
        caps=lambda: caps,
        limits=lambda: shape,
        state_path=tmp_path / "window.json",
        cores=cores,
    )


def _session(name: str):
    return SimpleNamespace(sdef=SimpleNamespace(name=name))


def _dead_pid() -> int:
    """A pid that certainly is not running: one we just watched exit."""
    proc = subprocess.run(
        [sys.executable, "-c", "pass"], capture_output=True
    )
    return proc.pid if hasattr(proc, "pid") else _spawned_dead_pid()


def _spawned_dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------------- #
# the two classes and their caps
# --------------------------------------------------------------------------- #
def test_a_sweep_excludes_everything(tmp_path):
    async def run():
        w = _make(tmp_path)
        first = await w.acquire("sweep", session="a", pid=os.getpid())
        assert first["granted"]
        second = await w.acquire("sweep", session="b", pid=os.getpid())
        targeted = await w.acquire("targeted", session="c", pid=os.getpid())
        assert not second["granted"]
        assert not targeted["granted"]

    asyncio.run(run())


def test_targeted_runs_share_up_to_the_cap(tmp_path):
    async def run():
        w = _make(tmp_path)
        # Two workers each keeps five runs inside the budget of twelve, so
        # the cap is the rule that binds the sixth.
        grants = [
            await w.acquire("targeted", session=f"t{i}", pid=os.getpid(), workers=2)
            for i in range(5)
        ]
        assert all(g["granted"] for g in grants)
        sixth = await w.acquire("targeted", session="t5", pid=os.getpid(), workers=2)
        assert not sixth["granted"]
        assert sixth["position"] == 1
        assert "targeted cap (5)" in sixth["reason"]

    asyncio.run(run())


def test_a_sweep_does_not_enter_while_targeted_runs_hold(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("targeted", session="t0", pid=os.getpid())
        sweep = await w.acquire("sweep", session="s", pid=os.getpid())
        assert not sweep["granted"]

    asyncio.run(run())


def test_a_queued_sweep_blocks_new_targeted_grants(tmp_path):
    """Writer preference: without it, rotating targeted runs starve the sweep."""
    async def run():
        w = _make(tmp_path)
        holder = await w.acquire("targeted", session="t0", pid=os.getpid())
        waiting = asyncio.create_task(
            w.acquire("sweep", session="s", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)  # let the sweep reach the queue
        newcomer = await w.acquire("targeted", session="t1", pid=os.getpid())
        assert not newcomer["granted"]
        w.release(holder["grant_id"])
        granted = await waiting
        assert granted["granted"]

    asyncio.run(run())


def test_a_waiting_entry_jumps_no_queue_with_wait_zero(tmp_path):
    """A free-but-queued slot belongs to the queue's head, not to a poller."""
    async def run():
        w = _make(tmp_path, caps=(1, 1))
        await w.acquire("targeted", session="t0", pid=os.getpid())
        waiter = asyncio.create_task(
            w.acquire("targeted", session="t1", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        poller = await w.acquire("targeted", session="t2", pid=os.getpid())
        assert not poller["granted"]
        w.release_session("t0")
        assert (await waiter)["granted"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# death releases
# --------------------------------------------------------------------------- #
def test_an_exited_session_releases_and_the_queue_advances(tmp_path):
    async def run():
        manager = _Manager()
        manager.set("a", exited=False)
        manager.set("b", exited=False)
        w = _make(tmp_path, manager=manager)
        holder = await w.acquire("sweep", session="a", pid=os.getpid())
        assert holder["granted"]
        waiter = asyncio.create_task(
            w.acquire("sweep", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        manager.set("a", exited=True)
        w.session_exited(_session("a"))
        assert (await waiter)["granted"]
        assert len(w.status()["holders"]) == 1

    asyncio.run(run())


def test_a_dead_manual_holder_is_reaped_at_the_next_acquire(tmp_path):
    async def run():
        w = _make(tmp_path)
        held = await w.acquire("sweep", session=None, pid=_dead_pid())
        assert held["granted"]
        nxt = await w.acquire("sweep", session="b", pid=os.getpid())
        assert nxt["granted"]

    asyncio.run(run())


def test_a_session_the_daemon_never_had_is_reaped(tmp_path):
    async def run():
        w = _make(tmp_path, manager=_Manager())
        held = await w.acquire("sweep", session="ghost", pid=os.getpid())
        assert held["granted"]
        nxt = await w.acquire("sweep", session="b", pid=os.getpid())
        assert nxt["granted"]

    asyncio.run(run())


def test_reaping_a_dead_holder_advances_an_existing_waiter(tmp_path):
    async def run():
        manager = _Manager()
        for name in ("a", "b", "c"):
            manager.set(name, exited=False)
        w = _make(tmp_path, manager=manager)
        await w.acquire("sweep", session="a", pid=os.getpid())
        waiter = asyncio.create_task(
            w.acquire("sweep", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        manager.set("a", exited=True)
        newcomer = await w.acquire("targeted", session="c", pid=os.getpid())
        assert not newcomer["granted"]
        assert (await waiter)["granted"]
        assert [h["session"] for h in w.status()["holders"]] == ["b"]

    asyncio.run(run())


def test_a_cancelled_long_poll_leaves_no_orphan_queue_entry(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="a", pid=os.getpid())
        waiter = asyncio.create_task(
            w.acquire("sweep", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert w.status()["queue"] == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# advisory_n: the fair share the arbiter is positioned to compute
# --------------------------------------------------------------------------- #
def test_advisory_n_splits_cores_over_active_runs(tmp_path):
    async def run():
        # Budget off and a wide targeted ceiling isolate the core share.
        w = _make(tmp_path, cores=32, worker_budget=0, targeted_width=8)
        first = await w.acquire("targeted", session="t0", pid=os.getpid())
        assert first["advisory_n"] == 8  # 32 // 1, capped
        for i in range(1, 5):
            await w.acquire("targeted", session=f"t{i}", pid=os.getpid())
        assert w.advisory_n(extra=1) == 5  # 32 // 6
        # The floor binds when the share drops below it: 4 cores over 4
        # active runs is 1, and the answer stays 2.
        w2 = _make(tmp_path / "other", cores=4, worker_budget=0)
        for i in range(3):
            await w2.acquire("targeted", session=f"u{i}", pid=os.getpid())
        assert w2.advisory_n(extra=1) == window_mod.ADVISORY_MIN

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# spending limits (claunch-8kald): class width, worker budget, per session
# --------------------------------------------------------------------------- #
def test_a_targeted_grant_is_capped_at_the_class_width(tmp_path):
    async def run():
        w = _make(tmp_path, cores=32)
        targeted = await w.acquire("targeted", session="t0", pid=os.getpid())
        assert targeted["advisory_n"] == 4
        w.release(targeted["grant_id"])
        sweep = await w.acquire("sweep", session="s", pid=os.getpid())
        assert sweep["advisory_n"] == 8

    asyncio.run(run())


def test_the_grant_never_exceeds_the_requested_width(tmp_path):
    async def run():
        w = _make(tmp_path)
        one = await w.acquire("targeted", session="t0", pid=os.getpid(), workers=1)
        assert one["advisory_n"] == 1
        assert w.status()["workers_in_use"] == 1
        assert w.status()["holders"][0]["workers"] == 1

    asyncio.run(run())


def test_the_worker_budget_narrows_then_refuses_targeted_grants(tmp_path):
    async def run():
        w = _make(tmp_path, worker_budget=6)
        first = await w.acquire("targeted", session="a", pid=os.getpid())
        second = await w.acquire("targeted", session="b", pid=os.getpid())
        assert (first["advisory_n"], second["advisory_n"]) == (4, 2)
        third = await w.acquire("targeted", session="c", pid=os.getpid())
        assert not third["granted"]
        assert "worker budget (6)" in third["reason"]
        waiter = asyncio.create_task(
            w.acquire("targeted", session="c", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        w.release(first["grant_id"])
        granted = await waiter
        assert granted["granted"] and granted["advisory_n"] == 4

    asyncio.run(run())


def test_one_session_holds_one_targeted_grant_at_a_time(tmp_path):
    async def run():
        w = _make(tmp_path)
        mine = await w.acquire("targeted", session="a", pid=os.getpid(), workers=1)
        assert mine["granted"]
        again = await w.acquire("targeted", session="a", pid=os.getpid(), workers=1)
        assert not again["granted"]
        assert "session a already holds 1 targeted grant" in again["reason"]
        other = await w.acquire("targeted", session="b", pid=os.getpid(), workers=1)
        assert other["granted"]
        # A manual, sessionless hold is not a session and is not limited.
        manual = [
            await w.acquire("targeted", session=None, pid=os.getpid(), workers=1)
            for _ in range(2)
        ]
        assert all(m["granted"] for m in manual)

    asyncio.run(run())


def test_a_queued_same_session_request_does_not_block_other_sessions(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("targeted", session="a", pid=os.getpid(), workers=1)
        blocked = asyncio.create_task(
            w.acquire("targeted", session="a", pid=os.getpid(), workers=1, wait=5)
        )
        await asyncio.sleep(0.05)
        other = asyncio.create_task(
            w.acquire("targeted", session="b", pid=os.getpid(), workers=1, wait=5)
        )
        assert (await asyncio.wait_for(other, 1))["granted"]
        w.release_session("a")
        assert (await blocked)["granted"]

    asyncio.run(run())


def test_the_per_session_limit_can_be_switched_off(tmp_path):
    async def run():
        w = _make(tmp_path, targeted_per_session=0)
        for _ in range(2):
            got = await w.acquire("targeted", session="a", pid=os.getpid(), workers=1)
            assert got["granted"]

    asyncio.run(run())


def test_limits_default_from_the_daemon_config(home, tmp_path):
    from claude_launcher import store

    w = WindowManager(None, state_path=tmp_path / "window.json", cores=32)
    status = w.status()
    assert status["caps"] == {"sweep": 1, "targeted": 3}
    assert status["limits"] == window_mod.DEFAULT_LIMITS
    assert store.DAEMON_DEFAULTS["window_targeted_cap"] == 3
    for key, value in window_mod.DEFAULT_LIMITS.items():
        assert store.DAEMON_DEFAULTS[f"window_{key}"] == value


# --------------------------------------------------------------------------- #
# operator overrides (claunch-8kald): priority and force
# --------------------------------------------------------------------------- #
def test_prioritize_moves_a_waiting_request_to_the_top(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="holder", pid=os.getpid())
        first = asyncio.create_task(
            w.acquire("sweep", session="b", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            w.acquire("sweep", session="c", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        queued = w.status()["queue"]
        assert [q["session"] for q in queued] == ["b", "c"]
        moved = w.prioritize(queued[1]["grant_id"])
        assert moved == {"priority": 1, "granted": False, "position": 1}
        assert [q["session"] for q in w.status()["queue"]] == ["c", "b"]
        w.release_session("holder")
        assert (await second)["granted"]
        assert not first.done()
        w.release_session("c")
        assert (await first)["granted"]

    asyncio.run(run())


def test_a_newcomer_queues_behind_priority_and_ahead_of_demoted_requests(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="holder", pid=os.getpid())
        demoted = asyncio.create_task(
            w.acquire("sweep", session="low", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        low = w.status()["queue"][0]["grant_id"]
        assert w.prioritize(low, -1)["position"] == 1
        poll = await w.acquire("sweep", session="new", pid=os.getpid())
        assert poll["position"] == 1  # nobody at priority >= 0 is waiting
        demoted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await demoted

    asyncio.run(run())


def test_prioritizing_past_a_queued_sweep_can_grant_on_the_spot(tmp_path):
    """Writer preference yields to the operator's order."""
    async def run():
        w = _make(tmp_path)
        await w.acquire("targeted", session="t0", pid=os.getpid(), workers=1)
        sweep = asyncio.create_task(
            w.acquire("sweep", session="s", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.02)
        targeted = asyncio.create_task(
            w.acquire("targeted", session="t1", pid=os.getpid(), workers=1, wait=5)
        )
        await asyncio.sleep(0.05)
        waiting = [q for q in w.status()["queue"] if q["session"] == "t1"][0]
        result = w.prioritize(waiting["grant_id"])
        assert result["granted"] and result["position"] is None
        assert (await targeted)["granted"]
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep

    asyncio.run(run())


def test_force_grants_a_waiting_request_next_to_the_holders(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="holder", pid=os.getpid())
        waiter = asyncio.create_task(
            w.acquire("sweep", session="urgent", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        grant_id = w.status()["queue"][0]["grant_id"]
        holder = w.force(grant_id)
        assert holder["forced"] and holder["workers"] == 8
        granted = await waiter
        assert granted["granted"] and granted["forced"]
        assert len(w.status()["holders"]) == 2
        assert w.force(grant_id) is None  # no longer waiting
        # A forced holder counts: nothing else enters past it.
        late = await w.acquire("targeted", session="late", pid=os.getpid())
        assert not late["granted"]

    asyncio.run(run())


def test_an_operator_acquisition_can_be_forced_but_a_session_cannot(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="holder", pid=os.getpid())
        refused = await w.acquire("sweep", session="agent", pid=os.getpid(), force=True)
        assert not refused["granted"]
        assert "operator action" in refused["error"]
        forced = await w.acquire("targeted", session=None, pid=os.getpid(), force=True)
        assert forced["granted"] and forced["forced"]

    asyncio.run(run())


def test_priority_and_force_survive_a_restart_as_recorded_state(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session=None, pid=os.getpid(), force=True)
        w2 = _make(tmp_path)
        assert w2.status()["holders"][0]["forced"] is True

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# persistence and release paths
# --------------------------------------------------------------------------- #
def test_the_window_survives_a_restart(tmp_path):
    async def run():
        w = _make(tmp_path)
        holder = await w.acquire("targeted", session="a", pid=os.getpid())
        assert holder["granted"]
        # No reaping at load: restoring sessions have not come back yet, so
        # death is judged lazily, at the next acquire.
        w2 = _make(tmp_path)
        status = w2.status()
        assert [h["session"] for h in status["holders"]] == ["a"]

    asyncio.run(run())


def test_release_by_session_and_by_grant_id(tmp_path):
    async def run():
        w = _make(tmp_path)
        a = await w.acquire("targeted", session="a", pid=os.getpid())
        b = await w.acquire("targeted", session="b", pid=os.getpid())
        assert w.release(a["grant_id"])
        assert not w.release(a["grant_id"])  # already gone
        assert w.release_session("b") == 1
        assert w.status()["holders"] == []

    asyncio.run(run())


def test_cancel_by_waiting_id_or_session_leaves_holders_untouched(tmp_path):
    async def run():
        w = _make(tmp_path)
        holder = await w.acquire("sweep", session="holder", pid=os.getpid())
        first = asyncio.create_task(
            w.acquire("sweep", session="waiter", pid=os.getpid(), wait=5)
        )
        second = asyncio.create_task(
            w.acquire("sweep", session="waiter", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        queued = w.status()["queue"]
        assert w.cancel(queued[0]["grant_id"])
        assert w.cancel_session("waiter") == 1
        assert not w.cancel(queued[0]["grant_id"])
        assert [h["grant_id"] for h in w.status()["holders"]] == [holder["grant_id"]]
        for task in (first, second):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


def test_an_unknown_class_is_refused(tmp_path):
    async def run():
        w = _make(tmp_path)
        result = await w.acquire("sideways", session="a", pid=os.getpid())
        assert not result["granted"]
        assert "unknown window class" in result["error"]

    asyncio.run(run())


def test_a_wait_that_times_out_leaves_no_queue_entry(tmp_path):
    async def run():
        w = _make(tmp_path)
        await w.acquire("sweep", session="a", pid=os.getpid())
        result = await w.acquire("sweep", session="b", pid=os.getpid(), wait=0.1)
        assert not result["granted"]
        assert result["timeout"]
        assert w.status()["queue"] == []

    asyncio.run(run())


def test_window_wait_is_capped_at_thirty_minutes(tmp_path):
    w = _make(tmp_path)
    status = w.status()
    assert window_mod.MAX_WAIT == 30 * 60
    assert status["max_wait"] == 30 * 60


def test_window_holder_reminder_repeats_every_three_minutes(tmp_path):
    async def run():
        session = _ReminderSession()
        manager = _ReminderManager(session)
        w = _make(tmp_path, manager=manager)
        granted = await w.acquire("sweep", session="holder", pid=os.getpid())
        clock = window_mod.WindowReminderClock(manager, w)

        assert clock.scan(now=0) == []
        assert clock.scan(now=179) == []
        due = clock.scan(now=180)
        assert [entry["grant_id"] for entry in due] == [granted["grant_id"]]
        await clock._deliver(due[0])
        assert len(session.delivered) == 1
        assert "repeats every 3 minutes" in session.delivered[0]
        assert granted["grant_id"] in session.delivered[0]
        assert "completed or failed" in session.delivered[0]

        # Delivery re-arms only the independent holder clock.  The ordinary
        # session reminder service is not constructed in this test.
        clock._seen[granted["grant_id"]] = 180
        assert clock.scan(now=359) == []
        assert len(clock.scan(now=360)) == 1
        assert w.release(granted["grant_id"])
        assert clock.scan(now=361) == []

    asyncio.run(run())


def test_window_api_status_acquire_release_and_cancel(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        window = _make(tmp_path)
        app = build_app(
            manager,
            "secret",
            started_at=time.monotonic(),
            window=window,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            assert (await client.get("/api/window")).status == 401
            status = await (await client.get("/api/window", headers=headers)).json()
            assert status["caps"] == {"sweep": 1, "targeted": 5}
            assert status["max_wait"] == 30 * 60
            assert status["reminder_interval"] == 3 * 60

            acquired = await (
                await client.post(
                    "/api/window/acquire",
                    json={
                        "class": "targeted",
                        "pid": os.getpid(),
                        "label": "api test",
                    },
                    headers=headers,
                )
            ).json()
            assert acquired["granted"]
            released = await (
                await client.post(
                    "/api/window/release",
                    json={"grant_id": acquired["grant_id"]},
                    headers=headers,
                )
            ).json()
            assert released == {"released": 1}

            held = await app["window"].acquire("sweep", session="holder", pid=os.getpid())
            waiting = asyncio.create_task(
                app["window"].acquire("sweep", session="waiter", pid=os.getpid(), wait=5)
            )
            await asyncio.sleep(0.05)
            queued = app["window"].status()["queue"][0]
            cancelled = await (
                await client.post(
                    "/api/window/cancel", json={"grant_id": queued["grant_id"]}, headers=headers
                )
            ).json()
            assert cancelled == {"cancelled": 1}
            assert [h["grant_id"] for h in app["window"].status()["holders"]] == [
                held["grant_id"]
            ]
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_window_api_prioritize_and_force(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        app = build_app(
            manager, "secret", started_at=time.monotonic(), window=_make(tmp_path)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            await app["window"].acquire("sweep", session="holder", pid=os.getpid())
            waiting = asyncio.create_task(
                app["window"].acquire("sweep", session="waiter", pid=os.getpid(), wait=5)
            )
            await asyncio.sleep(0.05)
            grant_id = app["window"].status()["queue"][0]["grant_id"]

            missing = await client.post(
                "/api/window/prioritize", json={"grant_id": "nope"}, headers=headers
            )
            assert missing.status == 404
            bad = await client.post(
                "/api/window/prioritize",
                json={"grant_id": grant_id, "priority": "high"},
                headers=headers,
            )
            assert bad.status == 400
            moved = await (
                await client.post(
                    "/api/window/prioritize",
                    json={"grant_id": grant_id, "priority": 7},
                    headers=headers,
                )
            ).json()
            assert moved == {"priority": 7, "granted": False, "position": 1}

            forced = await (
                await client.post(
                    "/api/window/force", json={"grant_id": grant_id}, headers=headers
                )
            ).json()
            assert forced["forced"] and forced["holder"]["grant_id"] == grant_id
            assert (await waiting)["granted"]
            gone = await client.post(
                "/api/window/force", json={"grant_id": grant_id}, headers=headers
            )
            assert gone.status == 404

            refused = await client.post(
                "/api/window/acquire",
                json={"class": "sweep", "session": "agent", "force": True},
                headers=headers,
            )
            assert refused.status == 400
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# run history (claunch-8kald): the daemon's own record, kept 7 days
# --------------------------------------------------------------------------- #
def test_a_released_grant_leaves_one_history_line_with_its_result(tmp_path):
    async def run():
        w = _make(tmp_path)
        grant = await w.acquire(
            "targeted", session="a", pid=os.getpid(), label="pytest x", workers=2
        )
        assert w.report_result(grant["grant_id"], {
            "outcome": "failed", "passed": 10, "failed": 2, "exit_code": 1,
            "duration": 3.25, "junk": "dropped",
        })
        assert w.release(grant["grant_id"])
        history = w.history()
        assert history["retention_days"] == 7 and history["total"] == 1
        line = history["entries"][0]
        assert line["end"] == "released" and line["workers"] == 2
        assert line["label"] == "pytest x" and line["held_seconds"] is not None
        assert line["result"] == {
            "outcome": "failed", "passed": 10, "failed": 2, "exit_code": 1,
            "duration": 3.25,
        }  # the unknown "junk" field is dropped
        stats = history["stats"]["classes"]["targeted"]
        assert stats["runs"] == 1 and stats["outcomes"] == {"failed": 1}
        # The file carries the line across a restart.
        assert _make(tmp_path).history()["total"] == 1

    asyncio.run(run())


def test_a_result_needs_a_live_grant_and_known_fields(tmp_path):
    async def run():
        w = _make(tmp_path)
        assert not w.report_result("nope", {"outcome": "passed"})
        grant = await w.acquire("targeted", session="a", pid=os.getpid())
        assert not w.report_result(grant["grant_id"], {"colour": "green"})
        assert not w.report_result(grant["grant_id"], "passed")
        assert w.report_result(grant["grant_id"], {"outcome": "sideways", "passed": "4"})
        assert w.status()["holders"][0]["result"] == {"passed": 4}

    asyncio.run(run())


def test_every_way_a_request_ends_is_recorded(tmp_path):
    async def run():
        manager = _Manager()
        for name in ("h", "gone", "w1", "w2"):
            manager.set(name, exited=False)
        w = _make(tmp_path, manager=manager)
        await w.acquire("sweep", session="h", pid=os.getpid())
        timed_out = await w.acquire("sweep", session="w1", pid=os.getpid(), wait=0.05)
        assert timed_out.get("timeout")
        waiting = asyncio.create_task(
            w.acquire("sweep", session="w2", pid=os.getpid(), wait=5)
        )
        await asyncio.sleep(0.05)
        assert w.cancel_session("w2") == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        w.session_exited(_session("h"))
        manager.set("gone", exited=False)
        held = await w.acquire("sweep", session="gone", pid=os.getpid())
        assert held["granted"]
        manager.set("gone", exited=True)
        await w.acquire("targeted", session=None, pid=os.getpid())  # reaps "gone"
        ends = [e["end"] for e in reversed(w.history()["entries"])]
        assert ends == ["timeout", "cancelled", "session_exited", "reaped"]
        stats = w.history()["stats"]["classes"]["sweep"]
        assert stats["runs"] == 2 and stats["gave_up_waiting"] == 2

    asyncio.run(run())


def test_the_history_keeps_seven_days_and_drops_the_rest(tmp_path):
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "window-history.jsonl"
    now = datetime.now(timezone.utc)
    lines = [
        {"grant_id": "old", "cls": "targeted", "acquired_at": "x",
         "ended_at": (now - timedelta(days=8)).isoformat()},
        {"grant_id": "new", "cls": "targeted", "acquired_at": "x",
         "ended_at": (now - timedelta(days=6)).isoformat()},
    ]
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
    w = _make(tmp_path)
    assert [e["grant_id"] for e in w.history()["entries"]] == ["new"]
    # Dropped from the file too, not only from the answer.
    assert "old" not in path.read_text(encoding="utf-8")
    # A look-back never exceeds the retention, and can be narrower.
    assert w.history(days=30)["days"] == 7
    assert w.history(days=1)["total"] == 0


def test_history_statistics_and_filters(tmp_path):
    async def run():
        w = _make(tmp_path, targeted_per_session=0)
        for i in range(4):
            g = await w.acquire("targeted", session="a" if i < 3 else "b",
                                pid=os.getpid(), workers=1)
            w.report_result(g["grant_id"], {"outcome": "passed" if i else "failed"})
            w.release(g["grant_id"])
        both = w.history()
        assert both["stats"]["classes"]["targeted"]["outcomes"] == {"failed": 1, "passed": 3}
        assert both["stats"]["top_sessions"][0] == {"holder": "a", "runs": 3}
        assert w.history(session="b")["total"] == 1
        assert w.history(cls="sweep")["total"] == 0
        assert len(w.history(limit=2)["entries"]) == 2
        assert window_mod._quantile([1, 2, 3, 4, 100], 0.9) == 100
        assert window_mod._quantile([], 0.5) is None

    asyncio.run(run())


def test_window_api_report_and_history(home, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        app = build_app(
            manager, "secret", started_at=time.monotonic(), window=_make(tmp_path)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            grant = await app["window"].acquire("targeted", session="s", pid=os.getpid())
            ok = await client.post(
                "/api/window/report",
                json={"grant_id": grant["grant_id"], "result": {"outcome": "passed"}},
                headers=headers,
            )
            assert ok.status == 200
            missing = await client.post(
                "/api/window/report",
                json={"grant_id": "nope", "result": {"outcome": "passed"}},
                headers=headers,
            )
            assert missing.status == 404
            released = await (
                await client.post(
                    "/api/window/release",
                    json={"grant_id": grant["grant_id"], "result": {"passed": 5}},
                    headers=headers,
                )
            ).json()
            assert released == {"released": 1}
            history = await (
                await client.get("/api/window/history?class=targeted&limit=5",
                                 headers=headers)
            ).json()
            assert history["retention_days"] == 7
            assert history["entries"][0]["result"] == {"passed": 5}
            bad = await client.get("/api/window/history?days=soon", headers=headers)
            assert bad.status == 400
        finally:
            await client.close()
            await manager.shutdown_all()

    asyncio.run(run())


def test_window_state_changes_do_not_emit_mesh_messages(home, tmp_path):
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        manager = SessionManager(
            idle_threshold=0.5, scrollback=100, restore_default=False
        )
        mesh = _MessageSink()
        app = build_app(
            manager,
            "secret",
            started_at=time.monotonic(),
            mesh=mesh,
        )
        try:
            grant = await app["window"].acquire(
                "targeted", session="worker", pid=os.getpid()
            )
            assert grant["granted"]
            assert app["window"].release(grant["grant_id"])
            await asyncio.sleep(0)
            assert mesh.messages == []
        finally:
            await manager.shutdown_all()

    asyncio.run(run())
