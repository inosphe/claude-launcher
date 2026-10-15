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
from claude_launcher.daemon import __main__ as daemon_main  # noqa: E402
from claude_launcher.daemon.harness import SessionDef  # noqa: E402
from claude_launcher.daemon.manager import SessionManager  # noqa: E402
from claude_launcher.daemon.ptyhost import client as hc  # noqa: E402
from claude_launcher.daemon.session import DeadSession  # noqa: E402

#: Turns bracketed paste on (a mode the next daemon must keep), echoes
#: lines, prints ``late:X`` a second after ``later:X`` (output made while no
#: daemon is attached) and exits with 3 a second after ``quit-later``.
CHILD = (
    "import sys, threading, time\n"
    "sys.stdout.write('\\x1b[?2004hREADY\\n'); sys.stdout.flush()\n"
    "def later(text, code=None):\n"
    "    time.sleep(1.0)\n"
    "    print('late:' + text, flush=True)\n"
    "    if code is not None:\n"
    "        import os; os._exit(code)\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line.startswith('later:'):\n"
    "        threading.Thread(target=later, args=(line[6:],)).start()\n"
    "    elif line == 'quit-later':\n"
    "        threading.Thread(target=later, args=('bye', 3)).start()\n"
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
    """The next daemon's connection (``_connect_pty_host``)."""
    c = hc.HostClient.connect(paths.ptyhost_dir())
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
        pid = s.pid
        await mgr.shutdown_all(mode="keep")
        host.close()
        assert not s.exited and s.detached_pty["seq"] > 0
        # A kept session did not end, so nobody is recorded as its ender
        # and no exit event is written (claunch-o6xws).
        assert getattr(s, "ended_by", None) is None
        assert "exit" not in [e.get("kind") for e in mgr.events.rows(s)]
        assert s.detached_pty["modes"]["bracketed_paste"] is True
        # The child writes while no daemon is attached.
        await asyncio.sleep(1.5)
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
        assert "a1" not in mgr2.resumed_busy and "a1" not in mgr2.resumed_idle
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
        pid = s.pid
        await mgr.shutdown_all(mode="keep")
        host.close()
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
