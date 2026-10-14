"""The PTY host (claunch-xnnct, stage 1): protocol, host process, RemotePty.

Every test that starts a host ends it; tests/conftest.py fails a test that
leaves one running (``ptyhost_leak_check``).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the PTY host is POSIX only (stage 1)")

from claude_launcher.daemon import pty_backend  # noqa: E402
from claude_launcher.daemon.ptyhost import client as hc, protocol, socket_path, token_path  # noqa: E402

ECHO = (
    "import sys\n"
    "print('READY', flush=True)\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line == 'quit':\n"
    "        sys.exit(3)\n"
    "    print('echo:' + line, flush=True)\n"
)

SIZE = (
    "import os, sys, signal, time\n"
    "def show(*_):\n"
    "    c, r = os.get_terminal_size(0)\n"
    "    print(f'size:{c}x{r}', flush=True)\n"
    "signal.signal(signal.SIGWINCH, show)\n"
    "show()\n"
    "while True: time.sleep(0.1)\n"
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it has exited for our purposes.
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


def _read_until(pty, needle: bytes, timeout: float = 10.0) -> bytes:
    import threading

    got = bytearray()
    done = threading.Event()

    def pump():
        while needle not in got:
            chunk = pty.read()
            if not chunk:
                break
            got.extend(chunk)
        done.set()

    threading.Thread(target=pump, daemon=True).start()
    assert done.wait(timeout), f"{needle!r} not seen; got {bytes(got)!r}"
    assert needle in got, f"{needle!r} not seen before EOF; got {bytes(got)!r}"
    return bytes(got)


@pytest.fixture
def host_dir(tmp_path):
    # Short, so the socket lives inside it (the long-path case has its own test).
    return tmp_path / "ph"


@pytest.fixture
def client(host_dir):
    c = hc.ensure_host(host_dir, idle_exit=2.0)
    yield c
    if not c.closed.is_set():
        c.shutdown_host()
    pid = hc.host_alive(host_dir)
    assert pid is None or _wait(lambda: not _pid_alive(pid)), "host did not exit"


def _spawn(c, code: str, tmp_path: Path, **kw):
    env = dict(os.environ)
    return c.spawn([sys.executable, "-u", "-c", code], env=env, cwd=str(tmp_path),
                   cols=kw.get("cols", 80), rows=kw.get("rows", 24))


# -- protocol --------------------------------------------------------------------

def test_frame_round_trip_keeps_payload_bytes():
    a, b = socket.socketpair()
    try:
        payload = bytes(range(256)) * 3
        a.sendall(protocol.encode({"op": "write", "sid": "x"}, payload))
        a.sendall(protocol.encode({"op": "ping", "id": 1}))
        assert protocol.read_frame(b) == ({"op": "write", "sid": "x"}, payload)
        assert protocol.read_frame(b) == ({"op": "ping", "id": 1}, b"")
        a.close()
        assert protocol.read_frame(b) is None
    finally:
        b.close()


def test_frame_lengths_out_of_range_are_refused_before_reading():
    import struct

    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack(">II", protocol.HEADER_MAX + 1, 0))
        with pytest.raises(protocol.ProtocolError):
            protocol.read_frame(b)
    finally:
        a.close()
        b.close()


def test_truncated_frame_and_frame_without_op_are_refused():
    import json
    import struct

    a, b = socket.socketpair()
    try:
        head = json.dumps({"no": "op"}).encode()
        a.sendall(struct.pack(">II", len(head), 0) + head)
        with pytest.raises(protocol.ProtocolError, match="no op"):
            protocol.read_frame(b)
        a.sendall(protocol.encode({"op": "x"})[:10])  # cut inside the header
        a.close()
        with pytest.raises(protocol.ProtocolError, match="inside a frame"):
            protocol.read_frame(b)
    finally:
        b.close()


def test_socket_path_moves_to_temp_dir_when_too_long(tmp_path):
    short = tmp_path / "s"
    assert socket_path(short) == short / "ptyhost.sock"
    deep = tmp_path / ("d" * 120)
    p = socket_path(deep)
    assert p.parent != deep and len(os.fsencode(str(p))) <= 108
    assert p == socket_path(deep)  # the client computes the same name
    assert socket_path(tmp_path / ("e" * 120)) != p


# -- host process ----------------------------------------------------------------

def test_spawn_echo_exit_code(client, tmp_path):
    pty = _spawn(client, ECHO, tmp_path)
    assert pty.pid and _pid_alive(pty.pid)
    _read_until(pty, b"READY")
    pty.write(b"hello\r")
    _read_until(pty, b"echo:hello")
    pty.write(b"quit\r")
    assert _wait(lambda: not pty.isalive())
    assert pty.reap() == 3  # the host is the parent; the code crosses the socket
    while pty.read():  # output sent before the exit is delivered first
        pass
    pty.close()
    assert client.list() == []


def test_split_utf8_input_reaches_the_child_whole(client, tmp_path):
    pty = _spawn(client, ECHO, tmp_path)
    _read_until(pty, b"READY")
    word = "한글".encode("utf-8")
    pty.write(word[:2], "viewer")
    pty.write(word[2:] + b"\r", "viewer")
    _read_until(pty, "echo:한글".encode("utf-8"))
    pty.terminate(force=True)
    assert _wait(lambda: not pty.isalive())
    pty.close()


def test_resize_reaches_the_child(client, tmp_path):
    pty = _spawn(client, SIZE, tmp_path, cols=90, rows=30)
    _read_until(pty, b"size:90x30")
    pty.resize(100, 40)
    _read_until(pty, b"size:100x40")
    pty.terminate()
    assert _wait(lambda: not pty.isalive())
    pty.close()


def test_spawn_failure_is_a_pty_error(client, tmp_path):
    with pytest.raises(pty_backend.PtyError):
        client.spawn([str(tmp_path / "no-such-program")], env=dict(os.environ),
                     cwd=str(tmp_path), cols=80, rows=24)
    assert client.list() == []


def test_list_reports_live_sessions(client, tmp_path):
    pty = _spawn(client, ECHO, tmp_path)
    _read_until(pty, b"READY")
    rows = client.list()
    assert [(r["sid"], r["pid"], r["exited"]) for r in rows] == [(pty.sid, pty.pid, False)]
    assert rows[0]["seq"] >= len(b"READY")
    pty.terminate(force=True)
    assert _wait(lambda: not pty.isalive())
    pty.close()


def test_bad_token_and_second_client_are_refused(client, host_dir):
    real = token_path(host_dir).read_text(encoding="ascii")
    token_path(host_dir).write_text("0" * 64, encoding="ascii")
    try:
        with pytest.raises(hc.HostUnavailable, match="bad token"):
            hc.HostClient.connect(host_dir)
    finally:
        token_path(host_dir).write_text(real, encoding="ascii")
    with pytest.raises(hc.HostUnavailable, match="busy"):
        hc.HostClient.connect(host_dir)
    assert client.ping()["pid"] == client.host_pid


def test_client_going_away_ends_its_sessions(client, host_dir, tmp_path):
    """Stage 1: a daemon exit still ends the sessions it owned."""
    pty = _spawn(client, ECHO, tmp_path)
    _read_until(pty, b"READY")
    pid = pty.pid
    client.close()
    assert _wait(lambda: not _pid_alive(pid)), "the child outlived its client"
    while pty.read():
        pass
    assert pty.exit_code() is None
    host = hc.host_alive(host_dir)
    # With no sessions and no client the host leaves after idle_exit (2s).
    assert host is None or _wait(lambda: not _pid_alive(host), timeout=10.0)


def test_spawn_after_the_host_is_gone_raises(client, tmp_path):
    client.shutdown_host()
    with pytest.raises(hc.HostUnavailable):
        _spawn(client, ECHO, tmp_path)


def test_host_exits_when_its_directory_is_removed(host_dir):
    import shutil

    c = hc.ensure_host(host_dir, idle_exit=600.0)
    pid = c.host_pid
    c.close()
    shutil.rmtree(host_dir)
    assert _wait(lambda: not _pid_alive(pid)), "host outlived its directory"


def test_long_directory_uses_temp_socket_and_cleans_it(tmp_path):
    deep = tmp_path / ("d" * 120)
    c = hc.ensure_host(deep, idle_exit=2.0)
    sock = socket_path(deep)
    assert sock.exists() and sock.parent != deep
    c.shutdown_host()
    assert _wait(lambda: not sock.exists())


# -- the daemon side: Session on a RemotePty ---------------------------------------

def test_session_runs_unchanged_on_the_host(home, client, tmp_path, monkeypatch):
    from tests.test_daemon_e2e import _manager, _register_py_harness, _wait_screen
    from claude_launcher.daemon import paths
    from claude_launcher.daemon.harness import SessionDef

    _register_py_harness()
    monkeypatch.setattr(pty_backend, "_host", client)

    async def run():
        mgr = _manager()
        session = mgr.create(SessionDef(name="h1", harness="py", cwd=str(tmp_path)))
        assert isinstance(session.pty, hc.RemotePty)
        assert [r["pid"] for r in client.list()] == [session.pid]
        await _wait_screen(session, "READY")
        await session.send_keys(["hello host", "Enter"])
        await _wait_screen(session, "echo:hello host")
        session.resize(100, 30)
        await session.send_keys(["quit", "Enter"])
        assert await session.wait_for("exited", timeout=10.0, threshold=0.5) == "exited"
        assert session.exit_code == 0
        assert b"READY" in paths.session_log("h1").read_bytes()
        assert client.list() == []

        # kill goes through the host too
        s2 = mgr.create(SessionDef(name="h2", harness="py", cwd=str(tmp_path)))
        await _wait_screen(s2, "READY")
        pid = s2.pid
        mgr.kill("h2")
        assert await s2.wait_for("exited", timeout=10.0, threshold=0.5) == "exited"
        assert _wait(lambda: not _pid_alive(pid))
        await mgr.shutdown_all()

    asyncio.run(run())


def test_spawn_falls_back_to_local_without_a_host(monkeypatch, tmp_path):
    monkeypatch.setattr(pty_backend, "_host", None)
    pty = pty_backend.spawn([sys.executable, "-c", "print('x')"], env=dict(os.environ),
                            cwd=str(tmp_path), cols=80, rows=24)
    try:
        assert not isinstance(pty, hc.RemotePty)
        assert pty.reap(timeout=5.0) == 0
    finally:
        pty.close()


def test_daemon_setting_off_installs_nothing(monkeypatch):
    from claude_launcher.daemon import __main__ as daemon_main

    monkeypatch.setattr(pty_backend, "_host", None)
    assert daemon_main._connect_pty_host({"pty_host": False}) is None
    assert pty_backend._host is None


def test_daemon_setting_on_installs_a_host_client(home, monkeypatch):
    from claude_launcher import store
    from claude_launcher.daemon import __main__ as daemon_main, paths

    assert store.DAEMON_DEFAULTS["pty_host"] is False
    monkeypatch.setattr(pty_backend, "_host", None)
    c = daemon_main._connect_pty_host({"pty_host": True})
    try:
        assert isinstance(c, hc.HostClient) and pty_backend._host is c
        assert hc.host_alive(paths.ptyhost_dir()) == c.host_pid
    finally:
        pty_backend.use_host(None)
        c.shutdown_host()
