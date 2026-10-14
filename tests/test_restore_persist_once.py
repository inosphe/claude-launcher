"""A daemon restart persists the registry once, not once per relaunch.

``restore_all`` relaunches every session that was running, one after
another, before the port is bound. Each relaunch used to end in
``persist()``, which rewrites every record in the registry (exited ones
included, about a thousand on the machine that reported it) and re-reads
every live session's transcript for its model. That is work growing with
the square of the fleet, on the stretch of the boot nobody can reach the
daemon (claunch-t8bgj). The relaunches now skip it and the one persist at
the end of ``restore_all`` writes the whole fleet.

The boot and the shutdown also report their slow parts in the log, so the
next slow restart is read off the log rather than re-measured.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import __main__ as daemon_main
from claude_launcher.daemon import db
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import Session

CHILD = "import sys\nprint('READY')\nsys.stdin.readline()\n"


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


class _CountingManager(SessionManager):
    """Counts persists; the launch path is the real one minus the process."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.persists = 0

    def persist(self) -> None:
        self.persists += 1
        super().persist()


def _seed(names, exited=()) -> None:
    entries = [
        {
            "def": SessionDef(name=n, harness="py", restore=True).to_dict(),
            "was_running": True,
            "created_at": f"2026-10-09T00:00:0{i}+00:00",
        }
        for i, n in enumerate(names)
    ]
    entries += [
        {
            "def": SessionDef(name=n, harness="py").to_dict(),
            "was_running": False,
            "exit_code": 0,
        }
        for n in exited
    ]
    db.open_default().save(entries, prune=True, allow_empty=True)


def test_restore_persists_once_for_the_whole_fleet(home, monkeypatch):
    _register_py_harness()
    _seed(["s1", "s2", "s3"], exited=["old1", "old2"])
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)

    async def run():
        mgr = _CountingManager(idle_threshold=0.5, scrollback=100, restore_default=True)
        assert mgr.restore_all() == []
        assert mgr.persists == 1
        # The one persist still writes every record, relaunched and exited.
        names = {e["def"]["name"] for e in db.open_default().load_all()}
        assert names == {"s1", "s2", "s3", "old1", "old2"}

    asyncio.run(run())


def test_a_relaunch_outside_a_restore_still_persists(home, monkeypatch):
    """Respawn, wake and redefine relaunch with ``restoring=True`` too. They
    still persist, but the ones that land within
    :attr:`SessionManager.PERSIST_COALESCE_SECONDS` of each other -- the
    restore queue and the use-wakes after a restart -- share one write
    (claunch-7l7b7)."""
    _register_py_harness()
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)
    monkeypatch.setattr(SessionManager, "PERSIST_COALESCE_SECONDS", 0.05)

    async def run():
        mgr = _CountingManager(idle_threshold=0.5, scrollback=100, restore_default=True)
        for name in ("s7", "s8", "s9"):
            mgr.create(SessionDef(name=name, harness="py", restore=True), restoring=True)
        assert mgr.persists == 0
        await asyncio.sleep(0.2)
        assert mgr.persists == 1
        names = {e["def"]["name"] for e in db.open_default().load_all()}
        assert names == {"s7", "s8", "s9"}

    asyncio.run(run())


def test_persist_soon_without_a_loop_persists_at_once(home):
    mgr = _CountingManager(idle_threshold=0.5, scrollback=100, restore_default=True)
    mgr.persist_soon()
    assert mgr.persists == 1


def test_any_persist_takes_the_place_of_a_pending_one(home, monkeypatch):
    """A direct persist (shutdown_all's first of all) writes everything the
    pending one would have, so the pending one does not run after it."""
    _register_py_harness()
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)
    monkeypatch.setattr(SessionManager, "PERSIST_COALESCE_SECONDS", 0.05)

    async def run():
        mgr = _CountingManager(idle_threshold=0.5, scrollback=100, restore_default=True)
        mgr.create(SessionDef(name="s9", harness="py", restore=True), restoring=True)
        mgr.persist()
        assert mgr.persists == 1
        await asyncio.sleep(0.2)
        assert mgr.persists == 1
        assert [e["def"]["name"] for e in db.open_default().load_all()] == ["s9"]

    asyncio.run(run())


def test_a_pending_persist_keeps_the_restore_guard(home, monkeypatch):
    """The coalesced write is the same persist: while a restore is filling
    the set it must not prune the records not loaded yet."""
    _register_py_harness()
    _seed(["s1"], exited=["old1"])
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)
    monkeypatch.setattr(SessionManager, "PERSIST_COALESCE_SECONDS", 0.05)

    async def run():
        mgr = _CountingManager(idle_threshold=0.5, scrollback=100, restore_default=True)
        mgr.persist_soon()
        mgr._loading = True  # a restore began before the write came due
        try:
            await asyncio.sleep(0.2)
        finally:
            mgr._loading = False
        assert mgr.persists == 1
        names = {e["def"]["name"] for e in db.open_default().load_all()}
        assert names == {"s1", "old1"}

    asyncio.run(run())


def test_restore_logs_where_its_time_went(home, monkeypatch, caplog):
    _register_py_harness()
    _seed(["s1", "s2"])
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)
        with caplog.at_level(logging.INFO, logger="claude_launcher.daemon.manager"):
            mgr.restore_all()

    asyncio.run(run())
    lines = [r.getMessage() for r in caplog.records if "restore took" in r.getMessage()]
    assert len(lines) == 1
    assert "for 2 session(s)" in lines[0]
    for part in ("prepare/replay", "spawn", "persist", "other"):
        assert part in lines[0]


def test_teardown_names_the_slow_steps(monkeypatch, caplog):
    monkeypatch.setattr(daemon_main, "SLOW_TEARDOWN_STEP", 0.05)

    async def run():
        teardown = daemon_main._Teardown()
        await teardown.step("quick", asyncio.sleep(0))
        await teardown.step("slow", asyncio.sleep(0.1))
        teardown.report()

    with caplog.at_level(logging.INFO, logger=daemon_main.log.name):
        asyncio.run(run())
    messages = [r.getMessage() for r in caplog.records]
    assert any("shutdown step 'slow' took" in m for m in messages)
    assert not any("'quick'" in m for m in messages)
    summary = [m for m in messages if m.startswith("shutdown teardown took")]
    assert len(summary) == 1 and "; slow " in summary[0]


def test_a_failing_teardown_step_is_still_timed(caplog, monkeypatch):
    """A step that raises is still named when slow; the error propagates to
    the caller exactly as the bare ``await`` it replaced did."""
    monkeypatch.setattr(daemon_main, "SLOW_TEARDOWN_STEP", 0.0)

    async def boom():
        raise RuntimeError("x")

    async def run():
        teardown = daemon_main._Teardown()
        try:
            await teardown.step("boom", boom())
        except RuntimeError:
            return True
        return False

    with caplog.at_level(logging.INFO, logger=daemon_main.log.name):
        assert asyncio.run(run())
    assert any("'boom'" in r.getMessage() for r in caplog.records)


def test_a_persist_from_a_worker_thread_leaves_the_pending_one(home, monkeypatch):
    """The model-reconcile clock persists on a worker thread; it must not
    cancel the loop's timer from there. The pending write still runs."""
    _register_py_harness()
    monkeypatch.setattr(Session, "start", lambda self, argv, env, cwd: None)
    monkeypatch.setattr(SessionManager, "PERSIST_COALESCE_SECONDS", 0.05)

    class _ThreadTaggingManager(SessionManager):
        """Records which thread each persist ran on. The timer may fire while
        the worker-thread persist is still running on a loaded machine, so
        the two writes are told apart by thread, not by order
        (claunch-uojc9)."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.threads: list[int] = []

        def persist(self) -> None:
            self.threads.append(threading.get_ident())
            super().persist()

    async def run():
        mgr = _ThreadTaggingManager(
            idle_threshold=0.5, scrollback=100, restore_default=True
        )
        mgr.create(SessionDef(name="s9", harness="py", restore=True), restoring=True)
        loop_thread = threading.get_ident()
        await asyncio.to_thread(mgr.persist)
        deadline = time.monotonic() + 10
        while loop_thread not in mgr.threads and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        on_loop = [t for t in mgr.threads if t == loop_thread]
        assert len(mgr.threads) - len(on_loop) == 1  # the worker-thread one
        assert len(on_loop) == 1  # the pending write was not cancelled

    asyncio.run(run())
