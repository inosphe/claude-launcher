"""Running the VMs (vm-setup-8s6, step 2): ``tart`` and the guest's ssh, kept
behind two small runners so the rules in :mod:`vmhub` can be driven without a
VM.

What the d-002 measurements decided (vm-setup-q0g.2, runs/d-002):

* ``tart run`` always carries ``--suspendable``; a VM started without it
  cannot be suspended (suspend ~4 s, resume to ssh ~7 s). A resume fails
  while the host console screen is locked (d-006), so :meth:`Manager.start`
  checks that first; the default way to keep a VM's state is a clean stop
  and a cold boot.
* A VM is stopped from inside: ``sync; sudo shutdown -h now``, then wait for
  tart to report ``stopped`` (~34 s). ``tart stop`` returned in 0.24 s and
  lost unsynced writes in 2 of 3 runs, so it is never the first move: a
  guest that does not stop in time leaves the VM ``stopping`` and the
  caller decides on :meth:`Manager.force_stop`, which is recorded.
* The VM process is started detached (its own session, output to a log) so
  a daemon restart does not end it; :meth:`Manager.reconcile` adopts what
  ``tart list`` reports.
* Booting to ssh took ~9 s and to a console login ~9 s; the guest's cua
  daemon does not start on its own (the guest channel, step 4, starts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ipaddress
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import vm_share, vmhub
from .vmhub import Registry, VmError

TART = "tart"
STOP_WAIT = 120.0
BOOT_WAIT = 90.0
SUSPEND_WAIT = 60.0
CLONE_WAIT = 900.0
POLL = 2.0
OUTPUT_CAP = 64 * 1024
EXEC_MAX = 600.0

#: Guest traffic to the host's own addresses stays closed: with softnet the
#: gateway IS the host (d-005), so every host port would otherwise be open to
#: the guest. The guest's /29 is random per boot, so the block is by range.
SOFTNET_BLOCK = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

def parse_softnet(raw_softnet, raw_block) -> Tuple[bool, str]:
    """The softnet settings as the manager uses them. The block list is what
    keeps a guest off the host's own ports, so a value that is not a list of
    CIDRs (or is empty) is replaced by the default rather than read as "no
    block"; ``vm_softnet`` is off only when it says so in a plain way."""
    if isinstance(raw_softnet, str):
        on = raw_softnet.strip().lower() not in ("false", "no", "off", "0")
    else:
        on = raw_softnet is not False and raw_softnet != 0
    parts = raw_block if isinstance(raw_block, (list, tuple)) else str(raw_block or "").split(",")
    nets = []
    for part in parts:
        part = str(part).strip()
        if not part:
            continue
        try:
            nets.append(str(ipaddress.ip_network(part, strict=False)))
        except ValueError:
            return on, SOFTNET_BLOCK
    return on, ",".join(nets) or SOFTNET_BLOCK


def host_locked(run: "Runner" = None) -> bool:
    """Whether the host's console screen is locked. Restoring a suspended VM
    fails while it is (VZErrorDomain Code=12, d-006); the key is present in
    ``ioreg -n Root -d1`` only during a lock. A failed query reads as not
    locked: the resume then runs and reports its own error."""
    rc, out, _ = (run or run_command)(["ioreg", "-n", "Root", "-d1"], 10)
    return rc == 0 and re.search(r'"CGSSessionScreenIsLocked"\s*=\s*Yes', out) is not None


def host_ipv4s(run: "Runner" = None) -> List[str]:
    """This host's IPv4 addresses (``ifconfig -a``); empty when the query fails."""
    rc, out, _ = (run or run_command)(["ifconfig", "-a"], 10)
    return re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", out) if rc == 0 else []


#: What tart exec prints when the agent or the VM is not there, as opposed to
#: what the guest command itself printed (measured on tart 2.41.1: d-005,
#: s2 t03 runs of 2026-10-31).
AGENT_ERROR = re.compile(
    r'Error: unavailable \(\d+\): .*|VM ".*" is not running|the specified VM ".*" does not exist|'
    r'Error: .*guest agent.*', re.I)


def is_agent_error(rc: int, out: str, err: str) -> bool:
    """tart exec's own failure rather than the guest command's: stderr is
    exactly one of tart's lines (a command that failed prints its own text and
    usually more than that), and it failed."""
    return rc != 0 and AGENT_ERROR.fullmatch((err or "").strip()) is not None

#: (returncode, stdout, stderr)
Result = Tuple[int, str, str]
Runner = Callable[[Sequence[str], float], Result]
#: ``(argv, feed, drain, timeout) -> StreamResult`` -- see :func:`run_stream`.
Streamer = Callable[..., "StreamResult"]
Spawner = Callable[[Sequence[str], Path], int]


#: What run_command keeps of each stream; the rest is read and dropped so a
#: guest command cannot fill the daemon's memory (exec cuts to OUTPUT_CAP).
READ_CAP = 4 * OUTPUT_CAP


def _drain(stream, cap: int, into: list) -> None:
    kept = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        if kept < cap:
            into.append(chunk[:cap - kept])
            kept += len(chunk[:cap - kept])


def run_command(argv: Sequence[str], timeout: float = 30.0, cap: int = READ_CAP) -> Result:
    try:
        # No stdin: a command that reads it would wait for ever (d-003).
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    except OSError as exc:
        return 127, "", str(exc)
    out: list = []
    err: list = []
    readers = [threading.Thread(target=_drain, args=(s, cap, b), daemon=True)
               for s, b in ((proc.stdout, out), (proc.stderr, err))]
    for r in readers:
        r.start()
    try:
        rc = proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        rc = 124
        err.append(f"timed out after {timeout:g}s".encode())
    for r in readers:
        r.join(5)
    text = lambda parts: b"".join(parts).decode("utf-8", "replace")  # noqa: E731
    return rc, text(out), text(err)


@dataclass
class StreamResult:
    rc: int
    stderr: str
    #: What a ``feed`` or ``drain`` raised (the process is killed when one does)
    feed_error: Optional[BaseException] = None
    drain_error: Optional[BaseException] = None
    timed_out: bool = False


def run_stream(argv: Sequence[str], feed: Optional[Callable] = None,
               drain: Optional[Callable] = None, timeout: float = 600.0,
               cap: int = READ_CAP) -> StreamResult:
    """Run ``argv`` with bytes streamed in and/or out (file transfer).

    ``feed(stdin)`` writes the process's input (in its own thread; stdin is
    closed after it returns), ``drain(stdout)`` reads all of its output (in the
    caller's thread; output nobody drains is read and dropped). The process is
    killed when either raises or ``timeout`` passes; stderr is kept to ``cap``.
    """
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return StreamResult(127, str(exc))
    err: list = []
    result = StreamResult(0, "")
    stderr_reader = threading.Thread(target=_drain, args=(proc.stderr, cap, err), daemon=True)
    stderr_reader.start()

    def feeder():
        try:
            feed(proc.stdin)
        except BrokenPipeError:
            pass    # the guest side ended first; its exit status says why
        except BaseException as exc:  # noqa: BLE001 -- handed to the caller
            result.feed_error = exc
            proc.kill()
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    feeding = None
    if feed:
        feeding = threading.Thread(target=feeder, daemon=True)
        feeding.start()

    def expired():
        result.timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, expired)
    timer.daemon = True
    timer.start()
    try:
        if drain:
            drain(proc.stdout)
        _drain(proc.stdout, 0, [])      # whatever is left is read and dropped
    except BaseException as exc:  # noqa: BLE001 -- handed to the caller
        result.drain_error = exc
        proc.kill()
    finally:
        result.rc = proc.wait()
        timer.cancel()
    if feeding:
        feeding.join(5)
    stderr_reader.join(5)
    result.stderr = b"".join(err).decode("utf-8", "replace")
    if result.timed_out:
        result.rc = 124
    return result


def spawn_detached(argv: Sequence[str], log: Path) -> int:
    """Start ``argv`` in its own session, output to ``log``; return its pid."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
            start_new_session=True,
        )
    return proc.pid


@dataclass
class TartVm:
    name: str
    state: str
    running: bool
    source: str
    #: GB tart reports as used on disk, and as the disk's size
    size_gb: int = 0
    disk_gb: int = 0


def _gb(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_list(text: str) -> List[TartVm]:
    try:
        rows = json.loads(text)
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or "Name" not in row:
            continue
        out.append(TartVm(
            name=str(row["Name"]),
            state=str(row.get("State") or ("running" if row.get("Running") else "stopped")).lower(),
            running=bool(row.get("Running")),
            source=str(row.get("Source") or "").lower(),
            size_gb=_gb(row.get("Size")), disk_gb=_gb(row.get("Disk")),
        ))
    return out


#: The guest's own orderly shutdown, by absolute path (PATH is minimal under
#: tart exec; plain ``shutdown`` is not found).
SHUTDOWN = ("/usr/bin/sudo -n /usr/bin/true || exit 77; /bin/sync; "
            "(/usr/bin/sudo -n /sbin/shutdown -h now </dev/null >/dev/null 2>&1 &)")


def _tail(log: Optional[Path], lines: int = 3) -> str:
    try:
        text = Path(log).read_text(encoding="utf-8", errors="replace") if log else ""
    except OSError:
        return "no output"
    rows = [r.strip() for r in text.strip().splitlines() if r.strip()]
    return " | ".join(rows[-lines:]) or "no output"


def pid_alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _operation(fn):
    """A lifecycle call holds its VM for its whole run: settle() and
    reconcile() leave a VM alone while one of these has it."""
    @functools.wraps(fn)
    def wrapper(self, vm_id, *args, **kwargs):
        with self._working(vm_id):
            return fn(self, vm_id, *args, **kwargs)
    return wrapper


class Manager:
    """Starts, stops, suspends and re-adopts the registered VMs."""

    def __init__(
        self,
        registry: Registry,
        *,
        log_dir: Path,
        run: Runner = run_command,
        stream: Streamer = run_stream,
        spawn: Spawner = spawn_detached,
        alive: Callable[[int], bool] = pid_alive,
        graphics: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        stop_wait: float = STOP_WAIT,
        suspend_wait: float = SUSPEND_WAIT,
        boot_wait: float = BOOT_WAIT,
        softnet: bool = True,
        softnet_block: str = SOFTNET_BLOCK,
        locked: Optional[Callable[[], bool]] = None,
        host_addrs: Optional[Callable[[], List[str]]] = None,
        share_roots: Optional[Callable[[], List[str]]] = None,
        share_forbidden: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self.registry = registry
        self.log_dir = log_dir
        self._run = run
        self._stream = stream
        self._spawn = spawn
        self._alive = alive
        #: Phase 1 hands the screen to the user through the ``tart run``
        #: window, so the window stays unless the daemon is told otherwise.
        self.graphics = graphics
        self._sleep = sleep
        self.stop_wait = stop_wait
        self.suspend_wait = suspend_wait
        self.boot_wait = boot_wait
        self.softnet = softnet
        self.softnet_block = softnet_block
        self._locked = locked or (lambda: host_locked(self._run))
        self._host_addrs = host_addrs or (lambda: host_ipv4s(self._run))
        #: Where host folders may be shared from (the ``vm_share_roots`` setting,
        #: read at each start) and what is never shared (daemon/vm_share.py).
        self._share_roots = share_roots or (lambda: [])
        self._share_forbidden = share_forbidden or (lambda: vm_share.forbidden_paths())
        self._busy = set()
        self._busy_lock = threading.Lock()
        #: vm id -> when a suspend gave up waiting; settle() decides later.
        self._abandoned = {}

    @contextlib.contextmanager
    def _working(self, vm_id: str):
        with self._busy_lock:
            self._busy.add(vm_id)
        try:
            yield
        finally:
            with self._busy_lock:
                self._busy.discard(vm_id)

    def _is_busy(self, vm_id: str) -> bool:
        with self._busy_lock:
            return vm_id in self._busy

    # ---- tart ----------------------------------------------------------- #
    def tart_list(self) -> List[TartVm]:
        rc, out, err = self._run([TART, "list", "--source", "local", "--format", "json"], 30)
        if rc != 0:
            raise VmError("tart_failed", f"tart list: {err.strip() or rc}")
        return parse_list(out)

    def tart_find(self, name: str) -> Optional[TartVm]:
        for row in self.tart_list():
            if row.name == name:
                return row
        return None

    def tart_clone(self, source: str, name: str, timeout: float = CLONE_WAIT) -> None:
        """``tart clone`` of a local VM. The caller has checked that ``name``
        is free: tart would overwrite nothing, but a half-made copy under a
        name that was not ours to take is not ours to remove."""
        rc, _, err = self._run([TART, "clone", "--", source, name], timeout)
        if rc != 0:
            raise VmError("tart_failed", f"tart clone: {err.strip()[:300] or rc}")

    def tart_delete(self, name: str, timeout: float = 120.0) -> None:
        rc, _, err = self._run([TART, "delete", "--", name], timeout)
        if rc != 0:
            raise VmError("tart_failed", f"tart delete: {err.strip()[:300] or rc}")

    def tart_state(self, name: str) -> str:
        for row in self.tart_list():
            if row.name == name:
                return row.state
        return "missing"

    def run_argv(self, name: str, softnet: Optional[bool] = None,
                 expose: Sequence[str] = (), allow: Sequence[str] = (),
                 shares: Sequence[dict] = ()) -> List[str]:
        argv = [TART, "run", "--suspendable"]
        if self.softnet if softnet is None else softnet:
            argv += ["--net-softnet"]
            if self.softnet_block:
                argv.append(f"--net-softnet-block={self.softnet_block}")
            # Longest prefix wins: a narrow allow opens its own address inside
            # the blocked private ranges and nothing else.
            if allow:
                argv.append("--net-softnet-allow=" + ",".join(allow))
            if expose:
                argv.append("--net-softnet-expose=" + ",".join(expose))
        for share in shares:
            argv += ["--dir", vm_share.tart_arg(share)]
        if not self.graphics:
            argv.append("--no-graphics")
        return argv + ["--", name]

    def _guest(self, vm: vmhub.Vm, command: str, timeout: float = 20.0) -> Result:
        """Run ``command`` in the guest through ``tart exec`` (the Tart Guest
        Agent): no address, key or network needed, PATH is minimal so commands
        are written with absolute paths."""
        return self._run([TART, "exec", "--", vm.tart_name, "/bin/sh", "-c", command],
                         timeout)

    def guest_stream(self, vm: vmhub.Vm, script: str, *, feed=None, drain=None,
                     timeout: float = 600.0) -> "StreamResult":
        """``/bin/sh -c script`` in the guest with bytes streamed through
        ``tart exec`` (``-i`` when ``feed`` supplies stdin). A guest agent that
        does not answer is ``vm_unavailable``."""
        argv = [TART, "exec"] + (["-i"] if feed else []) + ["--", vm.tart_name, "/bin/sh", "-c", script]
        out = self._stream(argv, feed=feed, drain=drain, timeout=timeout)
        if is_agent_error(out.rc, "", out.stderr):
            raise VmError("vm_unavailable", f"{vm.id}: the guest agent said {out.stderr.strip()[:200]}",
                          retry_after=5)
        return out

    def guest_run(self, vm: vmhub.Vm, script: str, timeout: float = 60.0) -> Result:
        """``/bin/sh -c script`` in the guest, output returned (cut at
        ``READ_CAP``). A guest agent that does not answer is ``vm_unavailable``."""
        rc, out, err = self._guest(vm, script, timeout)
        if is_agent_error(rc, out, err):
            raise VmError("vm_unavailable", f"{vm.id}: the guest agent said {err.strip()[:200]}",
                          retry_after=5)
        return rc, out, err

    def exec(self, vm_id: str, command: str, timeout: float = 60.0) -> dict:
        """Run ``command`` in the guest's shell. The result is cut at
        :data:`OUTPUT_CAP` per stream; the command text is not recorded. A
        guest without its agent (or a VM that is not up) is ``vm_unavailable``,
        not a command that failed."""
        vm = self.registry.get(vm_id)
        if vm.state != "running":
            raise VmError("vm_not_ready", f"{vm_id} is {vm.state}")
        rc, out, err = self._guest(vm, command, min(max(timeout, 1.0), EXEC_MAX))
        if is_agent_error(rc, out, err):
            raise VmError("vm_unavailable", f"{vm_id}: the guest agent said {err.strip()[:200]}",
                          retry_after=5)
        cut = len(out) > OUTPUT_CAP or len(err) > OUTPUT_CAP
        return {"rc": rc, "stdout": out[:OUTPUT_CAP], "stderr": err[:OUTPUT_CAP],
                "truncated": cut, "timed_out": rc == 124}

    # ---- start / resume -------------------------------------------------- #
    @_operation
    def start(self, vm_id: str) -> vmhub.Vm:
        """Claim a slot, run the VM (``--suspendable``), wait for its address
        and ssh. A suspended VM is resumed the same way: ``tart run`` on a
        suspended VM restores it."""
        prior = self.registry.get(vm_id).state
        if self.registry.get(vm_id).locked:
            raise vmhub.locked_error(self.registry.get(vm_id))
        if prior == "suspended" and self.host_locked():
            # Nothing has been claimed or run: the saved state stays and a
            # retry after the unlock is safe.
            raise VmError("vm_host_locked",
                          f"{vm_id}: the host screen is locked, and a suspended VM cannot be "
                          "resumed until it is unlocked")
        # Refused before a slot is claimed: the VM stays as it was.
        allow = self._checked_allow(self.registry.get(vm_id))
        shares = self._checked_shares(self.registry.get(vm_id))
        vm = self.registry.claim_slot(vm_id)
        pid = 0
        try:
            tart = self.tart_state(vm.tart_name)
            if tart == "missing":
                raise VmError("tart_missing", f"tart has no VM named {vm.tart_name!r}")
            if tart == "running":
                raise VmError("tart_already_running",
                              f"{vm.tart_name} is already running; reconcile instead")
            log = self.log_dir / vm_id / "tart.log"
            vmhub.rotate_log(log)
            argv = self.run_argv(vm.tart_name, vm.softnet, vm.expose, allow, shares)
            vm.net_applied = {"expose": list(vm.expose), "allow": list(vm.allow)}
            vm.shares_applied = [dict(s) for s in vm.shared_dirs]
            pid = self._spawn(argv, log)
            deadline = time.monotonic() + self.boot_wait
            self._wait_agent(vm, deadline, pid, log)
            self.registry.set_state(vm_id, "running")
            self.registry.set_ready(vm_id, ssh=True)  # "ssh" is the flag's old name: the guest agent answers
            self._probe(vm_id, deadline)
            self._note_ip(vm_id)
        except VmError as exc:
            self._fail(vm_id, exc, prior)
            raise
        return self.registry.get(vm_id)

    def _checked_allow(self, vm: vmhub.Vm) -> List[str]:
        """The allow list as it will be passed, checked again against the host's
        addresses as they are now (an address can change since it was saved)."""
        if not vm.allow:
            return []
        addrs = self._host_addrs()
        return [vmhub.check_allow(c, addrs) for c in vm.allow]

    def share_rules(self) -> tuple:
        """``(roots, forbidden)`` as they are now."""
        forbidden = self._share_forbidden()
        return vm_share.clean_roots(self._share_roots(), forbidden), forbidden

    def _checked_shares(self, vm: vmhub.Vm) -> List[dict]:
        """The shares as they will be passed, each checked again against the
        roots and the never-shared list as they are now. One that fails is left
        out (and said so in ``shares_skipped`` and the audit log): the VM still
        starts, and nothing the rules refuse is mounted."""
        vm.shares_skipped = {}
        if not vm.shared_dirs:
            return []
        roots, forbidden = self.share_rules()
        ok: List[dict] = []
        for share in vm.shared_dirs:
            try:
                ok.append(vm_share.recheck(share, roots, forbidden))
            except VmError as exc:
                vm.shares_skipped[share["name"]] = f"{exc.code}: {exc}"[:300]
                self.registry.note(vm.id, "share_skipped", share=share["name"], code=exc.code)
        return ok

    def host_locked(self) -> bool:
        return bool(self._locked())

    def _wait_agent(self, vm: vmhub.Vm, deadline: float, pid: int = 0,
                    log: Optional[Path] = None) -> None:
        """Until the guest agent answers ``tart exec``."""
        while True:
            rc, _, _ = self._guest(vm, "/usr/bin/true", 10)
            if rc == 0:
                return
            if pid and not self._alive(pid) and self.tart_state(vm.tart_name) != "running":
                # tart run ended without a VM: Apple's slot limit on a resume
                # (VZErrorDomain Code=6) shows up this way, within a second.
                raise VmError("tart_exited", f"tart run exited: {_tail(log)}")
            if time.monotonic() >= deadline:
                raise VmError("vm_boot_timeout",
                              f"{vm.id}: the guest agent did not answer within {self.boot_wait:g}s")
            self._sleep(POLL)

    def _note_ip(self, vm_id: str) -> None:
        """The guest's address is for display; nothing depends on it."""
        vm = self.registry.get(vm_id)
        rc, out, _ = self._run([TART, "ip", "--", vm.tart_name], 10)
        if rc == 0 and out.strip():
            try:
                self.registry.set_ready(vm_id, ip=out.strip().splitlines()[0])
            except VmError:
                pass

    def _reprobe(self, vm_id: str, deadline: float) -> None:
        vm = self.registry.get(vm_id)
        self._wait_agent(vm, deadline)
        self.registry.set_ready(vm_id, ssh=True)
        self._probe(vm_id, deadline)
        self._note_ip(vm_id)

    def _probe(self, vm_id: str, deadline: float) -> None:
        """A console login: what a boot must reach before the guest channel is
        worth opening (the agent has answered already). The cua daemon is the
        channel's business."""
        vm = self.registry.get(vm_id)
        while True:
            rc, out, _ = self._guest(vm, "/usr/bin/stat -f %Su /dev/console", 10)
            if rc == 0 and out.strip() == vm.ssh_user:
                self.registry.set_ready(vm_id, gui=True)
                return
            if time.monotonic() >= deadline:
                raise VmError("vm_boot_timeout",
                              f"{vm_id}: no console login within {self.boot_wait:g}s")
            self._sleep(POLL)

    def _fail(self, vm_id: str, exc: VmError, prior: str = "stopped") -> None:
        vm = self.registry.get(vm_id)
        if vm.state not in vmhub.OCCUPIED:
            return
        if prior == "suspended" and self.tart_state(vm.tart_name) == "suspended":
            # A resume that did not happen leaves the saved state where it was.
            self.registry.adopt(vm_id, "suspended")
            self.registry.note(vm_id, "resume_failed", code=exc.code, why=str(exc))
            return
        self.registry.set_state(vm_id, "fault", reason=f"{exc.code}: {exc}")

    # ---- stop / suspend --------------------------------------------------- #
    @_operation
    def stop(self, vm_id: str) -> vmhub.Vm:
        """Shut the guest down from inside and wait for tart to say stopped.
        If it does not, the VM stays ``stopping`` and ``VmError`` says so;
        nothing is forced."""
        vm = self.registry.get(vm_id)
        if vm.state == "stopped":
            return vm
        if vm.state not in ("running", "starting", "paused", "stopping"):
            raise VmError("vm_bad_state", f"{vm_id} is {vm.state}; cannot stop it")
        # The shutdown is sent each time stop is asked, a retry of a stop that
        # did not take included: sudo is checked first (a guest that needs a
        # password must say so, not look like a shutdown), the guest syncs, and
        # the shutdown is started in the background so that this call returns
        # (in the foreground tart exec holds until the VM is gone and ends with
        # "Transport became inactive": measured, 34 s). On a VM already
        # "stopping" an answer from the guest agent that is an error just means
        # the guest is on its way down.
        rc, out, err = self._guest(vm, SHUTDOWN, 30)
        if rc != 0 and vm.state != "stopping":
            raise VmError("vm_stop_failed",
                          f"{vm_id}: guest shutdown said rc {rc}: {(err or out).strip()[:200]}")
        if vm.state != "stopping":
            self.registry.set_state(vm_id, "stopping")
        deadline = time.monotonic() + self.stop_wait
        while time.monotonic() < deadline:
            if self.tart_state(vm.tart_name) in ("stopped", "missing"):
                if self.registry.get(vm_id).state != "stopped":
                    self.registry.set_state(vm_id, "stopped")
                return self.registry.get(vm_id)
            self._sleep(POLL)
        # The shutdown did not take. A guest that still answers is up and
        # usable: give it back as running (the route reopens its channel)
        # instead of leaving it "stopping" with nothing watching it.
        back = self._guest(vm, "/usr/bin/true", 10)[0] == 0 and \
            self.tart_state(vm.tart_name) == "running"
        if back:
            self.registry.set_state(vm_id, "running")
            # Leaving the running state cleared the readiness; the agent has
            # just answered and the console login was there before.
            self.registry.set_ready(vm_id, ssh=True, gui=True)
            self.registry.note(vm_id, "stop_abandoned")
        raise VmError("vm_stop_timeout",
                      f"{vm_id} did not stop within {self.stop_wait:g}s"
                      + ("; it is running again" if back else "")
                      + "; force_stop would lose unsynced guest writes")
    @_operation
    def force_stop(self, vm_id: str) -> vmhub.Vm:
        """``tart stop``: the VM is gone at once and unsynced writes with it."""
        vm = self.registry.get(vm_id)
        if vm.state == "stopped":
            return vm
        if vm.state == "suspended":
            raise VmError("vm_bad_state", f"{vm_id} is suspended; there is nothing running to stop")
        rc, _, err = self._run([TART, "stop", "--", vm.tart_name], 30)
        if rc != 0:
            raise VmError("tart_failed", f"tart stop: {err.strip() or rc}")
        self.registry.note(vm_id, "forced_stop", data_loss_possible=True)
        # Whatever the record said (starting, saving, restoring, fault ...),
        # tart has just stopped the VM.
        self.registry.adopt(vm_id, "stopped")
        return self.registry.get(vm_id)

    def settle(self) -> List[str]:
        """Move a VM left in a transitional state to the end tart reports when
        that end is certain: saving -> suspended, stopping -> stopped. A suspend
        that gave up waiting and that tart still lists as running, a second
        wait period later, is taken as failed and the VM goes back to running
        (the caller reopens its channel). A VM a lifecycle call is working on
        is left alone. Returns the ids that moved."""
        done = []
        for vm in self.registry.list():
            if vm.state not in ("saving", "stopping") or self._is_busy(vm.id):
                continue
            actual = self.tart_state(vm.tart_name)
            if self._is_busy(vm.id):
                continue
            if vm.state == "saving" and actual == "suspended":
                self.registry.set_state(vm.id, "suspended")
            elif vm.state == "stopping" and actual in ("stopped", "missing"):
                self.registry.set_state(vm.id, "stopped")
            elif (vm.state == "saving" and actual == "running" and vm.id in self._abandoned
                  and time.monotonic() - self._abandoned[vm.id] >= self.suspend_wait):
                self._abandoned.pop(vm.id, None)
                self.registry.set_state(vm.id, "running")
                self.registry.note(vm.id, "suspend_abandoned")
            else:
                continue
            done.append(vm.id)
        return done

    @_operation
    def suspend(self, vm_id: str) -> vmhub.Vm:
        vm = self.registry.get(vm_id)
        if vm.state != "running":
            raise VmError("vm_bad_state", f"{vm_id} is {vm.state}; only a running VM suspends")
        self.registry.set_state(vm_id, "saving")
        rc, _, err = self._run([TART, "suspend", "--", vm.tart_name], 120)
        if rc != 0:
            self.registry.set_state(vm_id, "running")
            raise VmError("tart_failed", f"tart suspend: {err.strip() or rc}")
        # ``tart suspend`` returns before the VM is saved (⑥: rc 0 in 0.03 s,
        # state still "running"; "suspended" about 4 s later). Until tart says
        # so the VM still holds its slot.
        deadline = time.monotonic() + self.suspend_wait
        while self.tart_state(vm.tart_name) != "suspended":
            if time.monotonic() >= deadline:
                self._abandoned[vm_id] = time.monotonic()
                raise VmError("vm_suspend_timeout",
                              f"{vm_id} was not reported suspended within {self.suspend_wait:g}s")
            self._sleep(0.5)
        self._abandoned.pop(vm_id, None)
        if self.registry.get(vm_id).state != "suspended":
            self.registry.set_state(vm_id, "suspended")
        return self.registry.get(vm_id)

    # ---- after a daemon restart ------------------------------------------- #
    def reconcile(self, ids=None) -> List[str]:
        """Make the records agree with what tart reports: a VM that is up
        is adopted as ``running`` (readiness is probed again), one the
        record thought up but tart does not list as running is stopped or
        suspended. Returns what changed, for the log.

        Order does not matter to the outcome: the VMs that stop holding a slot
        are adopted first. A VM that cannot be adopted (more are up than the
        slot limit allows) is put in ``fault`` with the reason, not retried."""
        changed = []
        rows = {row.name: row for row in self.tart_list()}
        plan = []
        for vm in self.registry.list():
            if (ids is not None and vm.id not in ids) or self._is_busy(vm.id):
                continue
            row = rows.get(vm.tart_name)
            actual = row.state if row else "missing"
            want = {"running": "running", "suspended": "suspended"}.get(actual, "stopped")
            if want != vm.state and not (vm.state == "fault" and want == "stopped"):
                plan.append((vm, want))
        plan.sort(key=lambda p: p[1] == "running")
        for vm, want in plan:
            old = vm.state
            try:
                self.registry.adopt(vm.id, want)
            except VmError as exc:
                self.registry.set_state(vm.id, "fault", reason=f"{exc.code}: {exc}")
                changed.append(f"{vm.id}: {old} -> fault ({exc.code})")
                continue
            changed.append(f"{vm.id}: {old} -> {want}")
            if want == "running" and vm.locked:
                # Only recorded and shown: the daemon never stops a VM for this.
                self.registry.note(vm.id, "locked_but_running")
                changed.append(f"{vm.id}: locked but running (not stopped)")
        return changed

    async def reprobe(self, vm_id: str) -> None:
        """Probe an adopted VM's ssh and console again (off the event loop)."""
        vm = self.registry.get(vm_id)
        if not vm.ip:
            rc, out, _ = await asyncio.to_thread(self._run, [TART, "ip", "--", vm.tart_name], 10)
            if rc == 0 and out.strip():
                self.registry.set_ready(vm_id, ip=out.strip().splitlines()[0])
        await asyncio.to_thread(self._reprobe, vm_id, time.monotonic() + self.boot_wait)
