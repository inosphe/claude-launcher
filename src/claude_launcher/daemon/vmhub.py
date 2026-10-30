"""VM registry (vm-setup-8s6, design: vm-setup-q0g.3): who may look at and
drive a VM, and which VMs hold one of the machine's macOS VM slots.

This module is the pure half of the VmHub. It owns the records and the rules
and does no I/O beyond its own JSON file: starting ``tart``, the guest channel
and the HTTP routes are separate layers that call into it.

* **Slots.** Apple allows two macOS VMs to run at once; a third ``tart run``
  exits at once with an error. A VM holds a slot from ``starting`` through
  ``stopping`` (``suspended`` and ``stopped`` hold none), and
  :meth:`Registry.claim_slot` refuses a start that would exceed the limit
  with ``vm_slots_full`` and the names of the holders.
* **Grants.** ``grants[session]`` is the set of scopes the user gave that
  session: ``view`` (screenshots, status), ``control`` (UI input) and ``exec``
  (a guest shell). ``control`` and ``exec`` imply ``view``: driving a screen
  needs seeing it. Nothing is granted by default, and only the user grants.
* **Control.** At most one ``controller``. :meth:`Registry.acquire` hands out
  the current ``epoch``; every control or exec call presents it and a call
  made under an older epoch is refused (``vm_stale``). The user may add
  ``extra_controllers``; they share the controller's epoch and calls of all
  of them run through the one command queue of the layer above.
* **Human.** While the user is driving, control and exec calls are refused
  with ``vm_busy``; calls already running finish (``draining`` until the
  last one ends). When the user hands the screen back the controller keeps
  its control and its epoch, but ``needs_observe`` stays set until a ``view``
  call is made, so the first thing it does is look at the screen again.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import atomic

SLOT_LIMIT = 2

VIEW, CONTROL, EXEC = "view", "control", "exec"
SCOPES = (VIEW, CONTROL, EXEC)

#: States in which a VM holds one of the machine's macOS VM slots.
OCCUPIED = frozenset({
    "starting", "running", "pausing", "paused", "saving", "restoring", "stopping",
})
STATES = OCCUPIED | {"stopped", "suspended", "fault"}

#: Allowed moves. ``fault`` is reachable from everywhere and leaves only
#: towards ``stopped`` (the record is reset once the user has looked at it).
_MOVES = {
    "stopped": {"starting"},
    "starting": {"running", "stopping", "stopped"},
    "running": {"pausing", "saving", "stopping", "stopped"},
    "pausing": {"paused", "running"},
    "paused": {"running", "saving", "stopping"},
    "saving": {"suspended", "running"},
    "suspended": {"restoring", "stopped"},
    "restoring": {"running", "stopped"},
    "stopping": {"stopped"},
    "fault": {"stopped"},
}

AUDIT_RING = 500


class VmError(Exception):
    """A refusal with a stable ``code`` the tools hand back to the session."""

    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.code = code
        self.extra = extra

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self), **self.extra}


@dataclass
class Vm:
    id: str
    tart_name: str
    state: str = "stopped"
    ip: str = ""
    #: Boot readiness, set by the layer that watches the guest. ``running``
    #: only means tart reports the VM up; a session may acquire it once all
    #: three are true.
    ssh_ready: bool = False
    gui_ready: bool = False
    cua_ready: bool = False
    fault_reason: str = ""
    controller: str = ""
    extra_controllers: Dict[str, str] = field(default_factory=dict)  # session -> reason
    epoch: int = 0
    grants: Dict[str, List[str]] = field(default_factory=dict)
    human: str = "idle"  # idle | driving
    human_since: float = 0.0
    needs_observe: bool = False
    #: Calls running right now (not persisted).
    inflight: int = 0

    @property
    def ready(self) -> bool:
        return self.state == "running" and self.ssh_ready and self.gui_ready and self.cua_ready

    @property
    def draining(self) -> bool:
        return self.human == "driving" and self.inflight > 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "tart_name": self.tart_name, "state": self.state,
            "ip": self.ip, "ssh_ready": self.ssh_ready, "gui_ready": self.gui_ready,
            "cua_ready": self.cua_ready, "fault_reason": self.fault_reason,
            "controller": self.controller,
            "extra_controllers": dict(self.extra_controllers),
            "epoch": self.epoch, "grants": {k: list(v) for k, v in self.grants.items()},
            "human": self.human, "human_since": self.human_since,
            "needs_observe": self.needs_observe,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Vm":
        vm = cls(id=str(raw["id"]), tart_name=str(raw.get("tart_name") or raw["id"]))
        vm.state = raw.get("state") if raw.get("state") in STATES else "stopped"
        vm.ip = str(raw.get("ip") or "")
        vm.fault_reason = str(raw.get("fault_reason") or "")
        vm.epoch = int(raw.get("epoch") or 0)
        vm.grants = {
            str(s): [x for x in SCOPES if x in (v or [])]
            for s, v in (raw.get("grants") or {}).items()
        }
        return vm


def _normal(scopes) -> List[str]:
    wanted = set(scopes or ())
    unknown = wanted - set(SCOPES)
    if unknown:
        raise VmError("vm_bad_scope", f"unknown scope(s): {sorted(unknown)}")
    if wanted & {CONTROL, EXEC}:
        wanted.add(VIEW)
    return [s for s in SCOPES if s in wanted]


class Registry:
    """The VM records, behind one lock, saved after every change."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        slot_limit: int = SLOT_LIMIT,
        audit: Optional[Callable[[dict], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.slot_limit = slot_limit
        self._audit_sink = audit
        self._clock = clock
        self._lock = threading.RLock()
        self._vms: Dict[str, Vm] = {}
        self.audit_log: List[dict] = []

    # ---- persistence --------------------------------------------------- #
    def load(self) -> None:
        """Read the file. What a restarted daemon cannot trust is dropped:
        controllers, the human flag and readiness (the sessions that held
        them went down with the daemon; the guest is re-probed). Grants
        and epochs survive."""
        if self.path is None or not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            for raw in doc.get("vms") or []:
                try:
                    vm = Vm.from_dict(raw)
                except (KeyError, TypeError, ValueError):
                    continue
                self._vms[vm.id] = vm

    def _save(self) -> None:
        if self.path is None:
            return
        doc = {"vms": [vm.to_dict() for vm in self._vms.values()]}
        with atomic.scratch(self.path) as tmp:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            atomic.replace(tmp, self.path)

    def _log(self, vm: str, session: str, what: str, **extra) -> None:
        # The record never carries screen contents or command text: who did
        # what, when, under which epoch, and how it ended.
        row = {"at": self._clock(), "vm": vm, "session": session, "what": what, **extra}
        self.audit_log.append(row)
        del self.audit_log[:-AUDIT_RING]
        if self._audit_sink is not None:
            self._audit_sink(row)

    # ---- records ------------------------------------------------------- #
    def add(self, vm_id: str, tart_name: str = "") -> Vm:
        with self._lock:
            if vm_id in self._vms:
                raise VmError("vm_exists", f"{vm_id} is already registered")
            vm = Vm(id=vm_id, tart_name=tart_name or vm_id)
            self._vms[vm_id] = vm
            self._save()
            return vm

    def get(self, vm_id: str) -> Vm:
        try:
            return self._vms[vm_id]
        except KeyError:
            raise VmError("vm_unknown", f"no VM {vm_id!r}") from None

    def list(self) -> List[Vm]:
        with self._lock:
            return list(self._vms.values())

    def remove(self, vm_id: str) -> None:
        with self._lock:
            vm = self.get(vm_id)
            if vm.state not in ("stopped", "suspended", "fault"):
                raise VmError("vm_busy_state", f"{vm_id} is {vm.state}; stop it first")
            del self._vms[vm_id]
            self._save()

    # ---- slots and state ------------------------------------------------ #
    def holders(self, except_id: str = "") -> List[str]:
        return [v.id for v in self._vms.values()
                if v.state in OCCUPIED and v.id != except_id]

    def claim_slot(self, vm_id: str) -> Vm:
        """``stopped``/``suspended`` -> ``starting``/``restoring``, or refuse."""
        with self._lock:
            vm = self.get(vm_id)
            target = {"stopped": "starting", "suspended": "restoring"}.get(vm.state)
            if target is None:
                raise VmError("vm_bad_state", f"{vm_id} is {vm.state}; cannot start it")
            held = self.holders(except_id=vm_id)
            if len(held) >= self.slot_limit:
                raise VmError(
                    "vm_slots_full",
                    f"{len(held)} of {self.slot_limit} macOS VM slots are in use",
                    holders=held,
                )
            self._move(vm, target)
            return vm

    def set_state(self, vm_id: str, state: str, *, reason: str = "") -> Vm:
        with self._lock:
            vm = self.get(vm_id)
            self._move(vm, state, reason=reason)
            return vm

    def _move(self, vm: Vm, state: str, *, reason: str = "") -> None:
        if state not in STATES:
            raise VmError("vm_bad_state", f"unknown state {state!r}")
        if state != "fault" and state not in _MOVES.get(vm.state, ()):
            raise VmError("vm_bad_state", f"{vm.id}: {vm.state} -> {state} is not a move")
        old = vm.state
        vm.state = state
        vm.fault_reason = reason if state == "fault" else ""
        if state in ("stopped", "suspended", "fault", "starting", "restoring", "stopping"):
            vm.ssh_ready = vm.gui_ready = vm.cua_ready = False
        if state in ("stopped", "suspended", "fault", "stopping"):
            self._drop_control(vm, why=f"vm {state}")
        self._log(vm.id, "", "state", frm=old, to=state, reason=reason)
        self._save()

    def set_ready(self, vm_id: str, *, ssh=None, gui=None, cua=None, ip: str = "") -> Vm:
        with self._lock:
            vm = self.get(vm_id)
            if ssh is not None:
                vm.ssh_ready = bool(ssh)
            if gui is not None:
                vm.gui_ready = bool(gui)
            if cua is not None:
                vm.cua_ready = bool(cua)
            if ip:
                vm.ip = ip
            self._save()
            return vm

    # ---- grants (the user's) --------------------------------------------- #
    def grant(self, vm_id: str, session: str, scopes) -> List[str]:
        with self._lock:
            vm = self.get(vm_id)
            got = sorted(set(vm.grants.get(session, [])) | set(_normal(scopes)),
                         key=SCOPES.index)
            vm.grants[session] = got
            self._log(vm_id, session, "grant", scopes=got)
            self._save()
            return got

    def revoke(self, vm_id: str, session: str, scopes=None) -> List[str]:
        """Drop ``scopes`` (all when omitted). Losing ``control`` ends the
        control the session holds; losing ``view`` loses everything."""
        with self._lock:
            vm = self.get(vm_id)
            have = set(vm.grants.get(session, []))
            if scopes is None or VIEW in set(scopes):
                have = set()
            else:
                have -= set(scopes)
            if have:
                vm.grants[session] = [s for s in SCOPES if s in have]
            else:
                vm.grants.pop(session, None)
            if CONTROL not in have:
                self._drop_session_control(vm, session, why="control revoked")
            self._log(vm_id, session, "revoke", left=sorted(have))
            self._save()
            return sorted(have)

    def add_extra_controller(self, vm_id: str, session: str, reason: str) -> None:
        with self._lock:
            vm = self.get(vm_id)
            if CONTROL not in vm.grants.get(session, []):
                raise VmError("vm_no_grant", f"{session} has no control grant")
            if not reason.strip():
                raise VmError("vm_reason_required", "a duplicate grant needs a stated reason")
            if not vm.controller:
                raise VmError("vm_no_controller", "there is no controller to share with")
            if session != vm.controller:
                vm.extra_controllers[session] = reason.strip()
                self._log(vm_id, session, "extra_controller", reason=reason.strip())
                self._save()

    # ---- control (the session's) ----------------------------------------- #
    def acquire(self, vm_id: str, session: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            if CONTROL not in vm.grants.get(session, []):
                raise VmError("vm_no_grant", f"{session} has no control grant on {vm_id}")
            if not vm.ready:
                raise VmError(
                    "vm_not_ready",
                    f"{vm_id} is {vm.state} (ssh={vm.ssh_ready} gui={vm.gui_ready} "
                    f"cua={vm.cua_ready})",
                )
            if vm.human == "driving":
                raise VmError("vm_busy", "the user is driving the VM", retry_after=5)
            if vm.controller and vm.controller != session:
                return {"ok": False, "error": "vm_held", "held_by": vm.controller,
                        "epoch": vm.epoch}
            if vm.controller != session:
                vm.controller = session
                vm.epoch += 1
                vm.needs_observe = True
                self._log(vm_id, session, "acquire", epoch=vm.epoch)
                self._save()
            return {"ok": True, "epoch": vm.epoch, "needs_observe": vm.needs_observe}

    def release(self, vm_id: str, session: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            if session == vm.controller:
                self._drop_control(vm, why="released")
            elif session in vm.extra_controllers:
                del vm.extra_controllers[session]
                self._log(vm_id, session, "release_extra")
                self._save()
            return {"ok": True}

    def revoke_controller(self, vm_id: str) -> None:
        """The user takes control back."""
        with self._lock:
            self._drop_control(self.get(vm_id), why="user revoked")

    def session_exited(self, session: str) -> None:
        """A session ended: hand back whatever it held. Grants stay."""
        with self._lock:
            for vm in self._vms.values():
                self._drop_session_control(vm, session, why="session exited")

    def _drop_session_control(self, vm: Vm, session: str, *, why: str) -> None:
        if session == vm.controller:
            self._drop_control(vm, why=why)
        elif session in vm.extra_controllers:
            del vm.extra_controllers[session]
            self._log(vm.id, session, "drop_extra", why=why)
            self._save()

    def _drop_control(self, vm: Vm, *, why: str) -> None:
        if not vm.controller and not vm.extra_controllers:
            return
        self._log(vm.id, vm.controller, "drop_control", why=why, epoch=vm.epoch)
        vm.controller = ""
        vm.extra_controllers = {}
        vm.epoch += 1
        self._save()

    # ---- the gate every call passes ------------------------------------- #
    def begin_call(self, vm_id: str, session: str, scope: str, epoch: Optional[int] = None) -> Vm:
        """Check ``session`` may make a ``scope`` call now and count it as
        running. Pair with :meth:`end_call`."""
        with self._lock:
            vm = self.get(vm_id)
            if scope not in SCOPES:
                raise VmError("vm_bad_scope", f"unknown scope {scope!r}")
            if scope not in vm.grants.get(session, []):
                self._log(vm_id, session, "refused", scope=scope, why="no grant")
                raise VmError("vm_no_grant", f"{session} has no {scope} grant on {vm_id}")
            if scope == VIEW:
                vm.needs_observe = False
            else:
                if vm.human == "driving":
                    raise VmError("vm_busy", "the user is driving the VM", retry_after=5,
                                  draining=vm.draining)
                if session != vm.controller and session not in vm.extra_controllers:
                    raise VmError("vm_not_controller",
                                  f"{session} does not hold control of {vm_id}",
                                  held_by=vm.controller)
                if epoch is None or epoch != vm.epoch:
                    self._log(vm_id, session, "refused", scope=scope, why="stale",
                              epoch=epoch, current=vm.epoch)
                    raise VmError("vm_stale", "control was handed over since this epoch",
                                  epoch=vm.epoch)
                if vm.needs_observe:
                    raise VmError("vm_observe_first",
                                  "look at the screen (vm_screenshot) before acting",
                                  needs_observe=True)
            if not vm.ready:
                raise VmError("vm_not_ready", f"{vm_id} is {vm.state}")
            vm.inflight += 1
            self._log(vm_id, session, "call", scope=scope, epoch=vm.epoch)
            return vm

    def end_call(self, vm_id: str, session: str, scope: str, *, ok: bool = True) -> None:
        with self._lock:
            vm = self.get(vm_id)
            vm.inflight = max(0, vm.inflight - 1)
            self._log(vm_id, session, "done", scope=scope, ok=ok)

    # ---- the user at the screen ------------------------------------------ #
    def human_driving(self, vm_id: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            if vm.human != "driving":
                vm.human = "driving"
                vm.human_since = self._clock()
                self._log(vm_id, "", "human_driving")
                self._save()
            return {"draining": vm.draining, "inflight": vm.inflight}

    def human_idle(self, vm_id: str) -> None:
        with self._lock:
            vm = self.get(vm_id)
            if vm.human == "driving":
                vm.human = "idle"
                vm.needs_observe = True
                self._log(vm_id, "", "human_idle")
                self._save()

    def human_touch(self, vm_id: str) -> None:
        """Another input from the user; keeps ``driving`` fresh."""
        with self._lock:
            vm = self.get(vm_id)
            if vm.human == "driving":
                vm.human_since = self._clock()

    def human_timeout(self, vm_id: str, idle_seconds: float) -> bool:
        """Hand the screen back when the user has been still for
        ``idle_seconds``. Returns whether it did."""
        with self._lock:
            vm = self.get(vm_id)
            if vm.human == "driving" and self._clock() - vm.human_since >= idle_seconds:
                self.human_idle(vm_id)
                return True
            return False

    # ---- a restarted daemon -------------------------------------------- #
    def after_restart(self) -> None:
        """Nothing held before the restart is valid now: the sessions went
        down with the daemon and the guest is probed again."""
        with self._lock:
            for vm in self._vms.values():
                vm.controller = ""
                vm.extra_controllers = {}
                vm.human = "idle"
                vm.needs_observe = False
                vm.inflight = 0
                vm.ssh_ready = vm.gui_ready = vm.cua_ready = False
                vm.epoch += 1
            self._save()

    def view(self, vm_id: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            return {**vm.to_dict(), "ready": vm.ready, "draining": vm.draining,
                    "inflight": vm.inflight}
