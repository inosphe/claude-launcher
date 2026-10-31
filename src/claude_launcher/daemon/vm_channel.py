"""The guest channel (vm-setup-8s6, step 4): one ``tart exec -i ... cua-driver mcp``
per VM, held by the daemon and spoken to as an MCP client.

Why the daemon holds it (vm-setup-q0g.3, d-002 items 1-2): a session that
registers the guest command itself loses its tools when the connection drops, and the
harness does not bring them back; the failure never shows as an error. Here
the sessions talk to the daemon's own always-connected MCP server, and what
happens on the guest side is this module's business: a dropped exec is
reconnected a bounded number of times with growing pauses, and a call made
while the channel is down is answered with ``vm_unavailable`` rather than
hanging or vanishing.

The first ``cua-driver mcp`` on a booted guest also starts the cua daemon
(it does not start on its own, d-002 item 3). A connection counts as up only
after the MCP handshake and a cheap tool call have been answered, so
``cua_ready`` means a call went through, not that a process exists.

Messages are newline-delimited JSON-RPC 2.0 on the child's stdin and stdout.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from . import vmhub
from .vm_tart import TART
from .vmhub import Registry, VmError

BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)
HELLO_TIMEOUT = 20.0
CALL_TIMEOUT = 30.0
PROTOCOL = "2024-11-05"
#: Asked once after the handshake: needs no permission, answers fast.
PROBE_TOOL = "get_screen_size"
#: A connection that closes sooner than this after coming up counts as a
#: failed attempt: otherwise a guest that accepts and drops would be
#: reconnected for ever.
MIN_UPTIME = 10.0
#: One JSON-RPC message is one line, and a screenshot or the tool list is far
#: over asyncio's 64 KiB default line limit (⑥: tools/list closed the channel
#: with a closed channel until this was raised).
READ_LIMIT = 64 * 1024 * 1024


class ChannelDown(Exception):
    pass


def exec_argv(vm: vmhub.Vm) -> List[str]:
    """``tart exec -i``: stdin and stdout of the host process are the guest
    process's. cua_path is absolute (PATH is minimal there)."""
    return [TART, "exec", "-i", "--", vm.tart_name, vm.cua_path, "mcp"]


async def open_process(argv: Sequence[str], log: Path):
    log.parent.mkdir(parents=True, exist_ok=True)
    vmhub.rotate_log(log)
    err = open(log, "ab")
    try:
        return await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=err, limit=READ_LIMIT,
        )
    finally:
        err.close()


Opener = Callable[[Sequence[str], Path], Awaitable[object]]
Reaper = Callable[[vmhub.Vm], Awaitable[None]]


async def reap_stale(vm: vmhub.Vm, timeout: float = 10.0) -> None:
    """End the ``cua-driver mcp`` processes left in the guest by earlier
    channels. When the host side of a ``tart exec -i`` dies (killed, or closed
    and terminated) the guest process does not: measured on t03, one stays per
    reconnect, for ever. There is one channel per VM, so any ``mcp`` process
    there is stale. (``serve``, the shared daemon, is not matched; the
    bracket keeps pkill from matching its own command line.)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            TART, "exec", "--", vm.tart_name, "/usr/bin/pkill", "-f", "[c]ua-driver mcp",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout)  # rc 1: nothing to end
    except Exception:  # noqa: BLE001 -- best effort; the channel's own start tells
        pass


class Channel:
    def __init__(
        self,
        registry: Registry,
        vm_id: str,
        *,
        log_dir: Path,
        opener: Opener = open_process,
        backoff: Sequence[float] = BACKOFF,
        hello_timeout: float = HELLO_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_failed: Optional[Callable[[str, str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        reap: Optional[Reaper] = None,
    ) -> None:
        self.registry = registry
        self.vm_id = vm_id
        self.log = log_dir / vm_id / "channel.log"
        self._opener = opener
        # Only the real opener leaves processes in a real guest.
        self._reap = reap if reap is not None else (reap_stale if opener is open_process else None)
        self._backoff = tuple(backoff)
        self._hello_timeout = hello_timeout
        self._sleep = sleep
        self._on_failed = on_failed
        self._proc = None
        self._task: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next = 0
        self._up = asyncio.Event()
        self._closing = False
        self._reader_task: Optional[asyncio.Task] = None
        self._clock = clock

    # ---- public --------------------------------------------------------- #
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._kill()
        self._fail_pending(ChannelDown("channel stopped"))
        if self.vm_id in {v.id for v in self.registry.list()}:
            self.registry.set_channel(self.vm_id, "idle")

    async def wait_up(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._up.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def call(self, name: str, arguments: Optional[dict] = None,
                   timeout: float = CALL_TIMEOUT) -> dict:
        """``tools/call``. The MCP result comes back as is (``isError`` set
        by the tool is the tool's answer, not a channel fault)."""
        return await self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout)

    async def tools(self, timeout: float = CALL_TIMEOUT) -> List[dict]:
        return (await self._request("tools/list", {}, timeout)).get("tools") or []

    # ---- JSON-RPC ------------------------------------------------------- #
    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        proc = self._proc
        if proc is None or not self._up.is_set():
            vm = self.registry.get(self.vm_id)
            raise VmError("vm_unavailable", f"{self.vm_id}: guest channel is {vm.channel}",
                          retry_after=5, channel=vm.channel)
        return await self._send(proc, method, params, timeout)

    async def _send(self, proc, method: str, params: dict, timeout: float) -> dict:
        self._next += 1
        ident = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[ident] = fut
        try:
            proc.stdin.write((json.dumps(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
            ) + "\n").encode())
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # The guest may still do it: the caller cannot tell a call that
            # never ran from one whose answer was lost.
            raise VmError("vm_timeout", f"{method} not answered in {timeout:g}s",
                          outcome_unknown=True) from None
        except (ChannelDown, OSError, ConnectionError) as exc:
            raise VmError("vm_unavailable", f"guest channel dropped: {exc}",
                          retry_after=5, outcome_unknown=True) from None
        finally:
            self._pending.pop(ident, None)

    def _notify(self, proc, method: str) -> None:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())

    def _fail_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _reader(self, proc) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                self._dispatch(proc, msg)
            except Exception:  # noqa: BLE001 -- one odd message must not end the channel
                continue

    def _dispatch(self, proc, msg: dict) -> None:
        ident = msg.get("id")
        if isinstance(ident, bool) or not isinstance(ident, (int, str)):
            return
        if "result" in msg or "error" in msg:
            fut = self._pending.get(ident)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg["error"]
                    detail = err if isinstance(err, dict) else {"message": err}
                    fut.set_exception(VmError(
                        "vm_guest_error", str(detail.get("message") or detail),
                        rpc_code=detail.get("code")))
                else:
                    result = msg.get("result")
                    fut.set_result(result if isinstance(result, dict) else {})
        elif msg.get("method") == "ping":
            proc.stdin.write((json.dumps(
                {"jsonrpc": "2.0", "id": ident, "result": {}}) + "\n").encode())

    # ---- connection ------------------------------------------------------ #
    async def _connect(self) -> None:
        vm = self.registry.get(self.vm_id)
        if self._reap is not None:
            await self._reap(vm)
        self._proc = proc = await self._opener(exec_argv(vm), self.log)
        reader = asyncio.create_task(self._reader(proc))
        self._reader_task = reader
        try:
            await self._send(proc, "initialize", {
                "protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "claunch-vmhub", "version": "1"},
            }, self._hello_timeout)
            self._notify(proc, "notifications/initialized")
            await self._send(proc, "tools/call", {"name": PROBE_TOOL, "arguments": {}},
                             self._hello_timeout)
        except BaseException:
            reader.cancel()
            await self._kill()
            raise

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._up.clear()
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for call in ("terminate",):
            try:
                getattr(proc, call)()
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    async def _supervise(self) -> None:
        failures = 0
        while not self._closing:
            vm = self.registry.get(self.vm_id)
            if vm.state != "running" or not vm.ssh_ready:
                self.registry.set_channel(self.vm_id, "idle")
                return
            self.registry.set_channel(self.vm_id, "connecting")
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- any failure is one more attempt
                failures += 1
                error = f"{type(exc).__name__}: {exc}"
            else:
                came_up = self._clock()
                self.registry.set_channel(self.vm_id, "up")
                self._up.set()
                await self._reader_task_done()
                self._up.clear()
                self._fail_pending(ChannelDown("the guest channel closed"))
                await self._kill()
                error = "the guest channel closed"
                failures = failures + 1 if self._clock() - came_up < MIN_UPTIME else 0
            if failures >= len(self._backoff):
                self.registry.set_channel(
                    self.vm_id, "failed", f"{failures} attempts failed; last: {error}")
                if self._on_failed is not None:
                    self._on_failed(self.vm_id, error)
                return
            self.registry.set_channel(self.vm_id, "down", error)
            await self._sleep(self._backoff[min(failures, len(self._backoff) - 1)])

    async def _reader_task_done(self) -> None:
        try:
            await self._reader_task
        except asyncio.CancelledError:
            if self._closing:
                raise
        except Exception:  # noqa: BLE001
            pass


class Hub:
    """The channels of the registered VMs: one per running VM."""

    def __init__(self, registry: Registry, *, log_dir: Path, **channel_args) -> None:
        self.registry = registry
        self.log_dir = log_dir
        self._args = channel_args
        self.channels: Dict[str, Channel] = {}

    def ensure(self, vm_id: str) -> Channel:
        ch = self.channels.get(vm_id)
        if ch is None:
            ch = self.channels[vm_id] = Channel(
                self.registry, vm_id, log_dir=self.log_dir, **self._args)
        ch.start()
        return ch

    async def drop(self, vm_id: str) -> None:
        ch = self.channels.pop(vm_id, None)
        if ch is not None:
            await ch.stop()

    async def drop_all(self) -> None:
        for vm_id in list(self.channels):
            await self.drop(vm_id)

    def get(self, vm_id: str) -> Channel:
        ch = self.channels.get(vm_id)
        if ch is None:
            vm = self.registry.get(vm_id)
            raise VmError("vm_unavailable", f"{vm_id} has no guest channel ({vm.state})",
                          retry_after=5)
        return ch
