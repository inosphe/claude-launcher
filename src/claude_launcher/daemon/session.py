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
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from . import keys as keys_mod
from . import paths, pty_backend
from .harness import SessionDef
from .idle import IdleTracker
from .screen import ScreenState

#: Screen sampling cadence for idle detection (seconds).
SAMPLE_INTERVAL = 0.4

#: Rotate the raw output log beyond this size (a single .1 backup is kept).
LOG_MAX_BYTES = 10 * 1024 * 1024

STATUS_STARTING = "starting"
STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_EXITED = "exited"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    """Owns one PTY child; created and torn down by the SessionManager."""

    def __init__(
        self,
        sdef: SessionDef,
        argv: List[str],
        env: Dict[str, str],
        cwd: str,
        *,
        idle_threshold: float,
        scrollback: int,
    ) -> None:
        self.sdef = sdef
        self.argv = argv
        self.idle_threshold = idle_threshold
        self.screen = ScreenState(sdef.cols, sdef.rows, history=scrollback)
        self.tracker = IdleTracker()
        self.created_at = _utcnow()
        self.last_output_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.exited = False
        self._started_mono = time.monotonic()
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop = asyncio.get_running_loop()
        self._status = STATUS_STARTING
        self._saw_output = False

        session_dir = paths.session_dir(sdef.name)
        session_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = paths.session_log(sdef.name)
        self._log = open(self._log_path, "ab")

        self.pty = pty_backend.spawn(argv, env=env, cwd=cwd, cols=sdef.cols, rows=sdef.rows)
        self.pid = self.pty.pid

        # A dedicated *daemon* thread, NOT the loop's default executor: at
        # daemon exit asyncio joins executor threads, and a reader stuck in a
        # blocking ConPTY read would hold the whole process (and its singleton
        # lock) hostage. A daemon thread can never block interpreter exit.
        self._reader = threading.Thread(
            target=self._read_pump, name=f"pty-read-{sdef.name}", daemon=True
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
            "exited_at": _utcnow(),
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

    async def send_keys(self, args: List[str], *, literal: bool = False) -> bytes:
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        data = keys_mod.encode_keys(
            args, literal=literal, app_cursor=self.screen.app_cursor_keys
        )
        await self.write_bytes(data)
        return data

    async def write_bytes(self, data: bytes) -> None:
        if self.exited:
            raise SessionGone(f"session {self.sdef.name!r} has exited")
        # PTY writes can block briefly (ConPTY pipe backpressure); keep the
        # event loop responsive by writing from the thread pool.
        await self._loop.run_in_executor(None, self.pty.write, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.exited:
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
        if self.exited:
            return
        self.pty.terminate(force=force)

    async def shutdown(self, grace: float = 5.0) -> None:
        """Terminate the child and wait briefly; force-kill stragglers."""
        if self.exited:
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
        }


class SessionGone(Exception):
    """Raised when acting on a session whose child already exited."""
