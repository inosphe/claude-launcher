"""The Stats page's reading, done in a process of its own.

Parsing a transcript is pure Python: a thread takes it off the event loop's
schedule but not off the GIL, so a cold read of a long transcript (1.7 s for
a 267 MB one, claunch-3r94d) still made every other callback on the loop --
terminal frames, API answers -- wait in turns behind it (the effect
claunch-vyw9s measured). Here the reading runs in one long-lived child
process that the daemon talks to over a pipe, one JSON line each way.

The child keeps :mod:`tokenusage`'s followers between requests, so only the
first read of a transcript is cold, exactly as in-process; the background
catch-up and the ``partial`` flag work the same way inside it.

The daemon side (:class:`StatsWorker`) starts the child on first use, sends
one request at a time, and treats anything but a well-formed reply -- the
child could not start, exited, answered out of turn, or took longer than
:data:`CALL_TIMEOUT` -- as :class:`WorkerUnavailable`: it kills the child
(the next call starts a new one) and the caller reads in a thread instead.
A failed start is not retried for :data:`RETRY_AFTER` seconds, so a machine
where the child cannot run pays the attempt once, not on every request.
An exception *inside* the reading is :class:`WorkerError`; reading the same
file in the daemon would raise the same one, so it is not retried.

Protocol (UTF-8 JSON, one object per line)::

    -> {"id": 1, "op": "read", "head": {...}, "path": "...", "unit": "day",
        "offset": 32400}
    <- {"id": 1, "ok": true, "result": {...}}
    -> {"id": 2, "op": "compare", "items": [{"head": ..., "path": ...}, ...],
        "unit": "day", "offset": 32400}
    <- {"id": 2, "ok": false, "error": "..."}

``offset`` is the daemon's UTC offset in seconds, so buckets follow the
daemon's day even if the child's environment differs. The child exits when
its stdin closes, which includes the daemon dying.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from datetime import timedelta, timezone
from typing import Any, List, Optional

MODULE = "claude_launcher.daemon.statsworker"

#: Longest one request may take before the child is presumed stuck. The
#: inline read is bounded by ``sessionstats.STATS_BUDGET`` (the rest goes to
#: the child's catch-up thread), so a healthy answer comes in seconds.
CALL_TIMEOUT = 60.0

#: After a failed start, requests read in the daemon for this long.
RETRY_AFTER = 300.0

#: Largest reply line accepted (a compare of MAX_SESSIONS week readings is
#: well under 1 MB).
LINE_LIMIT = 16 * 1024 * 1024


class WorkerUnavailable(Exception):
    """The child gave no usable answer; read in the daemon instead."""


class WorkerError(Exception):
    """The reading itself raised, in the child."""


# -- child ------------------------------------------------------------------

def handle(request: dict) -> Any:
    """One request's result (the child's side; also callable in-process)."""
    from . import sessionstats, statscompare

    op = request.get("op")
    if op == "ping":
        return "pong"
    unit = request.get("unit", "day")
    offset = request.get("offset")
    zone = timezone(timedelta(seconds=offset)) if offset is not None else None
    if op == "read":
        return sessionstats.read_located(request["head"], request.get("path"),
                                         unit, zone)
    if op == "compare":
        readings = [sessionstats.read_located(i["head"], i.get("path"), unit, zone)
                    for i in request["items"]]
        return statscompare.compare(readings, unit)
    raise ValueError(f"unknown op {op!r}")


def serve(stdin, stdout) -> None:
    """Answer requests from ``stdin`` (binary) on ``stdout`` until EOF."""
    for raw in iter(stdin.readline, b""):
        rid = None
        try:
            request = json.loads(raw)
            rid = request.get("id")
            reply = {"id": rid, "ok": True, "result": handle(request)}
        except Exception as exc:  # noqa: BLE001 -- the daemon decides
            reply = {"id": rid, "ok": False,
                     "error": f"{type(exc).__name__}: {exc}",
                     "trace": traceback.format_exc(limit=8)}
        stdout.write(json.dumps(reply, ensure_ascii=True).encode("ascii") + b"\n")
        stdout.flush()


def main() -> None:
    out = sys.stdout.buffer
    # Nothing but replies may reach the pipe: a stray print would be read as
    # a malformed answer.
    sys.stdout = sys.stderr
    serve(sys.stdin.buffer, out)


# -- daemon side ------------------------------------------------------------

def child_env() -> dict:
    """The child's environment: the daemon's, with this package's own
    source root first on ``PYTHONPATH`` -- the child must run the code the
    daemon runs, not whichever copy the interpreter would find."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ)
    rest = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root + (os.pathsep + rest if rest else "")
    return env


def _creationflags() -> int:
    if sys.platform == "win32":
        return 0x08000000   # CREATE_NO_WINDOW: the daemon has no console
    return 0


class StatsWorker:
    """The daemon's handle on the child. One request at a time."""

    def __init__(self, argv: Optional[List[str]] = None,
                 timeout: float = CALL_TIMEOUT,
                 retry_after: float = RETRY_AFTER) -> None:
        self.argv = argv or [sys.executable, "-m", MODULE]
        self.timeout = timeout
        self.retry_after = retry_after
        self.starts = 0             # children started, for tests and /api
        self.fallbacks = 0          # calls answered by WorkerUnavailable
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock: Optional[asyncio.Lock] = None
        self._seq = 0
        self._down_until = 0.0

    @property
    def pid(self) -> Optional[int]:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    async def _ensure(self) -> asyncio.subprocess.Process:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            return proc
        if time.monotonic() < self._down_until:
            raise WorkerUnavailable("the stats worker failed to start recently")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=LINE_LIMIT,
                env=child_env(),
                creationflags=_creationflags(),
            )
        except (OSError, ValueError) as exc:
            self._down_until = time.monotonic() + self.retry_after
            raise WorkerUnavailable(f"could not start the stats worker: {exc}") from exc
        self._proc = proc
        self.starts += 1
        return proc

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass

    async def call(self, request: dict) -> Any:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                return await self._call(request)
            except WorkerUnavailable:
                self.fallbacks += 1
                raise

    async def _call(self, request: dict) -> Any:
        proc = await self._ensure()
        self._seq += 1
        seq = self._seq
        line = json.dumps({**request, "id": seq}).encode("utf-8") + b"\n"
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), self.timeout)
        except asyncio.TimeoutError:
            await self._kill()
            raise WorkerUnavailable(
                f"the stats worker gave no answer in {self.timeout:g}s") from None
        except (OSError, ValueError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as exc:
            await self._kill()
            raise WorkerUnavailable(f"the stats worker pipe failed: {exc}") from exc
        if not raw:
            await self._kill()
            raise WorkerUnavailable("the stats worker exited")
        try:
            reply = json.loads(raw)
        except ValueError:
            await self._kill()
            raise WorkerUnavailable("the stats worker answered garbage") from None
        if not isinstance(reply, dict) or reply.get("id") != seq:
            await self._kill()
            raise WorkerUnavailable("the stats worker answered out of turn")
        if not reply.get("ok"):
            raise WorkerError(reply.get("error") or "the stats worker failed")
        return reply.get("result")

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.close()      # the child's EOF: it exits by itself
            await asyncio.wait_for(proc.wait(), 3)
        except (asyncio.TimeoutError, OSError):
            self._proc = proc
            await self._kill()


def zone_offset() -> int:
    """The daemon's UTC offset now, in seconds (see ``sessionstats.local_zone``)."""
    from .sessionstats import local_zone
    from datetime import datetime

    delta = datetime.now(local_zone()).utcoffset()
    return int(delta.total_seconds()) if delta is not None else 0


if __name__ == "__main__":
    main()
