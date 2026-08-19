"""A live managed session: one PTY child plus everything observed about it.

Concurrency model: all mutable state lives on the daemon's asyncio event loop.
The only other thread is a per-session *reader* pumping blocking PTY reads into
the loop via ``call_soon_threadsafe`` — identical on Windows (ConPTY reads have
no fd to select on) and Unix (kept symmetric on purpose).

Each output chunk is fed to the pyte screen, appended to the on-disk raw log,
and fanned out to attached subscribers (WebSocket viewers). A sampler task
fingerprints the rendered screen a few times a second so the
:class:`~claude_launcher.daemon.idle.IdleTracker` can tell "the program is
painting a spinner" apart from "the program printed something new".
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from . import keys as keys_mod
from . import paths, pty_backend
from .harness import CLAUDE_HARNESS, SessionDef
from .idle import IdleTracker
from .screen import ScreenState

#: Screen sampling cadence for idle detection (seconds).
SAMPLE_INTERVAL = 0.4

#: Rotate the raw output log beyond this size (a single .1 backup is kept).
LOG_MAX_BYTES = 10 * 1024 * 1024

#: Gap between a paste and its submitting Enter (seconds), so the CR arrives in
#: its own PTY read — see :meth:`Session.paste`. Measured against Claude Code:
#: 0 never submits, 20ms already does; 150ms leaves room for a busy renderer.
PASTE_ENTER_DELAY = float(os.environ.get("CLAUNCH_PASTE_ENTER_DELAY") or 0.15)

#: How long a TUI has, from spawn, to become deliverable-to before
#: :meth:`Session.deliver` stops waiting and writes anyway. Generous: it is
#: paid at most once per session, and being late is free while being early
#: loses the message.
INPUT_READY_TIMEOUT = float(os.environ.get("CLAUNCH_INPUT_READY_TIMEOUT") or 30.0)

#: How long a TUI must stay idle *after* taking the keyboard before it is
#: considered done starting up. On top of the idle threshold, so the real wait
#: is longer; measured against Claude Code, whose input mounts a few seconds
#: before it finishes loading and starts accepting a submit.
INPUT_SETTLE = float(os.environ.get("CLAUNCH_INPUT_SETTLE") or 3.0)

#: How long the keyboard must have been quiet before an automated delivery may
#: type into a terminal (seconds). A human composing a message pauses to think
#: for a couple of seconds — long enough for the *screen* to read as idle —
#: and a paste-plus-Enter landing in that pause submits their half-typed line
#: with the delivery folded into it. Keystrokes are a signal the screen
#: sampler cannot see, so they are tracked separately (see
#: :meth:`Session.note_human_input`).
TYPING_GUARD = float(os.environ.get("CLAUNCH_TYPING_GUARD") or 5.0)

#: Bound on how long :meth:`Session.deliver` waits for the keyboard to go
#: quiet. Someone typing continuously holds a message at most this long — a
#: delayed delivery is recoverable, an interleaved one already went wrong, but
#: the message itself must never be dropped (the INPUT_READY_TIMEOUT
#: reasoning, applied to the reader's other half: the human).
TYPING_HOLD_TIMEOUT = float(os.environ.get("CLAUNCH_TYPING_HOLD_TIMEOUT") or 30.0)

log = logging.getLogger(__name__)

STATUS_STARTING = "starting"
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_EXITED = "exited"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """Owns one PTY child; created and torn down by the SessionManager.

    Constructed and *started* in two steps. Between them the session exists —
    it is in the registry, it has a name, it can be joined into a mesh — but
    nothing is running yet and its command line is not fixed. That gap is what
    lets a session be arranged before it runs: its mesh membership and its
    cflow run are settled first, and the opening message they compose is handed
    to the harness as an argument instead of typed into a terminal that may not
    be reading yet (see :meth:`_await_readable`). Only
    :class:`~claude_launcher.daemon.manager.SessionManager` should hold an
    unstarted one.
    """

    def __init__(
        self,
        sdef: SessionDef,
        *,
        idle_threshold: float,
        scrollback: int,
    ) -> None:
        self.sdef = sdef
        self.argv: List[str] = []
        self.pty = None
        self.pid: Optional[int] = None
        self.idle_threshold = idle_threshold
        self.screen = ScreenState(sdef.cols, sdef.rows, history=scrollback)
        self.tracker = IdleTracker()
        self.created_at = _utcnow()
        self.last_output_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.exited_at: Optional[str] = None
        self.exited = False
        self._started_mono = time.monotonic()
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop = asyncio.get_running_loop()
        self._status = STATUS_STARTING
        self._saw_output = False
        #: Latched once the harness has been seen ready to take a message; see
        #: :meth:`_await_readable`.
        self._input_ready = False
        #: Monotonic time a human last typed here (attach/web keystrokes,
        #: ``claunch send-keys``); 0.0 = never. See :meth:`keyboard_busy`.
        self._last_human_input = 0.0

        session_dir = paths.session_dir(sdef.name)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = paths.session_log(sdef.name)
        self._log = open(self._log_path, "ab")

    def start(self, argv: List[str], env: Dict[str, str], cwd: str) -> None:
        """Spawn the child and begin reading it. Called once, by the manager."""
        self.argv = argv
        self._started_mono = time.monotonic()
        self.pty = pty_backend.spawn(
            argv, env=env, cwd=cwd, cols=self.sdef.cols, rows=self.sdef.rows
        )
        self.pid = self.pty.pid

        # A dedicated *daemon* thread, NOT the loop's default executor: at
        # daemon exit asyncio joins executor threads, and a reader stuck in a
        # blocking ConPTY read would hold the whole process (and its singleton
        # lock) hostage. A daemon thread can never block interpreter exit.
        self._reader = threading.Thread(
            target=self._read_pump, name=f"pty-read-{self.sdef.name}", daemon=True
        )
        self._reader.start()
        self._sampler = self._loop.create_task(self._sample_loop())

    # ------------------------------------------------------------------ #
    # output pipeline (reader thread -> loop)
    # ------------------------------------------------------------------ #
    def _read_pump(self) -> None:  # runs on the reader thread
        try:
            while True:
                chunk = self.pty.read()
                if not chunk:
                    break
                self._loop.call_soon_threadsafe(self._on_output, chunk)
            self._loop.call_soon_threadsafe(self._on_eof)
        except RuntimeError:
            pass  # event loop already closed (daemon teardown)

    def _on_output(self, chunk: bytes) -> None:
        if self.exited:
            return
        self._saw_output = True
        self.last_output_at = _utcnow()
        self.screen.feed(chunk)
        self._append_log(chunk)
        self._broadcast(("data", chunk))

    def _append_log(self, chunk: bytes) -> None:
        try:
            if self._log.tell() > LOG_MAX_BYTES:
                self._log.close()
                backup = self._log_path.with_suffix(".log.1")
                if backup.exists():
                    backup.unlink()
                self._log_path.rename(backup)
                self._log = open(self._log_path, "ab")
            self._log.write(chunk)
            self._log.flush()
        except OSError:
            pass

    def _on_eof(self) -> None:
        if self.exited:
            return
        self._finish()

    def _finish(self) -> None:
        self.exited = True
        self.exit_code = self.pty.exit_code()
        self.exited_at = _utcnow()
        self._status = STATUS_EXITED
        if self._sampler:
            self._sampler.cancel()
        try:
            self._log.close()
        except OSError:
            pass
        self._write_meta()
        self._broadcast(("exit", self.exit_code))
        self.pty.close()

    def _write_meta(self) -> None:
        meta = {
            "name": self.sdef.name,
            "argv": self.argv,
            "created_at": self.created_at,
            "exited_at": self.exited_at,
            "exit_code": self.exit_code,
        }
        try:
            paths.session_dir(self.sdef.name).joinpath("meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # idle sampling
    # ------------------------------------------------------------------ #
    async def _sample_loop(self) -> None:
        try:
            while not self.exited:
                await asyncio.sleep(SAMPLE_INTERVAL)
                if self.exited:
                    break
                self.tracker.sample(self.screen.line_hashes(), time.monotonic())
                new = self._compute_status(self.idle_threshold)
                if new != self._status:
                    self._status = new
                    self._broadcast(("state", new))
                # ConPTY can delay reader EOF past child exit; poll liveness
                # as a safety net.
                if not self.pty.isalive():
                    self._finish()
                    break
        except asyncio.CancelledError:
            pass

    def _compute_status(self, threshold: float) -> str:
        if self.exited:
            return STATUS_EXITED
        if not self._saw_output:
            return STATUS_STARTING
        idle_for = self.tracker.idle_for(time.monotonic())
        if idle_for is not None and idle_for >= threshold:
            return STATUS_IDLE
        return STATUS_BUSY

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def status(self, threshold: Optional[float] = None) -> str:
        return self._compute_status(self.idle_threshold if threshold is None else threshold)

    def idle_since(self) -> Optional[float]:
        """Seconds the session has been idle (None when not idle)."""
        idle_for = self.tracker.idle_for(time.monotonic())
        if idle_for is None or idle_for < self.idle_threshold or self.exited:
            return None
        return idle_for

    async def deliver(self, text: str) -> bool:
        """Put ``text`` in front of the agent running here, as a user message.

        **The** way anything automated hands an agent something to act on —
        cflow nudges, mesh deliveries and policy nudges, a spawned child's
        opening task. Call this rather than assembling the write yourself:
        submitting a message correctly means a paste plus a *separately
        written* Enter (see :meth:`paste`), and every hand-rolled variant of
        that has eventually gotten the chunking wrong and left the message
        typed into the composer but never sent.

        Best-effort by design — every caller is a background sender with
        nothing to tell a user. Returns whether it landed, so a caller that
        must not lose the message (mesh delivery advancing its cursor) can
        hold its position and retry on the next tick.
        """
        try:
            await self._await_readable()
            await self._await_keyboard_quiet()
            await self.paste(text, enter=True)
        except Exception as exc:  # noqa: BLE001 — SessionGone, PTY write, ...
            log.debug("deliver to %r failed: %s", self.sdef.name, exc)
            return False
        return True

    async def _await_readable(self) -> None:
        """Block until a starting TUI can actually take a message.

        Quiet is not ready. A just-started Claude Code prints its banner and
        pauses long enough to read as idle, mounts its input a few seconds
        after that, and only finishes loading a few seconds after *that*.
        Written into any of those gaps, a paste and its separately-written
        Enter come back out of one read with the CR folded into the text: the
        message is typed and never sent — the exact failure :meth:`paste`
        splits the writes to avoid, reintroduced by the reader rather than the
        writer. No delay between the two writes can fix it.

        Ready is two things together, and neither alone is enough (both were
        measured against Claude Code):

        * **DECSET 2004 is on.** A program enables bracketed paste when it
          takes over the keyboard, so this is the input existing at all.
        * **...and it has been quiet since.** The mode goes on partway through
          startup, several seconds before the first submit is accepted; the
          burst of work that follows it is what has to finish.

        Latched: once a session has been seen ready it never waits again, so
        this is paid at most once. Only a harness known to set the mode is
        waited on — a shell never does and would stall here forever — and the
        whole wait is bounded from the moment the session was spawned, so a
        harness that never settles delays a message rather than losing it.
        """
        if self._input_ready or self.exited:
            return
        if self.sdef.harness != CLAUDE_HARNESS:
            self._input_ready = True  # not a TUI we know; nothing to wait for
            return
        deadline = self._started_mono + INPUT_READY_TIMEOUT
        quiet_since = None
        while time.monotonic() < deadline and not self.exited:
            if self.screen.bracketed_paste and self.status() == STATUS_IDLE:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= INPUT_SETTLE:
                    break
            else:
                quiet_since = None  # still starting; begin the count again
            await asyncio.sleep(0.2)
        self._input_ready = True

    def note_human_input(self) -> None:
        """Record a human keystroke aimed at this terminal.

        Called by the raw keyboard passthroughs — the WebSocket bridge behind
        the web terminal and ``claunch attach``, and :meth:`send_keys` — and
        never by :meth:`deliver`: telling the two apart is the whole point.
        """
        self._last_human_input = time.monotonic()

    def keyboard_busy(self, guard: Optional[float] = None) -> bool:
        """Whether a human has typed here within the last ``guard`` seconds.

        A composer mid-edit is invisible to the screen sampler — a thinking
        pause reads exactly like idle — so anything about to type into this
        terminal asks about the keyboard directly.
        """
        if guard is None:
            guard = TYPING_GUARD
        if self._last_human_input <= 0:
            return False
        return time.monotonic() - self._last_human_input < guard

    async def _await_keyboard_quiet(self) -> None:
        """Hold a delivery while a human is typing into this terminal.

        The idle-gates upstream cannot catch this by watching the screen:
        typing keeps it changing, but the pauses inside composing a message
        outlast the idle threshold, and a paste-plus-Enter injected into one
        submits the human's half-typed line with the delivery folded into it.
        So the last thing before the paste is the question the screen cannot
        answer — has the keyboard itself been quiet for a moment.

        Bounded, like every wait here: a keyboard that never goes quiet
        delays the message rather than losing it.
        """
        deadline = time.monotonic() + TYPING_HOLD_TIMEOUT
        while not self.exited and time.monotonic() < deadline:
            if not self.keyboard_busy():
                return
            await asyncio.sleep(0.2)

    async def send_keys(self, args: List[str], *, literal: bool = False) -> bytes:
        """Raw keystrokes — the passthrough for a human at a keyboard (the
        web terminal, ``claunch send-keys``). To hand an agent a *message*,
        use :meth:`deliver` instead.
        """
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        self.note_human_input()
        data = keys_mod.encode_keys(
            args, literal=literal, app_cursor=self.screen.app_cursor_keys
        )
        head, submit = keys_mod.split_submit(data)
        if submit and self.screen.bracketed_paste:
            # Text and its submitting CR in one write is the same trap
            # :meth:`paste` documents: a bracketed-paste TUI reads the chunk
            # as one paste and folds the CR into the text, so the line is
            # typed but never sent. Split it here too — this path is reached
            # by hand ('claunch send-keys ... Enter') where the caller has no
            # way to know the difference.
            await self.write_bytes(head)
            await asyncio.sleep(PASTE_ENTER_DELAY)
            await self.write_bytes(submit)
            return data
        await self.write_bytes(data)
        return data

    async def paste(self, text: str, *, enter: bool = False) -> bytes:
        """Inject multiline text as one paste (bracketed when the program
        opted in via DECSET 2004), so newlines don't submit once per line.

        The submitting Enter is a *separate*, delayed write. A bracketed-paste
        TUI (Claude Code and Ink-based prompts generally) treats one read as
        one paste: a CR sitting in the same chunk right after the ``ESC[201~``
        end marker is folded into the pasted text, so the block lands in the
        composer and is never submitted. Landing the CR in its own read is
        what makes it a keypress again.
        """
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        data = keys_mod.encode_paste(text, bracketed=self.screen.bracketed_paste)
        await self.write_bytes(data)
        if not enter:
            return data
        await asyncio.sleep(PASTE_ENTER_DELAY)
        await self.write_bytes(b"\r")
        return data + b"\r"

    async def write_bytes(self, data: bytes) -> None:
        if self.exited or self.pty is None:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        # PTY writes can block briefly (ConPTY pipe backpressure); keep the
        # event loop responsive by writing from the thread pool.
        await self._loop.run_in_executor(None, self.pty.write, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.exited or self.pty is None:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        self.pty.resize(cols, rows)
        self.screen.resize(cols, rows)
        self.sdef = dataclasses.replace(self.sdef, cols=cols, rows=rows)
        self._broadcast(("resize", (cols, rows)))

    def capture(self, *, history: bool = False) -> List[str]:
        lines = self.screen.render_history() if history else self.screen.render_screen()
        return lines

    async def wait_for(self, state: str, *, timeout: float, threshold: float) -> str:
        """Poll until the session reaches ``state`` (``idle`` or ``exited``).

        Returns the final status; raises :class:`asyncio.TimeoutError` on
        timeout. Waiting for idle also completes if the session exits — the
        caller inspects the returned status.
        """
        deadline = time.monotonic() + timeout
        while True:
            current = self._compute_status(threshold)
            if state == "exited" and current == STATUS_EXITED:
                return current
            if state == "idle" and current in (STATUS_IDLE, STATUS_EXITED):
                return current
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.2)

    def kill(self, *, force: bool = False) -> None:
        if self.exited or self.pty is None:
            return
        self.pty.terminate(force=force)

    async def shutdown(self, grace: float = 5.0) -> None:
        """Terminate the child and wait briefly; force-kill stragglers."""
        if self.exited or self.pty is None:
            return
        self.pty.terminate(force=False)
        deadline = time.monotonic() + grace
        while not self.exited and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not self.exited:
            self.pty.terminate(force=True)
            await asyncio.sleep(0.2)
            if not self.exited:
                self._finish()

    # ------------------------------------------------------------------ #
    # subscribers (WebSocket viewers)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        return q

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

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": self.status(),
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
        }


class SessionGone(Exception):
    """Raised when acting on a session whose child already exited."""


class DeadSession:
    """The record of a session that is no longer running.

    Sessions die with the daemon, but their *definitions* outlive it, and so
    does the right to revive them: on restart everything the previous daemon
    did not relaunch — already exited, created ``--no-restore``, or a relaunch
    that failed — comes back as one of these instead of being forgotten, so
    ``claunch respawn`` (and the web UI's resume) still reach it. Only the user
    drops such a record, via ``kill-session`` or ``clear-sessions``.

    It answers the read-only surface a viewer needs (list, capture, attach to
    read the final screen — replayed from the raw log) and raises
    :class:`SessionGone` for anything that needs a live child.
    """

    #: How much of the raw log is replayed to rebuild the last screen: enough
    #: for a full-screen TUI repaint, small enough that attaching stays quick.
    REPLAY_TAIL_BYTES = 256 * 1024

    exited = True

    def __init__(
        self,
        sdef: SessionDef,
        *,
        exit_code: Optional[int] = None,
        pid: Optional[int] = None,
        created_at: Optional[str] = None,
        last_output_at: Optional[str] = None,
        exited_at: Optional[str] = None,
        scrollback: int = 5000,
        idle_threshold: float = 2.0,
    ) -> None:
        self.sdef = sdef
        self.exit_code = exit_code
        self.pid = pid
        self.created_at = created_at or _utcnow()
        self.last_output_at = last_output_at
        self.exited_at = exited_at
        self.idle_threshold = idle_threshold
        self._scrollback = scrollback
        self._screen: Optional[ScreenState] = None

    @property
    def screen(self) -> ScreenState:
        """The session's last screen, rebuilt on first use.

        Replaying a log through pyte is not free, and most records are never
        looked at — so this stays unbuilt until someone captures or attaches.
        """
        if self._screen is None:
            screen = ScreenState(
                self.sdef.cols, self.sdef.rows, history=self._scrollback
            )
            self._replay_into(screen)
            self._screen = screen
        return self._screen

    def _replay_into(self, screen: ScreenState) -> None:
        path = paths.session_log(self.sdef.name)
        try:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > self.REPLAY_TAIL_BYTES:
                    fh.seek(size - self.REPLAY_TAIL_BYTES)
                screen.feed(fh.read())
        except OSError:
            pass  # no log (or unreadable): an empty screen is honest enough

    # ------------------------------------------------------------------ #
    # the live surface: nothing to drive any more
    # ------------------------------------------------------------------ #
    def _gone(self) -> SessionGone:
        return SessionGone(
            f"session {self.sdef.name!r} has exited "
            f"(respawn it to get a live terminal back)"
        )

    async def send_keys(self, args: List[str], *, literal: bool = False) -> bytes:
        raise self._gone()

    async def paste(self, text: str, *, enter: bool = False) -> bytes:
        raise self._gone()

    async def deliver(self, text: str) -> bool:
        return False  # nothing is running to read it

    async def write_bytes(self, data: bytes) -> None:
        raise self._gone()

    def resize(self, cols: int, rows: int) -> None:
        raise self._gone()

    def kill(self, *, force: bool = False) -> None:
        return None

    async def shutdown(self, grace: float = 5.0) -> None:
        return None

    # ------------------------------------------------------------------ #
    # the passive surface: same answers a live session would give
    # ------------------------------------------------------------------ #
    def status(self, threshold: Optional[float] = None) -> str:
        return STATUS_EXITED

    def idle_since(self) -> Optional[float]:
        return None

    def note_human_input(self) -> None:
        return None  # nobody is typing at a terminal that no longer exists

    def keyboard_busy(self, guard: Optional[float] = None) -> bool:
        return False

    def capture(self, *, history: bool = False) -> List[str]:
        return self.screen.render_history() if history else self.screen.render_screen()

    async def wait_for(self, state: str, *, timeout: float, threshold: float) -> str:
        return STATUS_EXITED  # exited satisfies both 'idle' and 'exited'

    def subscribe(self) -> asyncio.Queue:
        return asyncio.Queue(maxsize=1)  # nothing will ever be published to it

    def unsubscribe(self, q: asyncio.Queue) -> None:
        return None

    def info(self) -> dict:
        return {
            **self.sdef.to_dict(),
            "status": STATUS_EXITED,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
        }
