"""The VM screen for the dashboard (vm-setup-8s6, step 1c): JPEG frames the
daemon takes from the guest while someone is looking.

d-003 measured the ways to get a frame out of the guest (10 runs, p50):
``screencapture`` to a temp file and ``cat`` over the same ssh 206 ms (504 KB),
that with scp 297 ms, PNG with scp 364 ms, cua ``get_desktop_state`` with scp
567 ms. ``screencapture`` has no stdout mode, hence the temp file (a path in the
*guest's* /tmp, removed in the same command). The first is used; the cua
capture stays the session's ``vm_screenshot``, which also returns the
coordinate mapping a model needs.

Capture runs only while a viewer asks: each ``GET /api/vm/{id}/frame`` marks
the VM as watched for :data:`WATCH` seconds, and the loop for that VM stops
when nobody has asked for that long. One loop per VM, however many viewers.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

from .vm_tart import TART
from .vmhub import Registry

WATCH = 6.0
INTERVAL = 0.35
BACKOFF = 3.0
CAPTURE_TIMEOUT = 15.0
#: The capture command, run by the guest's shell. The temp file is the
#: guest's own and goes away in the same command.
#: (Not a dot-file name: screencapture refuses to write one, measured on t03.)
#: Files an earlier capture left (its tart exec was killed on a timeout) are
#: removed first: one poller runs per VM, so none of them is in use.
CAPTURE = ('/bin/rm -f /tmp/claunch-frame-*.jpg; f=/tmp/claunch-frame-$$.jpg; '  # tmp-paths: guest
           '/usr/sbin/screencapture -x -t jpg "$f" && /bin/cat "$f"; rc=$?; /bin/rm -f "$f"; exit $rc')

BytesResult = Tuple[int, bytes, str]
BytesRunner = Callable[[Sequence[str], float], BytesResult]


def run_bytes(argv: Sequence[str], timeout: float) -> BytesResult:
    try:
        done = subprocess.run(list(argv), capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, b"", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, b"", str(exc)
    return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace")


def capture_argv(vm) -> list:
    """The capture runs in the guest through ``tart exec`` (~0.14 s a frame
    after the first, measured on t03)."""
    return [TART, "exec", "--", vm.tart_name, "/bin/sh", "-c", CAPTURE]


@dataclass
class Frame:
    seq: int = 0
    data: bytes = b""
    at: float = 0.0
    error: str = ""


@dataclass
class _Watch:
    frame: Frame = field(default_factory=Frame)
    last_ask: float = 0.0
    task: Optional[asyncio.Task] = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class Frames:
    def __init__(self, registry: Registry, *, run: BytesRunner = run_bytes,
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = INTERVAL, watch: float = WATCH,
                 backoff: float = BACKOFF) -> None:
        self.registry = registry
        self._run = run
        self._clock = clock
        self.interval = interval
        self.watch = watch
        self.backoff = backoff
        self._w: Dict[str, _Watch] = {}

    def watched(self, vm_id: str) -> bool:
        w = self._w.get(vm_id)
        return bool(w and w.task and not w.task.done())

    async def get(self, vm_id: str, since: int = 0, wait: float = 2.0) -> Frame:
        """The newest frame, starting the loop if nobody is watching. With
        ``since`` it waits up to ``wait`` seconds for a newer one."""
        self.registry.get(vm_id)  # unknown VM: VmError
        w = self._w.setdefault(vm_id, _Watch())
        w.last_ask = self._clock()
        if w.task is None or w.task.done():
            w.task = asyncio.create_task(self._loop(vm_id, w))
        if w.frame.seq <= since:
            w.changed.clear()
            try:
                await asyncio.wait_for(w.changed.wait(), wait)
            except asyncio.TimeoutError:
                pass
        return w.frame

    async def _loop(self, vm_id: str, w: _Watch) -> None:
        while self._clock() - w.last_ask < self.watch:
            vm = self.registry.get(vm_id)
            if vm.state != "running" or not vm.ssh_ready:
                self._set(w, error=f"{vm_id} is {vm.state}" + ("" if vm.ssh_ready else ", the guest agent is not up"))
                await asyncio.sleep(self.backoff)
                continue
            rc, out, err = await asyncio.to_thread(self._run, capture_argv(vm), CAPTURE_TIMEOUT)
            if rc == 0 and out[:2] == b"\xff\xd8":
                self._set(w, data=out)
                await asyncio.sleep(self.interval)
            else:
                self._set(w, error=f"capture failed (rc {rc}): {err.strip()[:200] or 'no image'}")
                await asyncio.sleep(self.backoff)

    def _set(self, w: _Watch, *, data: bytes = b"", error: str = "") -> None:
        if data:
            w.frame = Frame(seq=w.frame.seq + 1, data=data, at=time.time())
        else:
            w.frame = Frame(seq=w.frame.seq + 1, data=b"", at=time.time(), error=error)
        w.changed.set()

    async def stop_all(self) -> None:
        for w in self._w.values():
            if w.task is not None:
                w.task.cancel()
        for w in self._w.values():
            if w.task is not None:
                try:
                    await w.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
