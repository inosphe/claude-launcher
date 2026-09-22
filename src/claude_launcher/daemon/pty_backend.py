"""Cross-platform PTY spawning: ConPTY (pywinpty) on Windows, ``pty`` on Unix.

Both backends expose the same tiny blocking interface — ``read`` (blocks, empty
bytes on EOF), ``write``, ``resize``, liveness and exit code — and the session
layer pumps ``read`` from a dedicated thread on every platform, so there is a
single concurrency model regardless of OS.
"""

from __future__ import annotations

import codecs
from collections import OrderedDict
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

    def write(self, data: bytes, writer: object = None) -> None:
        """Write ``data``, on behalf of ``writer``.

        ``writer`` identifies who is writing. A backend that has to decode
        (Windows) carries a partial character between calls, and a partial
        character belongs to the writer that sent its leading bytes -- see
        :meth:`_WinPty.write`. Backends that pass bytes through ignore it.
        """
        raise NotImplementedError

    def forget_writer(self, writer: object) -> None:
        """Drop whatever was being held for ``writer``; it will write no more."""
        return None

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


#: ``FORCE_COLOR`` values that mean "off". The variable is the one member of
#: this family whose presence can also mean *on*, so only the negative
#: readings are dropped -- an operator who exported ``FORCE_COLOR=1`` is
#: answering for the child, not about a pipe they own.
_FORCE_COLOR_OFF = ("", "0", "false")

#: ``TERM`` values that say "there is no terminal here".
_TERM_NOT_A_TERMINAL = ("", "dumb")


def strip_inherited_color_answers(env: Dict[str, str]) -> Dict[str, str]:
    """``env`` minus the "do not colour your output" answers it inherited.

    The daemon is routinely started from inside an agent session's tool shell
    (that is where ``claunch daemon start``, ``claunch web`` and every command
    that autostarts the daemon get run), and such a shell exports
    ``NO_COLOR=1`` / ``FORCE_COLOR=0`` / ``TERM=dumb`` so that *its own*
    subprocess prints plain text for a transcript. The daemon then hands its
    environment to every PTY child it spawns, and those answers are wrong
    there: a session's child draws a TUI into a real PTY that a terminal
    renders -- xterm.js in the web UI, or the terminal behind ``claunch
    attach``. Claude Code honours ``NO_COLOR`` and ``TERM=dumb`` (each alone
    is enough, measured), so an inherited pair turns the whole interface
    monochrome for the life of the daemon, while a harness that consults
    neither (codex) keeps its colours -- which is what the split looks like
    from the outside.

    Dropped rather than replaced, so nothing is asserted that the operator did
    not ask for: on Unix the caller's ``TERM`` default fills the hole, and a
    session that really wants plain output still sets ``NO_COLOR`` through its
    own ``--env`` or its profile's, both of which are applied after this.
    """
    out = dict(env)
    out.pop("NO_COLOR", None)
    if out.get("FORCE_COLOR", "").strip().lower() in _FORCE_COLOR_OFF:
        out.pop("FORCE_COLOR", None)
    if out.get("TERM", "").strip().lower() in _TERM_NOT_A_TERMINAL:
        out.pop("TERM", None)
    return out


class _WinPty(PtyHandle):
    """ConPTY via pywinpty's ptyprocess-style ``PtyProcess``.

    pywinpty's ``read`` returns *decoded text*; it is re-encoded to UTF-8 here
    so the rest of the pipeline is bytes-only like the Unix backend. Writes
    go the other way, and that direction has to remember what it has seen:
    see :meth:`write`.
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
        # The Unix backend ends the child's whole process group; on Windows
        # the equivalent is a kill-on-close job. pywinpty's terminate only
        # ever reaches the one pid it spawned, and the MCP server, the bash
        # tool's children and any background script the harness left running
        # would otherwise survive the session that started them.
        from . import win_job

        self._job = win_job.ProcessJob.for_pid(self.pid)
        self._open_decoder()

    def read(self) -> bytes:
        try:
            data = self._pty.read(65536)
        except (EOFError, OSError):
            return b""
        if not data:
            return b""
        return data.encode("utf-8", errors="replace") if isinstance(data, str) else data

    #: A session's writers are its viewers plus its own delivery, send-keys
    #: and paste paths, so the count is small and bounded by the people
    #: looking at one terminal. The cap is a backstop against a caller that
    #: invents a key per write rather than per writer: past it the oldest is
    #: dropped, which costs that writer a partial character and nothing else.
    MAX_WRITERS = 64

    def _open_decoder(self) -> None:
        """The decoders :meth:`write` carries between calls, one per writer."""
        self._in: "OrderedDict[object, codecs.IncrementalDecoder]" = OrderedDict()

    def _decoder_for(self, writer: object) -> codecs.IncrementalDecoder:
        dec = self._in.get(writer)
        if dec is None:
            if len(self._in) >= self.MAX_WRITERS:
                self._in.popitem(last=False)
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            self._in[writer] = dec
        else:
            self._in.move_to_end(writer)
        return dec

    def forget_writer(self, writer: object) -> None:
        self._in.pop(writer, None)

    def write(self, data: bytes, writer: object = None) -> None:
        """Bytes in, text out, with a sequence split across two calls kept.

        ConPTY takes text, so what a viewer typed is decoded here. Decoding
        each call on its own destroyed any character whose bytes were split
        between two of them: the leading bytes became U+FFFD at the end of
        one call and the trailing bytes became U+FFFD at the start of the
        next, so one character typed reached the child as two replacement
        characters. ASCII cannot show it -- one byte per character -- and
        Hangul is three bytes per syllable, which is where it was reported
        (s586, 2026-09-20: syllables going missing while typing quickly).

        Splits are ordinary. Every writer into a PTY chunks by something
        that is not a character boundary: a socket frame, a pipe read, a
        buffer that filled. So a decoder is kept and fed, and a trailing
        fragment waits here for the rest of its character. Bytes that can
        begin no sequence are still replaced, so rubbish cannot stall what
        follows it.

        One decoder per writer, because a partial character belongs to the
        writer that sent its leading bytes. A single decoder was enough only
        as long as nobody wrote between those two calls, and a session has
        several writers: each viewer, the delivery queue, send-keys. The
        lock in Session.write_bytes keeps two of them out of one decoder at
        the same moment; it does not keep a delivery from landing in the gap
        between a viewer's two frames, and then the delivery's first bytes
        were read as the end of the viewer's character. Measured on the two
        syllables of a Hangul word split over two frames with one delivery
        between them, four of the five split points lost a syllable (s586,
        2026-09-22).
        """
        text = self._decoder_for(writer).decode(data)
        if text:
            self._pty.write(text)

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
        # The gentle path keeps going through pywinpty (a console interrupt
        # the harness may handle); the forced one ends the tree as a unit so
        # that nothing under the child gets to outlive it.
        try:
            self._pty.terminate(force=force)
        except Exception:
            pass
        if force and self._job is not None:
            self._job.terminate()

    def close(self) -> None:
        try:
            self._pty.close()
        except Exception:
            pass
        # Closing the last job handle is the kill for whatever the child
        # left behind -- the case where it exited on its own and _finish is
        # tidying up after it.
        if self._job is not None:
            self._job.close()


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

    def write(self, data: bytes, writer: object = None) -> None:
        # Bytes go to the master as they are; there is nothing to decode and
        # so nothing to hold between calls. ``writer`` is accepted for the
        # one signature and ignored.
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
