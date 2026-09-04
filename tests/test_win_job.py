"""Windows: a session's process tree ends with the session, not just its root.

Regression for claunch-bt61. pywinpty's terminate reaches one pid; the MCP
server, the bash tool's children and background scripts under it used to
survive as orphans. A kill-on-close job object closes that gap.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

# A child that starts a grandchild, writes the grandchild's pid to a file the
# test is watching, and then sleeps -- so both are alive when the test acts.
_CHILD = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(grandchild.pid))
time.sleep(120)
"""


def _pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _read_grandchild_pid(marker: Path) -> int:
    assert _wait_until(lambda: marker.is_file() and marker.read_text().strip() != "")
    return int(marker.read_text().strip())


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "grandchild.pid"


def test_job_close_ends_the_grandchild(marker):
    from claude_launcher.daemon import win_job

    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(marker)])
    try:
        job = win_job.ProcessJob.for_pid(child.pid)
        assert job is not None, "job object could not be created on this host"
        grandchild = _read_grandchild_pid(marker)
        assert _pid_alive(grandchild)

        job.close()  # kill-on-close: the whole tree, no explicit terminate

        assert _wait_until(lambda: child.poll() is not None)
        assert _wait_until(lambda: not _pid_alive(grandchild))
    finally:
        if child.poll() is None:
            child.kill()


def test_job_terminate_ends_the_grandchild(marker):
    from claude_launcher.daemon import win_job

    child = subprocess.Popen([sys.executable, "-c", _CHILD, str(marker)])
    try:
        job = win_job.ProcessJob.for_pid(child.pid)
        assert job is not None
        grandchild = _read_grandchild_pid(marker)

        job.terminate()

        assert _wait_until(lambda: child.poll() is not None)
        assert _wait_until(lambda: not _pid_alive(grandchild))
        job.close()
    finally:
        if child.poll() is None:
            child.kill()


def test_for_pid_of_a_dead_process_is_none_not_an_error():
    from claude_launcher.daemon import win_job

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert win_job.ProcessJob.for_pid(gone.pid) is None


def test_pty_force_terminate_ends_the_grandchild(marker):
    """Through the real backend: the daemon's forced kill of a session ends
    what the session's child started, and closing the PTY afterwards is the
    same guarantee for a child that exited on its own."""
    from claude_launcher.daemon import pty_backend

    handle = pty_backend.spawn(
        [sys.executable, "-c", _CHILD, str(marker)],
        env=dict(__import__("os").environ),
        cwd=tempfile.gettempdir(),
        cols=80,
        rows=24,
    )
    try:
        grandchild = _read_grandchild_pid(marker)
        assert _pid_alive(grandchild)

        handle.terminate(force=True)

        assert _wait_until(lambda: not handle.isalive())
        assert _wait_until(lambda: not _pid_alive(grandchild))
    finally:
        handle.close()


def test_pty_close_after_own_exit_ends_the_grandchild(marker):
    """The child exits on its own; its leftover grandchild goes when the
    session's PTY is closed (Session._finish), not never."""
    from claude_launcher.daemon import pty_backend

    script = _CHILD.replace("time.sleep(120)\n", "", 1)  # child returns at once
    handle = pty_backend.spawn(
        [sys.executable, "-c", script, str(marker)],
        env=dict(__import__("os").environ),
        cwd=tempfile.gettempdir(),
        cols=80,
        rows=24,
    )
    grandchild = _read_grandchild_pid(marker)
    assert _wait_until(lambda: not handle.isalive())
    assert _pid_alive(grandchild), "precondition: the grandchild outlived its parent"

    handle.close()

    assert _wait_until(lambda: not _pid_alive(grandchild))
