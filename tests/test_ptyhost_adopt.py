"""A daemon restart that keeps its sessions (claunch-xnnct.1).

In-process: one :class:`SessionManager` goes down with
``shutdown_all(mode=...)`` and a second one, on the same home and the same
PTY host, runs ``restore_all`` -- the two halves of a restart. The last two
tests run real daemon processes: the RESTART_CODE self-restart
(``POST /api/daemon/restart``) and a daemon killed with SIGKILL.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the PTY host is POSIX only")

import claude_launcher  # noqa: E402
from claude_launcher import lineage, profile, store  # noqa: E402
from claude_launcher.daemon import pty_backend, paths  # noqa: E402
from claude_launcher.daemon import host_watch  # noqa: E402
from claude_launcher.daemon import __main__ as daemon_main  # noqa: E402
from claude_launcher.daemon.harness import SessionDef  # noqa: E402
from claude_launcher.daemon.manager import HostRelaunch, SessionManager  # noqa: E402
from claude_launcher.daemon import restore_queue  # noqa: E402
from claude_launcher.daemon.ptyhost import client as hc  # noqa: E402
from claude_launcher.daemon import session as session_mod  # noqa: E402
from claude_launcher.daemon.session import DeadSession  # noqa: E402

#: Turns bracketed paste on (a mode the next daemon must keep), echoes
#: lines, and after ``later:X`` prints ``late:X`` (output made while no
#: daemon is attached), after ``quit-later`` exits with 3 -- each once a
#: file named ``go`` appears in its cwd, which the test makes after the
#: daemon let go. A fixed delay raced a slow shutdown on a loaded machine.
#: ``size`` prints the size the program's terminal has.
CHILD = (
    "import os, sys, threading, time\n"
    "sys.stdout.write('\\x1b[?2004hREADY\\n'); sys.stdout.flush()\n"
    "def later(text, code=None):\n"
    "    deadline = time.time() + 60\n"
    "    while not os.path.exists('go') and time.time() < deadline:\n"
    "        time.sleep(0.05)\n"
    "    print('late:' + text, flush=True)\n"
    "    if code is not None:\n"
    "        os._exit(code)\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line.startswith('later:'):\n"
    "        threading.Thread(target=later, args=(line[6:],)).start()\n"
    "    elif line == 'quit-later':\n"
    "        threading.Thread(target=later, args=('bye', 3)).start()\n"
    "    elif line == 'size':\n"
    "        print('size:%dx%d' % tuple(os.get_terminal_size(0)), flush=True)\n"
    "    else:\n"
    "        print('echo:' + line, flush=True)\n"
)


def _register():
    store.update(lambda doc: doc.update(
        {"harnesses": {"ph": {"command": [sys.executable, "-u", "-c", CHILD]}}}))
    if not profile.resolve("ph").exists():
        lineage.set_harness(profile.create("ph"), "ph")


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _wait(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


async def _text(session) -> str:
    await session.screen_synced()
    return "\n".join(session.capture(history=True) + session.capture())


async def _wait_screen(session, needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in await _text(session):
            return
        await asyncio.sleep(0.1)
    log_tail = paths.session_log(session.sdef.name).read_bytes()[-2000:]
    raise AssertionError(f"{needle!r} never appeared; got:\n{await _text(session)}\n"
                         f"log: {log_tail!r}\nexited: {session.exited} {session.exit_code}")


@pytest.fixture
def host(home, monkeypatch):
    """A PTY host for this home, installed the way the daemon installs it."""
    c = hc.ensure_host(paths.ptyhost_dir(), idle_exit=2.0)
    monkeypatch.setattr(pty_backend, "_host", c)
    yield c
    pty_backend.use_host(None)
    _end_host()


def _end_host():
    directory = paths.ptyhost_dir()
    if hc.host_alive(directory) is None:
        return
    try:
        hc.HostClient.connect(directory).shutdown_host()
    except hc.HostUnavailable:
        pid = hc.host_alive(directory)
        if pid:
            os.kill(pid, signal.SIGTERM)
    pid = hc.host_alive(directory)
    assert pid is None or _wait(lambda: not _pid_alive(pid)), "host did not exit"


def _reconnect(monkeypatch) -> hc.HostClient:
    """The next daemon's connection (``_connect_pty_host``), which also
    retries while the host still answers busy for the previous client."""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            c = hc.HostClient.connect(paths.ptyhost_dir())
            break
        except hc.HostUnavailable as exc:
            if "busy" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    monkeypatch.setattr(pty_backend, "_host", c)
    return c


def test_keep_restart_adopts_the_running_session(host, tmp_path, monkeypatch):
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="a1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        await s.send_keys(["before", "Enter"])
        await _wait_screen(s, "echo:before")
        await s.send_keys(["later:away", "Enter"])
        await _wait_screen(s, "later:away")  # the line reached the child
        pid = s.pid
        # Let the status settle to idle before the shutdown reads it for
        # was_busy: the last screen write is younger than idle_threshold.
        await asyncio.sleep(1.0)
        await mgr.shutdown_all(mode="keep")
        host.close()
        (tmp_path / "go").touch()
        assert not s.exited and s.detached_pty["seq"] > 0
        # A kept session did not end, so nobody is recorded as its ender
        # and no exit event is written (claunch-o6xws).
        assert getattr(s, "ended_by", None) is None
        assert "exit" not in [e.get("kind") for e in mgr.events.rows(s)]
        assert s.detached_pty["modes"]["bracketed_paste"] is True
        # The child writes while no daemon is attached.
        await asyncio.sleep(1.0)
        assert _pid_alive(pid)
        assert b"late:away" not in paths.session_log("a1").read_bytes()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        t0 = time.monotonic()
        assert mgr2.restore_all() == []
        took = time.monotonic() - t0
        s2 = mgr2.get("a1")
        assert mgr2.adopted == ["a1"]
        assert s2.pid == pid and not s2.exited
        # Idle at the shutdown, an adopted session joins the resume
        # nudge's audience so a run waiting on it gets its wake-up
        # (claunch-slia4) — same list a relaunched idle one lands on.
        assert "a1" not in mgr2.resumed_busy
        assert list(mgr2.resumed_idle) == ["a1"]
        assert "a1" not in mgr2.restore_queue
        log_bytes = paths.session_log("a1").read_bytes()
        assert log_bytes.count(b"late:away") == 1 and log_bytes.count(b"READY") == 1
        await _wait_screen(s2, "echo:before")
        await _wait_screen(s2, "late:away")
        assert s2.screen.bracketed_paste is True
        await s2.send_keys(["after", "Enter"])
        await _wait_screen(s2, "echo:after")
        kinds = [e.get("kind") for e in mgr2.events.rows(s2)]
        assert "adopt" in kinds and "exit" not in kinds
        assert getattr(s2, "ended_by", None) is None
        print(f"restore_all with one adopted session: {took * 1000:.0f} ms")
        await mgr2.shutdown_all(mode="stop")
        assert _wait(lambda: not _pid_alive(pid))

    asyncio.run(run())


def test_the_boot_adopts_with_the_screen_built_off_the_loop(host, tmp_path, monkeypatch):
    """The boot's restore (restore_all_async) builds the adopted screen on a
    worker thread (claunch-opirg); what the session shows and accepts after
    it is the same as the inline restore above."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        await s.send_keys(["before", "Enter"])
        await _wait_screen(s, "echo:before")
        pid = s.pid
        await asyncio.sleep(1.0)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        assert await mgr2.restore_all_async() == []
        s2 = mgr2.get("t1")
        assert mgr2.adopted == ["t1"]
        assert s2.pid == pid and not s2.exited
        assert s2.screen.bracketed_paste is True  # the saved modes, applied
        await _wait_screen(s2, "echo:before")
        log_bytes = paths.session_log("t1").read_bytes()
        assert log_bytes.count(b"READY") == 1
        await s2.send_keys(["after", "Enter"])
        await _wait_screen(s2, "echo:after")
        await mgr2.shutdown_all(mode="stop")
        assert _wait(lambda: not _pid_alive(pid))

    asyncio.run(run())


async def _restore_with_slow_build(mgr, monkeypatch, during):
    """Run the boot's restore with every screen build held for a second,
    and call ``during(session)`` on the loop while the first one is held:
    the window a verb can reach a staged, not yet attached session."""
    from claude_launcher.daemon import manager as manager_mod
    real = manager_mod._Adoption.build

    def slow(self):
        time.sleep(1.0)
        real(self)

    monkeypatch.setattr(manager_mod._Adoption, "build", slow)
    restore = asyncio.ensure_future(mgr.restore_all_async())
    while True:
        staged = mgr._sessions.get("t1")
        if staged is not None and staged.pty is None and not staged.exited:
            break
        assert not restore.done(), "restore finished before the build was reached"
        await asyncio.sleep(0.01)
    during(staged)
    assert await restore == []
    return staged


@pytest.mark.parametrize("verb", ["kill", "pause"])
def test_an_ending_asked_while_the_screen_is_built_ends_the_program(
    host, tmp_path, monkeypatch, verb
):
    """A kill or pause that reaches a session between its stage and its
    attach -- the loop runs while the boot builds its screen off the loop
    (claunch-opirg) -- is owed to the attach, not dropped: the program ends
    and the record reads as that verb."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        staged = await _restore_with_slow_build(
            mgr2, monkeypatch, lambda _s: getattr(mgr2, verb)("t1"),
        )
        assert mgr2.get("t1") is staged
        for _ in range(100):
            if staged.exited:
                break
            await asyncio.sleep(0.05)
        assert staged.exited and staged.kill_requested
        assert bool(getattr(staged, "paused_at", None)) is (verb == "pause")
        assert _wait(lambda: not _pid_alive(pid))
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_input_and_a_resize_while_the_screen_is_built_reach_the_program(
    host, tmp_path, monkeypatch
):
    """A viewer that types or resizes between the stage and the attach is
    held for the attach, not told the running session has exited: the keys
    reach the program and the size is the one the screen and program get."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        typed = {}

        def during(staged):
            staged.resize(100, 30)
            typed["task"] = asyncio.ensure_future(staged.send_keys(["during", "Enter"]))

        staged = await _restore_with_slow_build(mgr2, monkeypatch, during)
        await typed["task"]
        await _wait_screen(staged, "echo:during")
        assert (staged.screen.cols, staged.screen.rows) == (100, 30)
        assert (staged.sdef.cols, staged.sdef.rows) == (100, 30)
        await staged.send_keys(["size", "Enter"])
        await _wait_screen(staged, "size:100x30")
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_an_archive_asked_while_the_screen_is_built_files_the_ended_session(
    host, tmp_path, monkeypatch
):
    """stop_and_archive between the stage and the attach waits for the
    attach, ends the program and files the record -- the archive is not
    refused as "still running" while the program ends anyway."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        asked = {}
        await _restore_with_slow_build(
            mgr2, monkeypatch,
            lambda _s: asked.update(task=asyncio.ensure_future(mgr2.stop_and_archive("t1"))),
        )
        archived = await asked["task"]
        assert archived.exited and archived.sdef.name == "t1"
        assert archived.archived_at
        assert _wait(lambda: not _pid_alive(pid))
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_kill_while_the_screen_is_built_survives_a_failed_adoption(
    host, tmp_path, monkeypatch
):
    """When the adoption of a session killed during its build fails, the
    record is retired as ended, not relaunched: a relaunch would undo the
    kill."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()

        def fail(self, work):
            raise RuntimeError("adoption failed")

        monkeypatch.setattr(SessionManager, "_adopt_finish", fail)
        await _restore_with_slow_build(mgr2, monkeypatch, lambda _s: mgr2.kill("t1"))
        after = mgr2.get("t1")
        assert isinstance(after, DeadSession) and after.exited
        assert _wait(lambda: not _pid_alive(pid))
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_persist_while_the_screen_is_built_keeps_the_host_session(
    host, tmp_path, monkeypatch
):
    """A persist on the loop while the screen is built (any other session's
    event writes the registry) still records where the kept program is:
    the record the previous daemon left, so a daemon that goes down here
    adopts it next time instead of relaunching it."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        seen = {}
        staged = await _restore_with_slow_build(
            mgr2, monkeypatch, lambda st: seen.update(rec=st.pty_record()),
        )
        assert seen["rec"]["pid"] == pid and seen["rec"]["seq"] is not None
        assert staged.adopting is None and staged.pid == pid
        await mgr2.shutdown_all(mode="stop")
        assert _wait(lambda: not _pid_alive(pid))

    asyncio.run(run())


def test_a_record_replaced_while_the_screen_is_built_is_left_alone(
    host, tmp_path, monkeypatch
):
    """A record replaced between the stage and the attach (a redefine, an
    archive's DeadSession) is the new owner of the name: the adoption does
    not hand the program to the staged session it no longer lists, and the
    host session it was going to adopt is ended (claunch-opirg.1)."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="t1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        await asyncio.sleep(0.5)
        await mgr.shutdown_all(mode="keep")
        host.close()

        _reconnect(monkeypatch)
        mgr2 = _manager()
        new = {}

        def replace(staged):
            new["rec"] = DeadSession(staged.sdef, exit_code=0)
            mgr2._sessions["t1"] = new["rec"]

        staged = await _restore_with_slow_build(mgr2, monkeypatch, replace)
        assert mgr2.get("t1") is new["rec"]
        assert mgr2.adopted == []
        assert staged.adopting is None and staged.pty is None
        assert _wait(lambda: not _pid_alive(pid))
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_busy_adopted_session_joins_the_nudge_audience(host, tmp_path, monkeypatch):
    """``was_busy`` decides which resume-nudge list an adopted session
    lands on (claunch-slia4): a busy one joins ``resumed_busy`` the way a
    relaunched one does, so a run its interrupted turn was driving gets a
    wake-up after all."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="b1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        # What persist() reads at the shutdown, without faking a turn on
        # a dumb PTY child: the record's was_busy comes from the manager's
        # delayed-busy set as well as the live status.
        mgr._delayed_busy.add("b1")
        await mgr.shutdown_all(mode="keep")
        host.close()
        (tmp_path / "go").touch()
        assert not s.exited

        _reconnect(monkeypatch)
        mgr2 = _manager()
        assert mgr2.restore_all() == []
        s2 = mgr2.get("b1")
        assert mgr2.adopted == ["b1"]
        assert "b1" in mgr2.resumed_busy
        assert "b1" not in mgr2.resumed_idle
        await mgr2.shutdown_all(mode="stop")
        assert _wait(lambda: not _pid_alive(pid))

    asyncio.run(run())


def test_a_sampler_tick_during_detach_does_not_file_the_session_as_exited(
        host, tmp_path, monkeypatch):
    """A detached RemotePty answers isalive() False. The 2026-10-15 targeted
    run caught the idle sampler ticking while detach() joined the reader:
    its liveness poll called _finish(), persist wrote was_running False,
    and the next daemon ended the still-running child as an orphan."""
    _register()
    monkeypatch.setattr(session_mod, "SAMPLE_INTERVAL", 0.02)

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="r1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        reader = s._reader
        join = reader.join

        def slow_join(timeout=None):
            time.sleep(0.5)  # 25 sampler intervals
            join(timeout)

        monkeypatch.setattr(reader, "join", slow_join)
        pid = s.pid
        await mgr.shutdown_all(mode="keep")
        assert not s.exited and s.exited_at is None
        assert s.detached_pty is not None
        host.close()
        assert _pid_alive(pid)

        _reconnect(monkeypatch)
        mgr2 = _manager()
        mgr2.restore_all()
        assert mgr2.adopted == ["r1"] and mgr2.get("r1").pid == pid
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_full_restart_ends_sessions_and_host_and_relaunches(host, tmp_path, monkeypatch):
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="f1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        await mgr.shutdown_all(mode="full")
        daemon_main._close_pty_host(host, "full")
        assert s.exited and _wait(lambda: not _pid_alive(pid))
        assert _wait(lambda: not _pid_alive(host_pid)), "the host outlived a full restart"

        c2 = hc.ensure_host(paths.ptyhost_dir(), idle_exit=2.0)
        monkeypatch.setattr(pty_backend, "_host", c2)
        mgr2 = _manager()
        mgr2.restore_all()
        s2 = mgr2.get("f1")
        assert mgr2.adopted == [] and not s2.exited and s2.pid != pid
        assert c2.host_pid != host_pid
        await _wait_screen(s2, "READY")
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_exit_while_the_daemon_is_down_is_filed_with_the_host_code(host, tmp_path, monkeypatch):
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="x1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        await s.send_keys(["quit-later", "Enter"])
        await _wait_screen(s, "quit-later")
        pid = s.pid
        await mgr.shutdown_all(mode="keep")
        host.close()
        (tmp_path / "go").touch()
        assert _wait(lambda: not _pid_alive(pid), timeout=10.0)

        c2 = _reconnect(monkeypatch)
        assert _wait(lambda: any(r["exited"] for r in c2.list()))
        mgr2 = _manager()
        mgr2.restore_all()
        rec = mgr2.get("x1")
        assert isinstance(rec, DeadSession) and rec.exited
        assert rec.exit_code == 3 and rec.failed_at
        # Nothing asked for it, so its own program ended it (claunch-o6xws).
        assert rec.ended_by["kind"] == "self"
        assert mgr2.adopted == [] and "x1" not in mgr2.restore_queue
        assert c2.list() == [], "the exit was not acknowledged"
        assert b"late:bye" in paths.session_log("x1").read_bytes()
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_host_session_no_record_owns_is_ended(host, tmp_path, monkeypatch):
    _register()

    async def run():
        stray = host.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                           env=dict(os.environ), cwd=str(tmp_path), cols=80, rows=24,
                           name="nobody")
        mgr = _manager()
        s = mgr.create(SessionDef(name="o1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        stray.detach()
        await mgr.shutdown_all(mode="keep")
        host.close()

        c2 = _reconnect(monkeypatch)
        mgr2 = _manager()
        mgr2.restore_all()
        assert mgr2.adopted == ["o1"]
        assert _wait(lambda: not _pid_alive(stray.pid)), "the stray session kept running"
        assert [r["name"] for r in c2.list()] == ["o1"]
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_daemon_that_died_without_detaching_is_adopted_from_the_ring(host, tmp_path, monkeypatch):
    """A killed daemon detaches nothing: the record has a sid and pid but no
    seq. The screen comes from the host's ring; the log is left as it is."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="k1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid = s.pid
        mgr.persist()
        # What SIGKILL leaves: the connection drops, nothing else runs.
        mgr.shutting_down = True
        host._sock.shutdown(socket.SHUT_RDWR)
        assert _wait(lambda: host.closed.is_set())
        await asyncio.sleep(0.5)
        assert _pid_alive(pid)

        _reconnect(monkeypatch)
        mgr2 = _manager()
        mgr2.restore_all()
        s2 = mgr2.get("k1")
        assert mgr2.adopted == ["k1"] and s2.pid == pid
        await _wait_screen(s2, "READY")
        await s2.send_keys(["after-kill", "Enter"])
        await _wait_screen(s2, "echo:after-kill")
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_setting_off_ends_a_leftover_host_and_resumes(host, tmp_path, monkeypatch):
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="n1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        await mgr.shutdown_all(mode="keep")
        host.close()
        monkeypatch.setattr(pty_backend, "_host", None)

        assert daemon_main._connect_pty_host({"pty_host": False}) is None
        assert _wait(lambda: not _pid_alive(host_pid)) and not _pid_alive(pid)
        mgr2 = _manager()
        mgr2.restore_all()
        s2 = mgr2.get("n1")
        assert mgr2.adopted == [] and not s2.exited and s2.pid != pid
        assert not isinstance(s2.pty, hc.RemotePty)
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def _refuse_us():
    """Make the running host refuse the next daemon, as a host of another
    protocol version does: its token no longer matches."""
    from claude_launcher.daemon.ptyhost import token_path
    real = token_path(paths.ptyhost_dir()).read_text(encoding="ascii")
    token_path(paths.ptyhost_dir()).write_text("0" * 64, encoding="ascii")
    return real


def test_a_host_that_refuses_the_daemon_is_ended_before_its_sessions_relaunch(
        host, tmp_path, monkeypatch):
    """s430's review (claunch-xnnct.1): a refusing host still runs its
    sessions, and a daemon that spawned them itself ran each conversation
    twice. The host is ended by signal, a fresh one started, and each record
    relaunched once -- with no board sweep (SESSION ENDED): the restart ended
    it, not the session."""
    _register()
    monkeypatch.setattr(pty_backend, "_stranded", None)

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="v1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        await mgr.shutdown_all(mode="keep")
        host.close()
        monkeypatch.setattr(pty_backend, "_host", None)
        _refuse_us()

        c2 = daemon_main._connect_pty_host({"pty_host": True})
        assert c2 is not None and c2.host_pid != host_pid
        assert not _pid_alive(host_pid) and _wait(lambda: not _pid_alive(pid))
        assert pty_backend.stranded_host() is None
        mgr2 = _manager()
        mgr2.restore_all()
        s2 = mgr2.get("v1")
        assert mgr2.adopted == [] and not s2.exited and s2.pid != pid
        assert isinstance(s2.pty, hc.RemotePty)
        assert [r["pid"] for r in c2.list()] == [s2.pid], "the session runs once"
        assert mgr2.take_retired_for_sweep() == []
        await _wait_screen(s2, "READY")
        await mgr2.shutdown_all(mode="stop")
        daemon_main._close_pty_host(c2, "stop")

    asyncio.run(run())


def test_a_host_that_cannot_be_ended_keeps_its_sessions_unrelaunched(
        host, tmp_path, monkeypatch):
    _register()
    monkeypatch.setattr(pty_backend, "_stranded", None)
    monkeypatch.setattr(hc, "stop_refusing_host", lambda directory, **kw: False)

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="w1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        await mgr.shutdown_all(mode="keep")
        host.close()
        monkeypatch.setattr(pty_backend, "_host", None)
        real = _refuse_us()

        assert daemon_main._connect_pty_host({"pty_host": True}) is None
        assert pty_backend.stranded_host() == host_pid
        mgr2 = _manager()
        mgr2.restore_all()
        rec = mgr2.get("w1")
        assert isinstance(rec, DeadSession) and _pid_alive(pid)
        assert "w1" not in mgr2.restore_queue and mgr2.take_retired_for_sweep() == []
        assert any(e.get("kind") == "restore" for e in mgr2.events.rows(rec))

        from claude_launcher.daemon.ptyhost import token_path
        token_path(paths.ptyhost_dir()).write_text(real, encoding="ascii")
        _reconnect(monkeypatch).shutdown_host()
        assert _wait(lambda: not _pid_alive(pid))

    asyncio.run(run())


async def _until_true(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def test_a_host_sigterm_relaunches_its_sessions_like_a_restart(host, tmp_path, monkeypatch):
    """The host goes away under a running daemon (claunch-hor3b.1): its
    sessions are not filed as failed, and the watch brings them back in a
    new host as a restart without the host would."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="h1", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        passes = []
        watcher = asyncio.create_task(host_watch.watch(
            mgr, lambda: hc.ensure_host(paths.ptyhost_dir(), idle_exit=2.0),
            on_relaunch=passes.append, interval=0.05))
        try:
            os.kill(host_pid, signal.SIGTERM)
            assert await _until_true(lambda: mgr.get("h1") is not s and not mgr.get("h1").exited)
            s2 = mgr.get("h1")
            assert s.exited and s.ended_by_host
            assert getattr(s, "failed_at", None) is None
            assert s.ended_by == {"kind": "daemon", "reason": "pty-host-ended"}
            assert "fail" not in [e.get("kind") for e in mgr.events.rows(s)]
            new = pty_backend.installed_host()
            assert new is not None and new.host_pid != host_pid
            assert s2.pid != pid
            await _wait_screen(s2, "READY")
            # Through the boot's own judgement (_restore_entry), so it lands
            # on the lists a boot hands its resume nudge -- this pass's share.
            assert ("resume", "PTY host 종료 후 세션 복원") in [
                (e.get("kind"), e.get("text")) for e in mgr.events.rows(s2)]
            assert await _until_true(lambda: bool(passes))
            assert passes[0].relaunched == ["h1"] and list(passes[0].idle) == ["h1"]
            log_text = (paths.ptyhost_dir() / "ptyhost.log").read_text(encoding="utf-8")
            assert "stopping (SIGTERM); ending 1 live session(s)" in log_text
        finally:
            watcher.cancel()
            await mgr.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_session_the_lost_host_took_is_restored_at_the_next_boot(host, tmp_path, monkeypatch):
    """No watch relaunched it (the daemon went down first): the record still
    reads running, so the next boot's restore_all brings it back -- a lost
    connection (SIGKILL: no word from the host) counts like a stop."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="h2", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        pid, host_pid = s.pid, host.host_pid
        os.kill(host_pid, signal.SIGKILL)
        assert await _until_true(lambda: s.exited)
        assert s.ended_by_host and getattr(s, "failed_at", None) is None
        assert mgr.host_ended() == ["h2"]
        assert _wait(lambda: not _pid_alive(pid))

        c2 = hc.ensure_host(paths.ptyhost_dir(), idle_exit=2.0)
        monkeypatch.setattr(pty_backend, "_host", c2)
        mgr2 = _manager()
        assert mgr2.restore_all() == []
        s2 = mgr2.get("h2")
        assert not s2.exited and s2.pid != pid
        await _wait_screen(s2, "READY")
        await mgr2.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_kill_is_not_undone_by_the_host_going(host, tmp_path, monkeypatch):
    """An ending somebody asked for stays theirs even when the host goes
    down in the same moment."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="h3", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        s.kill_requested = True
        os.kill(host.host_pid, signal.SIGTERM)
        assert await _until_true(lambda: s.exited)
        assert s.ended_by_host and mgr.host_ended() == []
        assert await mgr.relaunch_host_ended() == HostRelaunch()
        assert mgr.get("h3") is s

    asyncio.run(run())


def test_a_no_restore_session_ends_with_the_host_as_at_a_restart(host, tmp_path, monkeypatch):
    """restore off: a restart would not bring it back, so neither does the
    host's going (review of claunch-hor3b.1). It ends -- not failed, and its
    exit hooks run, as for any ending."""
    _register()

    async def run():
        mgr = _manager()
        ended = []
        mgr.exit_hooks.append(lambda sess: ended.append(sess.sdef.name))
        s = mgr.create(SessionDef(name="h4", harness="ph", cwd=str(tmp_path), restore=False))
        await _wait_screen(s, "READY")
        os.kill(host.host_pid, signal.SIGTERM)
        assert await _until_true(lambda: s.exited)
        assert s.ended_by_host and getattr(s, "failed_at", None) is None
        assert s.ended_by == {"kind": "daemon", "reason": "pty-host-ended"}
        assert ended == ["h4"]
        assert mgr.host_ended() == []
        assert await mgr.relaunch_host_ended() == HostRelaunch()
        assert mgr._registry_entry(s)["was_running"] is False
        assert "PTY host 종료로 세션 프로세스 종료 (restore 꺼짐)" in [
            e.get("text") for e in mgr.events.rows(s)]

    asyncio.run(run())


def test_a_resumable_session_the_host_took_waits_in_a_restore_queue(host, tmp_path, monkeypatch):
    """With restore_stagger set, a boot files a resumable record delayed for
    its restore queue instead of relaunching the fleet at once; the host's
    going does the same, and a queue over that pass's names wakes it."""
    _register()
    monkeypatch.setattr(SessionManager, "_resumable_def", staticmethod(lambda sdef: True))

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True,
                             restore_stagger=0.01)
        s = mgr.create(SessionDef(name="h5", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        os.kill(host.host_pid, signal.SIGTERM)
        assert await _until_true(lambda: s.exited)
        monkeypatch.setattr(pty_backend, "_host",
                            hc.ensure_host(paths.ptyhost_dir(), idle_exit=2.0))
        out = await mgr.relaunch_host_ended()
        assert out.delayed == ["h5"] and out.relaunched == [] and list(out.idle) == ["h5"]
        dead = mgr.get("h5")
        assert dead.exited and dead.delayed_at and not getattr(dead, "failed_at", None)
        queue = restore_queue.RestoreQueue(mgr, interval=0.0, names=out.delayed)
        queue.start()
        assert await _until_true(lambda: not mgr.get("h5").exited)
        assert queue.started == ["h5"]
        await _wait_screen(mgr.get("h5"), "READY")
        await queue.stop()
        await mgr.shutdown_all(mode="stop")

    asyncio.run(run())


def test_a_program_that_outlived_the_host_is_not_run_twice(host, tmp_path, monkeypatch):
    """A host killed outright can leave its child running (review of
    claunch-hor3b.1). A relaunch would run the conversation a second time;
    the record is left exited, as a boot leaves one stranded in a host it
    cannot reach."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="h6", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        os.kill(host.host_pid, signal.SIGKILL)
        assert await _until_true(lambda: s.exited)
        monkeypatch.setattr(SessionManager, "_outlived_host", staticmethod(lambda sess: True))
        assert mgr.outlived_host() == ["h6"]
        out = await mgr.relaunch_host_ended()
        assert out.running == ["h6"] and out.relaunched == []
        assert mgr.get("h6") is s and mgr.host_ended() == []
        assert mgr._registry_entry(s)["was_running"] is False

    asyncio.run(run())


def test_the_watch_stops_relaunching_a_host_that_keeps_going(monkeypatch):
    """Host losses past max_losses inside the window: a new host is still
    installed, but nothing is relaunched into it (review of
    claunch-hor3b.1) -- the records are left for the next boot."""
    import threading

    class _Client:
        def __init__(self, closed):
            self.closed = threading.Event()
            if closed:
                self.closed.set()

    installed = {"c": _Client(True)}
    monkeypatch.setattr(pty_backend, "installed_host", lambda: installed["c"])
    monkeypatch.setattr(pty_backend, "use_host", lambda c: installed.update(c=c))
    # Two more lost hosts, then one that stays.
    fresh = [_Client(True), _Client(True), _Client(False)]

    class _Mgr:
        shutting_down = False
        relaunches = 0

        def bound_to_host(self, client):
            return []

        def outlived_host(self):
            return []

        async def relaunch_host_ended(self):
            self.relaunches += 1
            return HostRelaunch()

    mgr = _Mgr()

    async def run():
        task = asyncio.create_task(host_watch.watch(
            mgr, lambda: fresh.pop(0), interval=0.01, settle=0.0,
            max_losses=2, loss_window=60.0))
        assert await _until_true(lambda: not fresh, timeout=5.0)
        await asyncio.sleep(0.05)
        task.cancel()
        assert mgr.relaunches == 2

    asyncio.run(run())


def test_a_record_that_will_not_parse_back_is_kept_and_filed_failed(
        host, tmp_path, monkeypatch):
    """The relaunch takes the record off the list before handing it to
    _restore_entry; a definition that does not parse back left nothing in
    its place, and the next persist dropped it from disk (claunch-hor3b.2).
    The record stays, filed failed."""
    _register()

    async def run():
        mgr = _manager()
        s = mgr.create(SessionDef(name="h7", harness="ph", cwd=str(tmp_path)))
        await _wait_screen(s, "READY")
        os.kill(host.host_pid, signal.SIGTERM)
        assert await _until_true(lambda: s.exited)

        def refuse(cls, data):
            raise ValueError("unreadable definition")

        monkeypatch.setattr(SessionDef, "from_dict", classmethod(refuse))
        out = await mgr.relaunch_host_ended()
        assert out.failed == ["h7"] and out.relaunched == [] and out.idle == {}
        assert mgr.get("h7") is s and getattr(s, "failed_at", None)
        assert "PTY host 종료 뒤 세션 복원 실패" in [e.get("text") for e in mgr.events.rows(s)]

    asyncio.run(run())


class _WatchClient:
    def __init__(self, closed):
        import threading
        self.closed = threading.Event()
        if closed:
            self.closed.set()


def test_the_watch_waits_for_an_outliving_program_before_relaunching(monkeypatch):
    """Settle waits while a lost host's program still runs (review of
    claunch-hor3b.1): relaunching before it ends would judge a session the
    hangup is about to end as one that outlived the host."""
    installed = {"c": _WatchClient(True)}
    monkeypatch.setattr(pty_backend, "installed_host", lambda: installed["c"])
    monkeypatch.setattr(pty_backend, "use_host", lambda c: installed.update(c=c))
    seen = []

    class _Mgr:
        shutting_down = False
        lingering = 3

        def bound_to_host(self, client):
            return []

        def outlived_host(self):
            self.lingering -= 1
            return ["x"] if self.lingering >= 0 else []

        async def relaunch_host_ended(self):
            seen.append(self.lingering)
            return HostRelaunch()

    mgr = _Mgr()

    async def run():
        task = asyncio.create_task(host_watch.watch(
            mgr, lambda: _WatchClient(False), interval=0.01, settle=30.0))
        assert await _until_true(lambda: seen, timeout=5.0)
        task.cancel()
        assert seen == [-1]

    asyncio.run(run())


def test_losses_older_than_the_window_do_not_count_toward_the_cap(monkeypatch):
    """The cap counts losses inside loss_window only: hosts lost further
    apart than that are each relaunched (review of claunch-hor3b.1)."""
    installed = {"c": _WatchClient(True)}
    monkeypatch.setattr(pty_backend, "installed_host", lambda: installed["c"])
    monkeypatch.setattr(pty_backend, "use_host", lambda c: installed.update(c=c))
    fresh = [_WatchClient(True), _WatchClient(True), _WatchClient(False)]

    class _Mgr:
        shutting_down = False
        relaunches = 0

        def bound_to_host(self, client):
            return []

        def outlived_host(self):
            return []

        async def relaunch_host_ended(self):
            self.relaunches += 1
            await asyncio.sleep(0.02)  # each loss lands outside the last one's window
            return HostRelaunch()

    mgr = _Mgr()

    async def run():
        task = asyncio.create_task(host_watch.watch(
            mgr, lambda: fresh.pop(0), interval=0.01, settle=0.0,
            max_losses=1, loss_window=0.01))
        assert await _until_true(lambda: not fresh, timeout=5.0)
        await asyncio.sleep(0.05)
        task.cancel()
        assert mgr.relaunches == 3

    asyncio.run(run())


def test_stop_refusing_host_signals_only_a_host(tmp_path):
    """The pid file is all it has to go on; a reused pid must not be shot."""
    from claude_launcher.daemon.ptyhost import pid_path
    d = tmp_path / "h"
    d.mkdir()
    pid_path(d).write_text(str(os.getpid()), encoding="ascii")
    assert hc.stop_refusing_host(d, timeout=0.5) is False
    pid_path(d).unlink()
    assert hc.stop_refusing_host(d, timeout=0.5) is True


def test_cli_shell_never_runs_in_the_host(host, tmp_path):
    from claude_launcher.daemon import clipty

    async def run():
        shell = clipty.ShellPty(argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                                cwd=str(tmp_path))
        assert shell.restart()
        try:
            assert not isinstance(shell._pty, hc.RemotePty)
            assert host.list() == []
        finally:
            await shell.shutdown()

    asyncio.run(run())


# -- real daemon processes ----------------------------------------------------------

def _daemon_env(home: Path) -> dict:
    env = os.environ.copy()
    src = str(Path(claude_launcher.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("CLAUNCH_SESSION", None)
    return env


#: A named instance: its own state directory and an ephemeral port.
INSTANCE = "adopt"


def _start_daemon(home: Path, log: Path) -> subprocess.Popen:
    with open(log, "ab") as fh:
        return subprocess.Popen(
            [sys.executable, "-m", "claude_launcher.daemon", "--name", INSTANCE],
            env=_daemon_env(home), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
            start_new_session=True,
        )


def _api(path: str, body=None, *, method="GET", timeout=5.0):
    doc = json.loads(paths.daemon_json().read_text(encoding="utf-8"))
    token = paths.token_file().read_text(encoding="utf-8").strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{doc['port']}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"{method} {path}: {exc.code} {exc.read()[:500]!r}") from None


def _serving_pid():
    try:
        doc = json.loads(paths.daemon_json().read_text(encoding="utf-8"))
        health = _api("/api/health", timeout=1.0)
    except Exception:
        return None
    return doc.get("pid") if health else None


def _session_row(name: str):
    rows = _api("/api/sessions")
    if isinstance(rows, dict):
        rows = rows.get("sessions") or []
    for row in rows:
        if row.get("name") == name:
            return row
    return None


def _screen(name: str) -> str:
    lines = []
    for query in ("format=json&history=1", "format=json"):
        lines += _api(f"/api/sessions/{name}/capture?{query}").get("lines") or []
    return "\n".join(lines)


def _type(name: str, text: str) -> None:
    _api(f"/api/sessions/{name}/keys", {"keys": [text, "Enter"]}, method="POST")


def _until(pred, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
        except Exception as exc:  # the daemon is between processes
            last = exc
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


@pytest.fixture
def live_daemon(home, tmp_path, monkeypatch):
    """A real daemon process with ``pty_host`` on, in this test's home."""
    monkeypatch.setenv("CLAUNCH_DAEMON", INSTANCE)
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _register()
    store.update(lambda doc: doc.setdefault("daemon", {}).update(
        {"pty_host": True, "idle_threshold": 0.5, "restore_stagger_seconds": 0}))
    log = tmp_path / "daemon.out"
    procs = [_start_daemon(home, log)]
    _until(_serving_pid, "the daemon to serve")
    yield procs, log
    try:
        if _serving_pid():
            _api("/api/daemon/shutdown", {"mode": "stop"}, method="POST")
    except Exception:
        pass
    for p in procs:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    pid = _serving_pid()
    if pid:
        os.kill(int(pid), signal.SIGKILL)
    _end_host()


def _create(name: str, cwd: Path) -> dict:
    _api("/api/sessions", {"name": name, "profile": "ph", "cwd": str(cwd),
                           "beads": False}, method="POST", timeout=30.0)
    _until(lambda: "READY" in _screen(name), f"{name} to draw")
    return _session_row(name)


def test_e2e_restart_code_self_restart_keeps_and_adopts(live_daemon, tmp_path):
    """POST /api/daemon/restart (keep): the daemon exits with RESTART_CODE,
    spawns its successor, and the successor adopts the same process."""
    procs, log = live_daemon
    row = _create("e1", tmp_path)
    child = row["pid"]
    host_pid = hc.host_alive(paths.ptyhost_dir())
    first = _serving_pid()
    _type("e1", "before")
    _until(lambda: "echo:before" in _screen("e1"), "the echo")

    assert _api("/api/daemon/restart", {"mode": "keep"}, method="POST")["mode"] == "keep"
    # _serve returned RESTART_CODE; main() spawned the successor and exits 0.
    assert procs[0].wait(timeout=60) == 0
    assert "spawning successor daemon" in Path(paths.log_file()).read_text(
        encoding="utf-8", errors="replace")
    second = _until(lambda: (p := _serving_pid()) and p != first and p, "the successor")
    assert hc.host_alive(paths.ptyhost_dir()) == host_pid
    assert _pid_alive(child)
    again = _until(lambda: _session_row("e1"), "the adopted session")
    assert again["pid"] == child
    _until(lambda: "echo:before" in _screen("e1"), "the old screen")
    _type("e1", "after")
    _until(lambda: "echo:after" in _screen("e1"), "input after the restart")
    assert "adopted 1 session(s) from the PTY host: e1" in Path(
        paths.log_file()).read_text(encoding="utf-8", errors="replace")
    os.kill(int(second), 0)


def test_e2e_killed_daemon_leaves_sessions_for_the_next_one(live_daemon, home, tmp_path):
    """SIGKILL the daemon: the host (its own session) and the child survive,
    and a new daemon adopts them."""
    procs, log = live_daemon
    row = _create("e2", tmp_path)
    child = row["pid"]
    host_pid = hc.host_alive(paths.ptyhost_dir())
    first = _serving_pid()
    os.kill(int(first), signal.SIGKILL)
    procs[0].wait(timeout=10)
    time.sleep(0.5)
    assert _pid_alive(host_pid) and _pid_alive(child)

    procs.append(_start_daemon(home, log))
    _until(lambda: (p := _serving_pid()) and p != first and p, "the new daemon", timeout=60)
    again = _until(lambda: _session_row("e2"), "the adopted session")
    assert again["pid"] == child
    assert hc.host_alive(paths.ptyhost_dir()) == host_pid
    _until(lambda: "READY" in _screen("e2"), "the screen from the ring")
    assert "adopted 1 session(s) from the PTY host: e2" in Path(
        paths.log_file()).read_text(encoding="utf-8", errors="replace")
    _type("e2", "after-kill")
    _until(lambda: "echo:after-kill" in _screen("e2"), "input after the kill")


def test_a_starting_daemon_waits_for_its_predecessor_to_let_go(host, monkeypatch):
    """The host serves one client; a successor that connects while the
    predecessor still holds it waits instead of starting without the host
    (which would relaunch every session the host still runs)."""
    import threading

    monkeypatch.setattr(pty_backend, "_host", None)
    threading.Timer(0.6, host.close).start()
    c = daemon_main._connect_pty_host({"pty_host": True})
    try:
        assert isinstance(c, hc.HostClient) and c.host_pid == host.host_pid
    finally:
        pty_backend.use_host(None)
        if c is not None:
            c.close()
