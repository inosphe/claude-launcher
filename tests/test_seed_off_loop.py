"""A restored session's scrollback is replayed off the event loop.

The replay of the previous run's log is pure-Python pyte work, and after a
restart the relaunches came one after another on the loop: every socket
waited for each of them (claunch-7l7b7). ``launch_async`` now builds the
replayed screen on a worker thread and hands it over with
:meth:`ScreenState.adopt`. These lock down that the result is the screen the
synchronous replay builds, that the readers who need the history wait for
it, and that what changed meanwhile survives the hand-over.
"""

from __future__ import annotations

import asyncio
import sys
import threading

from claude_launcher import lineage, profile, store
from claude_launcher.daemon import paths
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.session import Session

CHILD = "import sys\nprint('READY')\nsys.stdin.readline()\n"
ESC = chr(27)


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )
    if not profile.resolve("py").exists():
        lineage.set_harness(profile.create("py"), "py")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _write_log(name: str) -> None:
    body = ESC + "[?1049h" + ESC + "[?1h" + ESC + "[?2004h"
    body += "".join(f"old-line-{i}\r\n" for i in range(80))
    log = paths.session_log(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(body.encode())


def _snapshot(session: Session):
    screen = session.screen
    return (
        session.capture(history=True),
        session.capture(),
        screen.history_len,
        screen.history_limit,
        (screen.cols, screen.rows),
        (screen.app_cursor_keys, screen.bracketed_paste, screen.alt_screen,
         screen.mouse_tracking, screen.mouse_encoding),
    )


def test_off_loop_replay_builds_the_same_screen(home, tmp_path):
    _register_py_harness()
    _write_log("sync")
    _write_log("async")

    async def run():
        mgr = _manager()
        a = mgr.stage(SessionDef(name="sync", harness="py", cwd=str(tmp_path)))
        b = mgr.stage(SessionDef(name="async", harness="py", cwd=str(tmp_path)))
        try:
            a.seed_screen_from_log()
            screen = b.screen
            await b.seed_screen_from_log_async()
            assert b.screen is screen  # the object every reader holds
            assert _snapshot(a) == _snapshot(b)
            assert b.screen.history_len > 0
        finally:
            mgr.discard("sync")
            mgr.discard("async")

    asyncio.run(run())


def test_sliced_replay_matches_across_split_sequences(home, tmp_path):
    """The off-loop replay feeds the tail in slices; a slice boundary can
    fall inside an escape sequence or a multi-byte character, and the
    screen must come out the same as from one feed."""
    _register_py_harness()
    line = "".join(f"{ESC}[3{k % 8};1m단어{k}{ESC}[0m " for k in range(9))
    body = "".join(f"{i:05d} {line}\r\n" for i in range(400)).encode()
    assert len(body) > 8 * 4096
    for name in ("one", "sliced"):
        log = paths.session_log(name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(body)

    async def run():
        mgr = _manager()
        a = mgr.stage(SessionDef(name="one", harness="py", cwd=str(tmp_path)))
        b = mgr.stage(SessionDef(name="sliced", harness="py", cwd=str(tmp_path)))
        try:
            a.seed_screen_from_log()
            await b.seed_screen_from_log_async()
            assert _snapshot(a) == _snapshot(b)
            for offset in (0, 30, 200):
                assert a.screen.repaint_sequence(offset) == b.screen.repaint_sequence(offset)
        finally:
            mgr.discard("one")
            mgr.discard("sliced")

    asyncio.run(run())


def test_no_log_leaves_the_screen_alone(home, tmp_path):
    _register_py_harness()

    async def run():
        mgr = _manager()
        s = mgr.stage(SessionDef(name="fresh", harness="py", cwd=str(tmp_path)))
        try:
            before = _snapshot(s)
            await s.seed_screen_from_log_async()
            assert _snapshot(s) == before
            await asyncio.wait_for(s.screen_synced(), 1.0)
        finally:
            mgr.discard("fresh")

    asyncio.run(run())


def test_readers_of_the_history_wait_and_changes_meanwhile_survive(
    home, tmp_path, monkeypatch
):
    """A capture or an attaching viewer's repaint goes through
    ``screen_synced``: it must not answer with the history missing. A resize
    or a viewer focusing (a new history bound) during the replay applies to
    the replayed screen too."""
    _register_py_harness()
    _write_log("slow")
    gate = threading.Event()
    real = Session._replayed_screen

    def held(self, cols, rows, limit):
        gate.wait(5.0)
        return real(self, cols, rows, limit)

    monkeypatch.setattr(Session, "_replayed_screen", held)

    async def run():
        mgr = _manager()
        s = mgr.stage(SessionDef(name="slow", harness="py", cwd=str(tmp_path)))
        try:
            seeding = asyncio.create_task(s.seed_screen_from_log_async())
            await asyncio.sleep(0.05)
            synced = asyncio.create_task(s.screen_synced())
            await asyncio.sleep(0.05)
            assert not synced.done()
            cols, rows = s.screen.cols + 7, s.screen.rows + 3
            s.screen.resize(cols, rows)
            s.screen.set_history_limit(50)
            # The loop is free meanwhile: this coroutine ran while the
            # replay was still held.
            gate.set()
            await asyncio.wait_for(seeding, 5.0)
            await asyncio.wait_for(synced, 1.0)
            assert (s.screen.cols, s.screen.rows) == (cols, rows)
            assert s.screen.history_limit == 50
            assert 0 < s.screen.history_len <= 50
            assert "old-line-79" in "\n".join(s.capture(history=True))
        finally:
            gate.set()
            mgr.discard("slow")

    asyncio.run(run())


def test_launch_async_replays_off_the_loop(home, tmp_path, monkeypatch):
    _register_py_harness()
    _write_log("re")
    calls = []

    def sync_seed(self):
        raise AssertionError("launch_async replayed on the loop")

    async def start_async(self, argv, env, cwd):
        calls.append(self.screen.history_len)

    monkeypatch.setattr(Session, "seed_screen_from_log", sync_seed)
    monkeypatch.setattr(Session, "start_async", start_async)

    async def run():
        mgr = _manager()
        await mgr.create_async(
            SessionDef(name="re", harness="py", cwd=str(tmp_path), restore=True),
            restoring=True,
        )
        # Replayed before the spawn, as the synchronous launch does.
        assert calls and calls[0] > 0
        mgr.discard("re")

    asyncio.run(run())
