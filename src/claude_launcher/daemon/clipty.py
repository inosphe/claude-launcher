"""One raw, unmanaged shell PTY — the dashboard's CLI tab.

A session is a managed agent: named, arranged, restored. This is the opposite
end of the same pipe: a plain interactive shell on the daemon machine, with
no record, no harness and no lifecycle beyond the daemon's own. One per
daemon (the SPA's tab is one terminal), spawned lazily on the first viewer,
then left running — the shell's state (directory, history, exports) is meant
to outlive whichever browser tab is looking at it.

Concurrency model is borrowed from :class:`~claude_launcher.daemon.session
.Session`'s: all mutable state lives on the event loop; a dedicated daemon
reader thread pumps blocking PTY reads in via ``call_soon_threadsafe``. There
is less to observe, so no screen, no sampler, no log — just output relayed to
viewers and a bounded ring of it kept in memory, so a viewer that attaches
after output was produced (with nobody else watching) still sees what
happened. The wheel on the web side browses xterm's own scrollback, which is
why the browser gets a scrollback where the session terminal has none.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

from .. import store
from . import pty_backend

log = logging.getLogger(__name__)

#: How much of the shell's output is kept for a late-joining viewer, raw.
#: No screen is rendered server-side, so this ring IS the scrollback — the
#: bytes a new viewer is replayed before they switch to live. Bounded so a
#: crashed command that floods the shell cannot balloon the daemon's memory.
SHELL_BUFFER_MAX = 256 * 1024


def default_shell_argv() -> List[str]:
    """The platform's own interactive shell, spoken plainly.

    ``$SHELL`` on Unix can point at a login shell (a stray ``~/.profile``
    echo is noise the tab survives); ``/bin/sh`` is the fallback that always
    exists. ``COMSPEC`` on Windows is the console host configured by the
    system (``cmd.exe`` in practice); a user who wants PowerShell sets
    ``daemon.shell`` in the config instead.
    """
    if sys.platform == "win32":
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    return [os.environ.get("SHELL") or "/bin/sh"]


def _config_argv(shell: object) -> Optional[List[str]]:
    """A ``daemon.shell`` config value, normalized to an argv or ``None``."""
    if shell is None:
        return None
    if isinstance(shell, list):
        return [str(s) for s in shell]
    if isinstance(shell, str):
        return shell.split() or None   # a bare "powershell" should just work
    return None


class ShellPty:
    """Owns one persistent shell child; created once by the daemon's app.

    Spawned lazily: the first WebSocket viewer calls :meth:`start_once`,
    which starts the child and leaves it running until the shell exits on its
    own or the daemon shuts down. ``start_once`` only ever starts a child
    that never was started — a shell stopped mid-daemon (the user typed
    ``exit``) stays stopped until a viewer's ``restart`` control revives it.
    An asymmetry that is the point: after a daemon restart there is no old
    shell to miss and the tab comes up fresh; while the daemon lives, an
    exited shell is a deliberate stop, and only a person may undo it.
    """

    def __init__(
        self,
        *,
        argv: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        cols: int = 100,
        rows: int = 30,
        config: Optional[dict] = None,
    ) -> None:
        cfg = config if config is not None else store.daemon_config()
        self._argv = (
            list(argv)
            if argv is not None
            else (_config_argv(cfg.get("shell")) or default_shell_argv())
        )
        self._cwd = cwd if cwd is not None else (cfg.get("shell_cwd") or None)
        self._cols, self._rows = cols, rows
        #: Whether a child was ever spawned for this daemon incarnation.
        self._ever_started = False
        self.exited = True
        self.exit_code: Optional[int] = None
        self.pid: Optional[int] = None
        self._pty = None
        self._reader: Optional[threading.Thread] = None
        self._buffer: deque = deque()          # raw output chunks, oldest last
        self._buffered = 0                     # their total length
        self._subscribers = set()
        self._loop = asyncio.get_running_loop()
        #: One writer into the PTY at a time; see :meth:`write_bytes`.
        self._write_lock = asyncio.Lock()

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start_once(self) -> bool:
        """Spawn the child if this daemon never had one; report whether it
        is running now."""
        if not self.exited:
            return True
        if self._ever_started:
            return False   # an exited shell stays exited until restart
        return self.restart()

    def restart(self) -> bool:
        """Replace the child with a fresh shell, clearing its ring buffer.

        New viewers are told and so are attached ones: a ``init`` broadcast
        announces the new pid, and the fresh child's first bytes follow it.
        """
        if self._pty is not None:
            # Only ever reached for a dead child (restart is how an exited
            # shell comes back), but a race that left one alive must not
            # leave an orphan behind it.
            try:
                self._pty.terminate(force=True)
                self._pty.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._pty = None
        self._buffer.clear()
        self._buffered = 0
        self.exited = False
        self.exit_code = None
        # Same inheritance the session harness gets, and the same correction:
        # this shell also draws into a terminal on the other end.
        env = pty_backend.strip_inherited_color_answers(dict(os.environ))
        if sys.platform != "win32":   # cmd.exe has no termios to service
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
        try:
            self._pty = pty_backend.spawn(
                self._argv,
                env=env,
                cwd=self._cwd or os.getcwd(),
                cols=self._cols,
                rows=self._rows,
            )
        except pty_backend.PtyError as exc:
            log.warning("cannot spawn the CLI shell %r: %s", self._argv, exc)
            self.exited = True
            self._ever_started = True
            return False
        self._ever_started = True
        self.pid = self._pty.pid
        self._reader = threading.Thread(
            target=self._read_pump, name="pty-read-cli", daemon=True
        )
        self._reader.start()
        self._broadcast(("init", (self._cols, self._rows, self.pid)))
        return True

    async def shutdown(self, grace: float = 2.0) -> None:
        """Terminate the child and wait briefly; force-kill stragglers."""
        if self.exited or self._pty is None:
            return
        try:
            self._pty.terminate(force=False)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        deadline = time.monotonic() + grace
        while not self.exited and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if not self.exited and self._pty is not None:
            try:
                self._pty.terminate(force=True)
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    # ------------------------------------------------------------------ #
    # output pipeline (reader thread -> loop)
    # ------------------------------------------------------------------ #
    def _read_pump(self) -> None:  # runs on the reader thread
        try:
            while True:
                chunk = self._pty.read()
                if not chunk:
                    break
                self._loop.call_soon_threadsafe(self._on_output, chunk)
            self._loop.call_soon_threadsafe(self._on_eof)
        except RuntimeError:
            pass  # event loop already closed (daemon teardown)

    def _on_output(self, chunk: bytes) -> None:
        if self.exited:
            return
        self._buffer.append(chunk)
        self._buffered += len(chunk)
        while self._buffered > SHELL_BUFFER_MAX:
            self._buffered -= len(self._buffer[0])
            self._buffer.popleft()
        self._broadcast(("data", chunk))

    def _on_eof(self) -> None:
        if self.exited:
            return
        self.exited = True
        self.exit_code = self._pty.exit_code() if self._pty is not None else None
        if self._pty is not None:
            try:
                self._pty.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        self._broadcast(("exit", self.exit_code))

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    async def write_bytes(self, data: bytes, writer: object = None) -> None:
        """Keystrokes from a viewer. Dropped while the shell is dead.

        ``writer`` names the viewer, and a shell has as many as it has
        windows open on it. It is carried for the same reason a session
        carries it: the Windows backend holds a character split across two
        calls, and that fragment must not be finished off by somebody
        else's bytes (Session.write_bytes).
        """
        if self.exited or self._pty is None:
            return
        # One writer at a time, and off the loop: the same two rules a
        # session's writes follow, for the same reasons (Session.write_bytes).
        async with self._write_lock:
            await self._loop.run_in_executor(
                None, functools.partial(self._pty.write, data, writer)
            )

    def forget_writer(self, writer: object) -> None:
        """A viewer has gone; drop the partial character it never finished."""
        if self._pty is not None:
            self._pty.forget_writer(writer)

    def resize(self, cols: int, rows: int) -> None:
        """The viewer's grid. Remembered for the next child, applied and
        announced to every attached viewer when one is alive."""
        if cols == self._cols and rows == self._rows:
            return
        self._cols, self._rows = cols, rows
        if not self.exited and self._pty is not None:
            try:
                self._pty.resize(cols, rows)
            except Exception:  # noqa: BLE001 — racing child exit
                pass
        self._broadcast(("resize", (cols, rows)))

    # ------------------------------------------------------------------ #
    # subscribers (WebSocket viewers)
    # ------------------------------------------------------------------ #
    def attach(self) -> Tuple[asyncio.Queue, bytes]:
        """Subscribe a viewer and hand it the output ring in one step.

        Both halves run synchronously on the loop, so the snapshot covers
        exactly the bytes published before this viewer subscribed and the
        queue carries everything after — no gap and no duplicate.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        return q, b"".join(self._buffer)

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, item: Tuple[str, object]) -> None:
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                dead.append(q)  # slow consumer: drop it, it can reattach
        for q in dead:
            self._subscribers.discard(q)
