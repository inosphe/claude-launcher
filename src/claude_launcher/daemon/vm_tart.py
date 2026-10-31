"""Running the VMs (vm-setup-8s6, step 2): ``tart`` and the guest's ssh, kept
behind two small runners so the rules in :mod:`vmhub` can be driven without a
VM.

What the d-002 measurements decided (vm-setup-q0g.2, runs/d-002):

* ``tart run`` always carries ``--suspendable``; a VM started without it
  cannot be suspended (suspend ~4 s, resume to ssh ~7 s).
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
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import vmhub
from .vmhub import Registry, VmError

TART = "tart"
STOP_WAIT = 120.0
BOOT_WAIT = 90.0
SUSPEND_WAIT = 60.0
POLL = 2.0
OUTPUT_CAP = 64 * 1024
EXEC_MAX = 600.0

#: (returncode, stdout, stderr)
Result = Tuple[int, str, str]
Runner = Callable[[Sequence[str], float], Result]
Spawner = Callable[[Sequence[str], Path], int]


def run_command(argv: Sequence[str], timeout: float = 30.0) -> Result:
    try:
        # No stdin: a command that reads it would wait for ever (d-003).
        done = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


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
        ))
    return out


#: What ssh prints (rc 255) when the guest drops the connection as it goes
#: down. Any other 255 is ssh failing to reach or enter the guest, and the
#: shutdown was not started.
CLOSED_BY_GUEST = re.compile(
    r"closed by remote host|Broken pipe|Connection to \S+ closed|"
    r"Shared connection to \S+ closed", re.I)


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
        spawn: Spawner = spawn_detached,
        alive: Callable[[int], bool] = pid_alive,
        graphics: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        stop_wait: float = STOP_WAIT,
        suspend_wait: float = SUSPEND_WAIT,
        boot_wait: float = BOOT_WAIT,
    ) -> None:
        self.registry = registry
        self.log_dir = log_dir
        self._run = run
        self._spawn = spawn
        self._alive = alive
        #: Phase 1 hands the screen to the user through the ``tart run``
        #: window, so the window stays unless the daemon is told otherwise.
        self.graphics = graphics
        self._sleep = sleep
        self.stop_wait = stop_wait
        self.suspend_wait = suspend_wait
        self.boot_wait = boot_wait
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

    def tart_state(self, name: str) -> str:
        for row in self.tart_list():
            if row.name == name:
                return row.state
        return "missing"

    def run_argv(self, name: str) -> List[str]:
        argv = [TART, "run", "--suspendable"]
        if not self.graphics:
            argv.append("--no-graphics")
        return argv + ["--", name]

    def _ssh(self, vm: vmhub.Vm, command: str, timeout: float = 20.0) -> Result:
        argv = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=accept-new"]
        if vm.ssh_key:
            argv += ["-i", vm.ssh_key]
        argv += ["--", f"{vm.ssh_user}@{vm.ip}", command]
        return self._run(argv, timeout)

    def exec(self, vm_id: str, command: str, timeout: float = 60.0) -> dict:
        """Run ``command`` in the guest's shell through ``tart exec`` (the
        guest agent: ~50 ms against ssh's ~105 ms in d-003, no address or key
        needed, and it works on a network-less VM). The result is cut at
        :data:`OUTPUT_CAP` per stream; the command text is not recorded."""
        vm = self.registry.get(vm_id)
        if vm.state != "running":
            raise VmError("vm_not_ready", f"{vm_id} is {vm.state}")
        rc, out, err = self._run(
            [TART, "exec", "--", vm.tart_name, "/bin/sh", "-c", command],
            min(max(timeout, 1.0), EXEC_MAX))
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
            pid = self._spawn(self.run_argv(vm.tart_name), log)
            deadline = time.monotonic() + self.boot_wait
            ip = self._wait_ip(vm, deadline, pid, log)
            self.registry.set_state(vm_id, "running")
            self.registry.set_ready(vm_id, ip=ip)
            self._probe(vm_id, deadline)
        except VmError as exc:
            self._fail(vm_id, exc, prior)
            raise
        return self.registry.get(vm_id)

    def _wait_ip(self, vm: vmhub.Vm, deadline: float, pid: int = 0,
                 log: Optional[Path] = None) -> str:
        while True:
            rc, out, _ = self._run([TART, "ip", "--", vm.tart_name], 10)
            if rc == 0 and out.strip():
                return vmhub.check_ip(out.strip().splitlines()[0])
            if pid and not self._alive(pid) and self.tart_state(vm.tart_name) != "running":
                # tart run ended without a VM: Apple's slot limit on a resume
                # (VZErrorDomain Code=6) shows up this way, within a second.
                raise VmError("tart_exited", f"tart run exited: {_tail(log)}")
            if time.monotonic() >= deadline:
                raise VmError("vm_boot_timeout", f"{vm.id}: no address within {self.boot_wait:g}s")
            self._sleep(POLL)

    def _probe(self, vm_id: str, deadline: float) -> None:
        """ssh up, then a console login: the two things a boot must reach
        before the guest channel is worth opening. The cua daemon is the
        channel's business."""
        vm = self.registry.get(vm_id)
        while True:
            rc, _, _ = self._ssh(vm, "true", 10)
            if rc == 0:
                self.registry.set_ready(vm_id, ssh=True)
                break
            if time.monotonic() >= deadline:
                raise VmError("vm_boot_timeout", f"{vm_id}: ssh not up within {self.boot_wait:g}s")
            self._sleep(POLL)
        while True:
            rc, out, _ = self._ssh(vm, "stat -f %Su /dev/console", 10)
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
        # A VM already "stopping" is being waited for again: its guest is on
        # the way down and ssh would only fail.
        rc, err = (0, "") if vm.state == "stopping" else self._ssh(
            vm, "sync; sudo -n shutdown -h now", 30)[::2]
        # ssh drops as the guest goes down ("Connection ... closed by remote
        # host", rc 255). The same rc 255 is also ssh failing to get in, and
        # then nothing was started: the VM stays as it was, not "stopping".
        if not (rc == 0 or (rc == 255 and CLOSED_BY_GUEST.search(err or "")) or
                (rc == 255 and not (err or "").strip())):
            raise VmError("vm_stop_failed", f"{vm_id}: guest shutdown said {err.strip() or rc}")
        if vm.state != "stopping":
            self.registry.set_state(vm_id, "stopping")
        deadline = time.monotonic() + self.stop_wait
        while time.monotonic() < deadline:
            if self.tart_state(vm.tart_name) in ("stopped", "missing"):
                if self.registry.get(vm_id).state != "stopped":
                    self.registry.set_state(vm_id, "stopped")
                return self.registry.get(vm_id)
            self._sleep(POLL)
        raise VmError("vm_stop_timeout",
                      f"{vm_id} did not stop within {self.stop_wait:g}s; "
                      "force_stop would lose unsynced guest writes")

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
        return changed

    async def reprobe(self, vm_id: str) -> None:
        """Probe an adopted VM's ssh and console again (off the event loop)."""
        vm = self.registry.get(vm_id)
        if not vm.ip:
            rc, out, _ = await asyncio.to_thread(self._run, [TART, "ip", "--", vm.tart_name], 10)
            if rc == 0 and out.strip():
                self.registry.set_ready(vm_id, ip=out.strip().splitlines()[0])
        await asyncio.to_thread(self._probe, vm_id, time.monotonic() + self.boot_wait)
