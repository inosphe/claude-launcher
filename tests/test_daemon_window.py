"""The measurement window's arbiter semantics (``daemon/window.py``).

What is pinned here is the part the chat protocol could not say: who holds
the machine's test window, decided by one process, with death releasing
instead of blocking. Board ``claunch-8y5j`` carries the design and the four
measured holes it closes (``claunch-rwq``'s blind spot, s286's stale slot,
``claunch-fnhu``'s out-of-mesh runner, the queue-in-chat).
"""

from __future__ import annotations

import asyncio
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


def _make(tmp_path, manager=None, caps=(1, 5), cores=32) -> WindowManager:
    return WindowManager(
        manager,
        caps=lambda: caps,
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
        grants = [
            await w.acquire("targeted", session=f"t{i}", pid=os.getpid())
            for i in range(5)
        ]
        assert all(g["granted"] for g in grants)
        sixth = await w.acquire("targeted", session="t5", pid=os.getpid())
        assert not sixth["granted"]
        assert sixth["position"] == 1

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
        w = _make(tmp_path, cores=32)
        first = await w.acquire("targeted", session="t0", pid=os.getpid())
        assert first["advisory_n"] == 8  # 32 // 1, capped
        for i in range(1, 5):
            await w.acquire("targeted", session=f"t{i}", pid=os.getpid())
        assert w.advisory_n(extra=1) == 5  # 32 // 6
        # The floor binds when the share drops below it: 4 cores over 4
        # active runs is 1, and the answer stays 2.
        w2 = _make(tmp_path / "other", cores=4)
        for i in range(3):
            await w2.acquire("targeted", session=f"u{i}", pid=os.getpid())
        assert w2.advisory_n(extra=1) == window_mod.ADVISORY_MIN

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
