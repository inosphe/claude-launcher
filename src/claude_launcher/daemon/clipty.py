"""The daemon's raw, unmanaged shell PTYs.

A session is a managed agent: named, arranged, restored. These are the
opposite end of the same pipe, and there are two kinds of them:

* :class:`ShellPty` — the dashboard's CLI tab. One per daemon, for the user
  alone: a plain interactive shell with no record, no harness and no
  lifecycle beyond the daemon's own. Spawned lazily on the first viewer, then
  left running — its state (directory, history, exports) is meant to outlive
  whichever browser tab is looking at it.
* :class:`SessionShell` — one per agent session, shown in that session's
  ``cli`` split pane. Same PTY, plus the proposal queue: the session's agent
  may read its output and propose input, and only the user accepts. A
  session's shell dies with the session (``daemon/session_shells.py``).

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
import secrets
import sys
import threading
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

from .. import store
from . import instance_manifest, pty_backend

log = logging.getLogger(__name__)

#: How much of the shell's output is kept for a late-joining viewer, raw.
#: No screen is rendered server-side, so this ring IS the scrollback — the
#: bytes a new viewer is replayed before they switch to live. Bounded so a
#: crashed command that floods the shell cannot balloon the daemon's memory.
SHELL_BUFFER_MAX = 256 * 1024

#: The most output one read returns, so a caller cannot ask the daemon to
#: copy the whole ring into one response and a slow reader stays cheap.
READ_MAX = 64 * 1024

#: Set for the CLI tab's shell only. The shell is a session leader that the
#: daemon terminates as part of its own shutdown (``api._close_cli_shell``),
#: so a ``claunch daemon restart`` typed into this tab is SIGHUPed between its
#: stop and its start and never brings the successor up (claunch-k9fpi).
#: ``cli_sessions._cmd_daemon`` reads the marker and, on POSIX, hands that
#: restart to a process in a session of its own. On Windows the daemon's job
#: object allows no breakaway, so the hand-off would not leave it and the
#: start is still lost there (claunch-k9fpi: unresolved on win32). Nothing
#: else in the tree sets the marker, and a session's shell is not marked
#: because a session survives a keep restart.
CLI_TAB_ENV = "CLAUNCH_CLI_TAB"

#: A proposed line's own limits: long enough for a compound command or a
#: short script, short enough that the pane stays readable. The per-session
#: cap keeps one agent from filling the user's strip; the total cap bounds
#: the memory the daemon spends on proposals nobody has decided. Per-shell,
#: since every session has one.
SUGGESTION_MAX_TEXT = 4096
SUGGESTION_MAX_WHY = 1000
SUGGESTION_PENDING_PER_SESSION = 10
SUGGESTION_PENDING_MAX = 50
SUGGESTION_KEEP_DECIDED = 200


def _now() -> str:
    """UTC seconds, the timestamp shape the daemon's records use."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    """Owns one persistent shell child; the CLI tab's shell.

    The user's own terminal and nothing else: no proposal queue, no agent
    routes (those are :class:`SessionShell`). Spawned lazily: the first
    WebSocket viewer calls :meth:`start_once`, which starts the child and
    leaves it running until the shell exits on its own or the daemon shuts
    down. ``start_once`` only ever starts a child that never was started — a
    shell stopped mid-daemon (the user typed ``exit``) stays stopped until a
    viewer's ``restart`` control revives it. An asymmetry that is the point:
    after a daemon restart there is no old shell to miss and the tab comes up
    fresh; while the daemon lives, an exited shell is a deliberate stop, and
    only a person may undo it.
    """

    def __init__(
        self,
        *,
        argv: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        cols: int = 100,
        rows: int = 30,
        config: Optional[dict] = None,
        cli_tab: bool = True,
    ) -> None:
        cfg = config if config is not None else store.daemon_config()
        self._argv = (
            list(argv)
            if argv is not None
            else (_config_argv(cfg.get("shell")) or default_shell_argv())
        )
        self._cwd = cwd if cwd is not None else (cfg.get("shell_cwd") or None)
        #: Whether this child carries the CLI tab marker (see CLI_TAB_ENV).
        #: The tab's shell does; a session's shell does not — the marker is
        #: read by ``claunch daemon restart`` and only that tab's shell was
        #: ever intended to hand the restart off.
        self._cli_tab = cli_tab
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
        #: Bytes ever read from the child in this incarnation. The ring keeps
        #: only its tail, so this is the cursor a reader resumes from: the
        #: buffer covers ``[_total - _buffered, _total)``.
        self._total = 0
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

    @property
    def total(self) -> int:
        """Bytes read from this incarnation — the cursor a later read uses."""
        return self._total

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
        self._total = 0
        self.exited = False
        self.exit_code = None
        # Same inheritance the session harness gets, and the same correction:
        # this shell also draws into a terminal on the other end. The daemon's
        # interpreter settings come out for the same reason as there -- a
        # ``claunch`` typed into this tab cannot start with ``PYTHON_GIL=0``
        # (see ``instance_manifest.INTERPRETER_ONLY_ENV``, claunch-t9xko).
        env = pty_backend.strip_inherited_color_answers(dict(os.environ))
        for var in instance_manifest.INTERPRETER_ONLY_ENV:
            env.pop(var, None)
        if sys.platform != "win32":   # cmd.exe has no termios to service
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
        # The CLI tab's shell dies with the daemon; anything it must outlive
        # has to know it was started there (see CLI_TAB_ENV).
        if self._cli_tab:
            env[CLI_TAB_ENV] = "1"
        try:
            # In this process even with the PTY host on (claunch-xnnct.1):
            # the shell has no record for a successor to adopt it by, so it
            # ends with the daemon, as the module docstring says.
            self._pty = pty_backend.spawn_local(
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
        self._total += len(chunk)
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

    # ------------------------------------------------------------------ #
    # shared reading (agents)
    # ------------------------------------------------------------------ #
    def read_output(self, after: Optional[int] = None, limit: int = READ_MAX) -> dict:
        """A slice of the output ring, for a reader that is not a terminal.

        ``after`` is the ``offset`` a previous read returned; without one the
        caller gets the tail. Offsets are absolute byte positions in this
        incarnation, so they survive the ring dropping old bytes: a reader
        that fell behind is told how much it missed (``dropped``) and
        resumes at ``retained_from`` instead of silently skipping it.
        """
        limit = max(1, min(int(limit), READ_MAX))
        blob = b"".join(self._buffer)
        retained_from = self._total - len(blob)
        if after is None:
            start = max(retained_from, self._total - limit)
            dropped = 0
        else:
            start = max(int(after), retained_from)
            dropped = max(0, retained_from - int(after)) if int(after) < self._total else 0
            start = min(start, self._total)
        end = min(self._total, start + limit)
        chunk = blob[start - retained_from:end - retained_from]
        return {
            "offset": end,
            "start": start,
            "end": end,
            "total": self._total,
            "pid": self.pid,
            "retained_from": retained_from,
            "dropped": dropped,
            # The cursor is from an earlier incarnation (the shell was
            # restarted); its numbering began again at 0.
            "reset": after is not None and int(after) > self._total,
            "text": chunk.decode("utf-8", "replace"),
            "exited": self.exited,
            "exit_code": self.exit_code,
        }

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


class SessionShell(ShellPty):
    """One agent session's own shell — the session's ``cli`` split pane.

    The same PTY the CLI tab has, with one thing added: the agent driving
    that session may **read** its output and **propose** input, and only the
    user accepts. There is no path from a proposal to the child except
    :meth:`accept`, which the user's side (the pane's run button, or
    ``claunch shell accept``) calls — the MCP tools have no accept tool and
    the route refuses a session-shaped accept (``daemon/shell_api.py``).

    Every shell here belongs to exactly one session and dies with it
    (``daemon/session_shells.py``), which is what makes the boundary
    legible: a proposal can only be made to the shell of the session that
    made it, so there is nothing to authorize across sessions.
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("cli_tab", False)
        super().__init__(**kwargs)
        #: Input proposed by this session's agent, by suggestion id. A
        #: proposal is text and a reason; only the user's accept writes it.
        self._suggestions: Dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # input proposals (the agent proposes, the user decides)
    # ------------------------------------------------------------------ #
    def suggestions(self, state: Optional[str] = None) -> List[dict]:
        """The proposal queue, oldest first; ``state`` filters it."""
        rows = [dict(r) for r in self._suggestions.values()]
        if state:
            rows = [r for r in rows if r["state"] == state]
        return rows

    def suggest(self, session: str, text: str, why: str = "") -> dict:
        """Queue text this session's agent proposes the user run.

        Nothing here reaches the child: a proposal is a row the session's
        ``cli`` pane shows beside its accept control.
        """
        session = (session or "").strip()
        if not session:
            raise ValueError("session is required")
        text = (text or "").strip()
        if not text:
            raise ValueError("text is required")
        if "\x00" in text:
            raise ValueError("text must not contain NUL")
        if len(text) > SUGGESTION_MAX_TEXT:
            raise ValueError(f"text is longer than {SUGGESTION_MAX_TEXT} characters")
        if len(why or "") > SUGGESTION_MAX_WHY:
            raise ValueError(f"why is longer than {SUGGESTION_MAX_WHY} characters")
        pending = [r for r in self._suggestions.values() if r["state"] == "pending"]
        if len(pending) >= SUGGESTION_PENDING_MAX:
            raise ValueError("the shell already has the maximum number of pending suggestions")
        if sum(1 for r in pending if r["session"] == session) >= SUGGESTION_PENDING_PER_SESSION:
            raise ValueError(f"{session} already has {SUGGESTION_PENDING_PER_SESSION} pending suggestions")
        row = {
            "id": "sh-" + secrets.token_hex(3),
            "session": session,
            "text": text,
            "why": why or "",
            "state": "pending",
            "created_at": _now(),
            "decided_at": None,
            "decided_by": None,
        }
        self._suggestions[row["id"]] = row
        self._prune_suggestions()
        self._broadcast_suggestions()
        return dict(row)

    def decide(self, sid: str, state: str, by: str) -> dict:
        """Move a pending proposal to ``state`` (``accepted``/``rejected``).

        Deciding does not write anything; :meth:`accept` does that after it.
        """
        row = self._suggestions.get(sid)
        if row is None:
            raise KeyError(sid)
        if row["state"] != "pending":
            raise ValueError(f"suggestion {sid} was already {row['state']}")
        row["state"] = state
        row["decided_at"] = _now()
        row["decided_by"] = by
        self._prune_suggestions()
        self._broadcast_suggestions()
        return dict(row)

    def withdraw(self, sid: str, session: str) -> dict:
        """The proposing session takes its own pending suggestion back."""
        row = self._suggestions.get(sid)
        if row is None:
            raise KeyError(sid)
        if row["session"] != session:
            raise PermissionError("only the session that proposed it may withdraw a suggestion")
        return self.decide(sid, "withdrawn", session)

    async def accept(self, sid: str, by: str = "user") -> dict:
        """The user accepts: type the text (and Enter) into the child."""
        row = self._suggestions.get(sid)
        if row is None:
            raise KeyError(sid)
        if row["state"] != "pending":
            raise ValueError(f"suggestion {sid} was already {row['state']}")
        if self.exited or self._pty is None:
            raise RuntimeError("the shell is not running; restart it in the session's CLI pane first")
        row = self.decide(sid, "accepted", by)
        await self.write_bytes(row["text"].encode("utf-8") + b"\r", "suggestion")
        return row

    def _broadcast_suggestions(self) -> None:
        self._broadcast(("suggestions", self.suggestions("pending")))

    def _prune_suggestions(self) -> None:
        """Keep the queue bounded: decided rows fall off oldest first."""
        decided = [r for r in self._suggestions.values() if r["state"] != "pending"]
        if len(decided) <= SUGGESTION_KEEP_DECIDED:
            return
        decided.sort(key=lambda r: r["created_at"])
        for row in decided[: len(decided) - SUGGESTION_KEEP_DECIDED]:
            self._suggestions.pop(row["id"], None)
