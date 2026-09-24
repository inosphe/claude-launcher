"""The HTTP paths spawn a session's process off the event loop.

Creating the process and its pseudo console took 250-350 ms per session on
this machine, and ``Session.start`` did it on the loop, so every socket and
request stopped for it (claunch-y9ax9.1, found by py-spy on the live daemon).
``launch_async`` and ``respawn_async`` -- what ``api.py`` calls -- run the
spawn on a worker thread; the synchronous ``launch``/``respawn`` stay for the
restore at start-up and the CLI-free callers.

What these pin: the spawn happens on another thread and the loop keeps
turning while it runs, the result is the session ``launch`` would have made,
and a failed spawn leaves the registry the way the synchronous path does.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import pty_backend
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager


class _Pty:
    pid = 4242

    def __init__(self):
        self.done = threading.Event()

    def read(self):
        self.done.wait(5)
        return b""

    def exit_code(self):
        return 0

    def close(self):
        pass

    def isalive(self):
        return not self.done.is_set()

    def terminate(self, force=False):
        self.done.set()

    def resize(self, cols, rows):
        pass

    def write(self, data, writer=None):
        pass

    def forget_writer(self, writer):
        pass


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-c", "pass"]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


@pytest.fixture
def spawns(home, monkeypatch):
    """Records the thread each spawn ran on; the spawn blocks 0.3 s, the
    size of the stall it stands for."""
    _register_py_harness()
    seen = []

    def fake_spawn(argv, *, env, cwd, cols, rows):
        seen.append(threading.get_ident())
        threading.Event().wait(0.3)
        return _Pty()

    monkeypatch.setattr(pty_backend, "spawn", fake_spawn)
    return seen


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)


async def _ticks_during(coro):
    """Run ``coro`` while counting how often the loop gets to run a 10 ms
    timer: a spawn on the loop would leave that count near zero."""
    ticks = 0
    stop = False

    async def tick():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.ensure_future(tick())
    try:
        result = await coro
    finally:
        stop = True
        await ticker
    return result, ticks


def test_launch_async_spawns_on_a_worker_thread(spawns):
    async def run():
        mgr = _manager()
        session = mgr.stage(SessionDef(name="a", harness="py"))
        started, ticks = await _ticks_during(mgr.launch_async(session))
        assert started is session and mgr.get("a") is session
        assert session.pid == _Pty.pid and session.argv
        assert spawns and spawns[0] != threading.get_ident()
        # 0.3 s of spawn: a loop that kept turning ran the 10 ms timer many
        # times; one held by the spawn would have run it once or twice.
        assert ticks >= 5
        session.pty.terminate()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_respawn_async_relaunches_the_same_session_off_the_loop(spawns):
    async def run():
        mgr = _manager()
        mgr._retire(SessionDef(name="old", harness="py"),
                    {"created_at": "2026-08-24T08:30:00+00:00"})
        revived, ticks = await _ticks_during(mgr.respawn_async("old"))
        assert revived.created_at == "2026-08-24T08:30:00+00:00"
        assert revived.resumed_by_human is True
        assert mgr.get("old") is revived and not revived.exited
        assert spawns[0] != threading.get_ident() and ticks >= 5
        revived.pty.terminate()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_a_failed_spawn_leaves_the_registry_as_the_sync_path_does(home, monkeypatch):
    _register_py_harness()

    def broken(argv, **kw):
        raise OSError("no such program")

    monkeypatch.setattr(pty_backend, "spawn", broken)

    async def run():
        mgr = _manager()
        mgr._retire(SessionDef(name="old", harness="py"), {"created_at": "2026-08-24T08:30:00+00:00"})
        record = mgr.get("old")
        with pytest.raises(OSError):
            await mgr.respawn_async("old")
        assert mgr.get("old") is record  # the exited record is kept

        staged = mgr.stage(SessionDef(name="new", harness="py"))
        with pytest.raises(OSError):
            await mgr.launch_async(staged)
        assert staged.pty is None

    asyncio.run(run())
