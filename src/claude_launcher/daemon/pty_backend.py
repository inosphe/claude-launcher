"""Cross-platform PTY spawning: ConPTY (pywinpty) on Windows, ``pty`` on Unix.

Both backends expose the same tiny blocking interface — ``read`` (blocks, empty
bytes on EOF), ``write``, ``resize``, liveness and exit code — and the session
layer pumps ``read`` from a dedicated thread on every platform, so there is a
single concurrency model regardless of OS.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, Optional, Sequence


class PtyError(Exception):
    """Raised when a PTY child cannot be spawned."""


class PtyHandle:
    """Interface both platform backends implement."""

    pid: Optional[int] = None

    def read(self) -> bytes:  # blocking; b"" signals EOF
        raise NotImplementedError

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def isalive(self) -> bool:
        raise NotImplementedError

    def exit_code(self) -> Optional[int]:
        raise NotImplementedError

    def terminate(self, force: bool = False) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def spawn(
    argv: Sequence[str],
    *,
    env: Dict[str, str],
    cwd: str,
    cols: int,
    rows: int,
) -> PtyHandle:
    """Spawn ``argv`` under a new PTY sized ``cols`` x ``rows``."""
    if sys.platform == "win32":
        return _WinPty(argv, env=env, cwd=cwd, cols=cols, rows=rows)
    return _UnixPty(argv, env=env, cwd=cwd, cols=cols, rows=rows)


class _WinPty(PtyHandle):
    """ConPTY via pywinpty's ptyprocess-style ``PtyProcess``.

    pywinpty's ``read`` returns *decoded text*; it is re-encoded to UTF-8 here
    so the rest of the pipeline is bytes-only like the Unix backend.
    """

    def __init__(self, argv, *, env, cwd, cols, rows):
        try:
            import winpty
        except ImportError as exc:  # pragma: no cover - dependency marker
            raise PtyError("pywinpty is required on Windows") from exc
        try:
            self._pty = winpty.PtyProcess.spawn(
                list(argv), cwd=cwd, env=env, dimensions=(rows, cols)
            )
        except Exception as exc:
            raise PtyError(f"could not spawn {argv[0]!r}: {exc}") from exc
        self.pid = self._pty.pid

    def read(self) -> bytes:
        try:
            data = self._pty.read(65536)
        except (EOFError, OSError):
            return b""
        if not data:
            return b""
        return data.encode("utf-8", errors="replace") if isinstance(data, str) else data

    def write(self, data: bytes) -> None:
        self._pty.write(data.decode("utf-8", errors="replace"))

    def resize(self, cols: int, rows: int) -> None:
        self._pty.setwinsize(rows, cols)

    def isalive(self) -> bool:
        try:
            return bool(self._pty.isalive())
        except Exception:
            return False

    def exit_code(self) -> Optional[int]:
        if self.isalive():
            return None
        return getattr(self._pty, "exitstatus", None)

    def terminate(self, force: bool = False) -> None:
        try:
            self._pty.terminate(force=force)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:
            pass


class _UnixPty(PtyHandle):
    """A ``pty.openpty`` pair with the child re-parented onto it as its
    controlling terminal (so ``C-c`` and friends work through the line
    discipline, tmux-style)."""

    def __init__(self, argv, *, env, cwd, cols, rows):
        import fcntl
        import pty
        import struct
        import termios

        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def _become_session_leader():  # pragma: no cover - child process
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self._proc = subprocess.Popen(
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                cwd=cwd,
                close_fds=True,
                preexec_fn=_become_session_leader,
            )
        except OSError as exc:
            os.close(self._master)
            os.close(slave)
            raise PtyError(f"could not spawn {argv[0]!r}: {exc}") from exc
        finally:
            try:
                os.close(slave)
            except OSError:
                pass
        self.pid = self._proc.pid

    def read(self) -> bytes:
        try:
            return os.read(self._master, 65536)
        except OSError:
            return b""  # EIO once every slave fd is gone == EOF

    def write(self, data: bytes) -> None:
        os.write(self._master, data)

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import signal
        import struct
        import termios

        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def isalive(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> Optional[int]:
        return self._proc.poll()

    def terminate(self, force: bool = False) -> None:
        try:
            if force:
                self._proc.kill()
            else:
                self._proc.terminate()
        except OSError:
            pass

    def close(self) -> None:
        try:
            os.close(self._master)
        except OSError:
            pass
