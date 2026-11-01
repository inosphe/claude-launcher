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

import ipaddress
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import atomic

log = logging.getLogger(__name__)

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
    "stopping": {"stopped", "running"},
    "fault": {"stopped"},
}

AUDIT_RING = 500
REJECTED_KEEP = 50

#: What may reach a command line or a file name. Everything below is checked
#: when a record is made and again when one is read back from vms.json, and
#: none may start with '-' (an ssh or tart option) or hold a separator.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]{0,62}|[A-Za-z0-9][A-Za-z0-9_.-]{0,62}[A-Za-z0-9_.]")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
_PATH_RE = re.compile(r"/[A-Za-z0-9_.+@%=,:/-]*")


def check_name(what: str, value: str) -> str:
    if not _NAME_RE.fullmatch(value) or ".." in value:
        raise VmError("vm_bad_request",
                      f"{what} {value!r}: letters, digits, '_', '.', '-' only (up to 64), "
                      "not starting with '-'")
    return value


#: A port on the host's egress interface is open to the whole LAN: from 1024
#: up, one VM per number, a few per VM.
EXPOSE_EXTERNAL_MIN = 1024
EXPOSE_MAX = 16
#: The ranges softnet's block list holds shut (``SOFTNET_BLOCK``). An allow that
#: covers one of them whole, or anything wider, lifts the isolation.
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ALLOW_MIN_PREFIX = 8


def _port(what: str, value, low: int, high: int = 65535) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise VmError("vm_bad_request", f"{what}: {low}-{high} 사이의 정수여야 합니다")
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or not low <= int(text) <= high:
        raise VmError("vm_bad_request", f"{what} {text!r}: {low}-{high} 사이의 정수여야 합니다")
    return int(text)


def check_expose(external, internal) -> str:
    """``external:internal``: the port on the host's egress interface and the
    port in the guest."""
    return (f"{_port('외부 포트', external, EXPOSE_EXTERNAL_MIN)}:"
            f"{_port('게스트 포트', internal, 1)}")


def split_expose(text: str) -> tuple:
    ext, sep, inner = str(text).partition(":")
    if not sep:
        raise VmError("vm_bad_request", f"노출 {text!r}: 외부포트:게스트포트 형식이어야 합니다")
    return check_expose(ext, inner).split(":")


def check_allow(value, host_addrs=()) -> str:
    """An IPv4 CIDR softnet may let through. Refused: anything not IPv4, a
    prefix shorter than /8 (0.0.0.0/0 included), a range that holds a whole
    private block (10/8, 172.16/12, 192.168/16), and a range that holds one of
    the host's own addresses (the guest would reach the host's ports)."""
    text = str(value).strip()
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise VmError("vm_bad_request", f"허용 주소 {text!r}: IPv4 주소 또는 CIDR이어야 합니다") from None
    if net.version != 4:
        raise VmError("vm_bad_request", f"허용 주소 {text!r}: IPv4만 쓸 수 있습니다")
    if net.prefixlen < ALLOW_MIN_PREFIX or any(net.supernet_of(p) for p in PRIVATE_NETS):
        raise VmError("vm_allow_too_wide",
                      f"허용 주소 {net}: 범위가 너무 넓어 격리가 사라집니다. "
                      f"/{ALLOW_MIN_PREFIX}보다 넓은 값, 0.0.0.0/0, 사설 대역(10/8, 172.16/12, 192.168/16) 전체는 쓸 수 없습니다. "
                      "/32 주소 하나를 쓰는 것이 일반적입니다")
    for addr in host_addrs:
        try:
            if ipaddress.ip_address(addr) in net:
                raise VmError("vm_allow_holds_host",
                              f"허용 주소 {net}: 이 호스트 자신의 주소 {addr}를 포함합니다. "
                              "게스트가 호스트의 포트에 접속할 수 있게 되므로 거절합니다")
        except ValueError:
            continue
    return str(net)


def check_user(value: str) -> str:
    if not _USER_RE.fullmatch(value):
        raise VmError("vm_bad_request", f"ssh_user {value!r}: not a plain account name")
    return value


def check_path(what: str, value: str, *, empty_ok: bool = False) -> str:
    """An absolute path of plain characters with no '..' segment (no glob, no
    quote, no space, no newline)."""
    if not value and empty_ok:
        return ""
    if not _PATH_RE.fullmatch(value) or "/../" in value + "/" or value.endswith("/.."):
        raise VmError("vm_bad_request", f"{what} {value!r}: an absolute path of plain "
                      "characters is required")
    return value


LOG_LIMIT = 1024 * 1024


def rotate_log(path: Path, limit: int = LOG_LIMIT) -> None:
    """Keep one previous generation: a log that has reached ``limit`` becomes
    ``<name>.1`` (replacing the older one) before the next run appends."""
    try:
        if path.stat().st_size >= limit:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def _plain(value) -> str:
    """A free-text field the daemon only stores: no control characters, bounded."""
    return "".join(c for c in str(value or "") if c.isprintable())[:512]


def check_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise VmError("vm_bad_address", f"not an IP address: {value[:60]!r}") from None


class VmError(Exception):
    """A refusal with a stable ``code`` the tools hand back to the session."""

    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.code = code
        self.extra = extra

    def as_dict(self) -> dict:
        # ``error`` is what a client that shows only that field prints, so it
        # carries the reading; ``code`` is the stable part to act on.
        text = str(self)
        return {"ok": False, "code": self.code,
                "error": self.code if text == self.code else f"{self.code}: {text}",
                "message": text, **self.extra}


@dataclass
class Vm:
    id: str
    tart_name: str
    state: str = "stopped"
    ip: str = ""
    #: ``ssh_user`` is the account that is logged in on the guest's console
    #: (the readiness check compares it). ``ssh_key`` and ``ssh_ready`` keep
    #: their names from when the guest was reached by ssh: the daemon reaches
    #: it by ``tart exec`` now and uses no key; ``ssh_key`` is only what a
    #: record said, and ``ssh_ready`` means the guest agent answers.
    ssh_user: str = "admin"
    ssh_key: str = ""
    #: The guest's cua-driver, by absolute path: a name would resolve to
    #: whatever the guest's PATH holds, and the host's own never applies.
    cua_path: str = "/Users/admin/.local/bin/cua-driver"
    #: The guest channel (daemon/vm_channel.py): idle | connecting | up |
    #: down | failed. Not saved: a restarted daemon has no channel.
    channel: str = "idle"
    channel_error: str = ""
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
    #: tart's softnet network for this VM: True/False, or None to follow the
    #: daemon-wide ``vm_softnet``. A VM made from a preset is pinned True, so
    #: turning the global off does not open it; only a user route sets it.
    softnet: Optional[bool] = None
    #: ``external:guest`` TCP ports published on the host's egress interface,
    #: and the IPv4 CIDRs softnet lets through besides the public internet.
    #: Applied at the next start (``tart run`` takes them as arguments).
    expose: List[str] = field(default_factory=list)
    allow: List[str] = field(default_factory=list)
    #: What the running ``tart run`` was given (None: not known, e.g. adopted
    #: after a daemon restart). Not persisted.
    net_applied: Optional[dict] = None
    #: The user's lock: a locked VM is not started, resumed, acquired or called
    #: by anything the daemon runs (docs/vm-hub.md). Saved; set only while the
    #: VM is stopped, and only by the user.
    locked: bool = False
    #: Bumped each time the controller is asked to look again; a look counts
    #: only if it began after the last bump (not persisted).
    observe_gen: int = 0
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
            "ip": self.ip, "ssh_user": self.ssh_user, "ssh_key": self.ssh_key,
            "cua_path": self.cua_path, "channel": self.channel,
            "channel_error": self.channel_error, "ssh_ready": self.ssh_ready, "gui_ready": self.gui_ready,
            "cua_ready": self.cua_ready, "fault_reason": self.fault_reason,
            "controller": self.controller,
            "extra_controllers": dict(self.extra_controllers),
            "epoch": self.epoch, "grants": {k: list(v) for k, v in self.grants.items()},
            "human": self.human, "human_since": self.human_since,
            "needs_observe": self.needs_observe, "softnet": self.softnet, "locked": self.locked,
            "expose": list(self.expose), "allow": list(self.allow),
        }

    def net_pending(self) -> bool:
        """The saved expose/allow differ from what the running VM was given."""
        if self.state not in ("starting", "restoring", "running"):
            return False
        now = {"expose": list(self.expose), "allow": list(self.allow)}
        if self.net_applied is None:
            return bool(now["expose"] or now["allow"])
        return self.net_applied != now

    @classmethod
    def from_dict(cls, raw: dict) -> "Vm":
        vm = cls(id=check_name("id", str(raw["id"])),
                 tart_name=check_name("tart_name", str(raw.get("tart_name") or raw["id"])))
        vm.state = raw.get("state") if raw.get("state") in STATES else "stopped"
        vm.ip = check_ip(str(raw["ip"])) if raw.get("ip") else ""
        vm.ssh_user = check_user(str(raw.get("ssh_user") or "admin"))
        vm.ssh_key = _plain(raw.get("ssh_key"))  # unused: never a reason to reject a record
        vm.cua_path = check_path("cua_path", str(raw.get("cua_path") or vm.cua_path))
        vm.fault_reason = str(raw.get("fault_reason") or "")
        vm.softnet = raw["softnet"] if isinstance(raw.get("softnet"), bool) else None
        # A hand-edited file does not lift the isolation: what the setters
        # would refuse is dropped here.
        vm.expose = _kept(raw.get("expose"), lambda v: check_expose(*split_expose(v)))
        vm.allow = _kept(raw.get("allow"), check_allow)
        vm.locked = raw.get("locked") is True   # anything but a JSON true reads as unlocked
        vm.epoch = int(raw.get("epoch") or 0)
        vm.grants = {
            str(s): [x for x in SCOPES if x in (v or [])]
            for s, v in (raw.get("grants") or {}).items()
        }
        return vm


def _kept(items, check) -> List[str]:
    out: List[str] = []
    for item in items if isinstance(items, list) else []:
        try:
            value = check(item)
        except (VmError, TypeError, ValueError):
            log.warning("dropped %r from a VM record: not acceptable", item)
            continue
        if value not in out:
            out.append(value)
    return out[:EXPOSE_MAX]

def locked_error(vm: "Vm") -> VmError:
    return VmError("vm_locked",
                   f"{vm.id}은(는) 잠겨 있어 사용할 수 없습니다. 사용자가 잠금을 해제해야 합니다",
                   locked=True)


def not_ready(vm: "Vm") -> VmError:
    """Why a running-looking VM cannot be used yet. A guest channel that is
    reconnecting (or has given up) is told apart from a VM that is still
    booting: the first is worth retrying in seconds, and a failed one needs
    the user."""
    if vm.state == "running" and vm.ssh_ready and vm.gui_ready and not vm.cua_ready:
        if vm.channel in ("connecting", "down"):
            return VmError("vm_unavailable",
                           f"{vm.id}: the guest channel is {vm.channel}"
                           + (f" ({vm.channel_error})" if vm.channel_error else ""),
                           retry_after=5, channel=vm.channel)
        if vm.channel == "failed":
            return VmError("vm_unavailable",
                           f"{vm.id}: the guest channel failed: {vm.channel_error}",
                           channel="failed")
    return VmError(
        "vm_not_ready",
        f"{vm.id} is {vm.state} (ssh={vm.ssh_ready} gui={vm.gui_ready} cua={vm.cua_ready})")


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
        self._rejected: list = []
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
            self._rejected = [r for r in (doc.get("rejected") or []) if isinstance(r, dict)]
            for raw in doc.get("vms") or []:
                try:
                    vm = Vm.from_dict(raw)
                except (KeyError, TypeError, ValueError, VmError) as exc:
                    # Not trusted, and not deleted either: it is kept in the
                    # file (under "rejected") so its grants survive a fix.
                    log.warning("vm record %r rejected: %s", (raw or {}).get("id") if isinstance(raw, dict) else raw, exc)
                    self._rejected.append({"record": raw, "why": str(exc)})
                    continue
                self._vms[vm.id] = vm
            taken = set()
            for vm in self._vms.values():
                keep = []
                for pair in vm.expose:
                    if pair.split(":")[0] in taken:
                        log.warning("%s: external port of %s is held by another VM; dropped", vm.id, pair)
                        continue
                    taken.add(pair.split(":")[0])
                    keep.append(pair)
                vm.expose = keep
            del self._rejected[:-REJECTED_KEEP]

    def load_and_reset(self) -> None:
        """Daemon start: read the file and drop what a restart invalidates."""
        self.load()
        if self.list():
            self.after_restart()

    def _save(self) -> None:
        if self.path is None:
            return
        doc = {"vms": [vm.to_dict() for vm in self._vms.values()]}
        if self._rejected:
            doc["rejected"] = self._rejected
        with atomic.scratch(self.path) as tmp:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            atomic.replace(tmp, self.path)

    def note(self, vm: str, what: str, **extra) -> None:
        """An audit row from the layers above (forced stop, adoption)."""
        with self._lock:
            self._log(vm, "", what, **extra)

    def _log(self, vm: str, session: str, what: str, **extra) -> None:
        # The record never carries screen contents or command text: who did
        # what, when, under which epoch, and how it ended.
        row = {"at": self._clock(), "vm": vm, "session": session, "what": what, **extra}
        self.audit_log.append(row)
        del self.audit_log[:-AUDIT_RING]
        if self._audit_sink is not None:
            self._audit_sink(row)

    # ---- records ------------------------------------------------------- #
    def add(self, vm_id: str, tart_name: str = "", *, ssh_user: str = "admin",
            ssh_key: str = "", softnet: Optional[bool] = None, cua_path: str = "") -> Vm:
        check_name("id", vm_id)
        check_name("tart_name", tart_name or vm_id)
        check_user(ssh_user)
        ssh_key = _plain(ssh_key)
        with self._lock:
            if vm_id in self._vms:
                raise VmError("vm_exists", f"{vm_id} is already registered")
            vm = Vm(id=vm_id, tart_name=tart_name or vm_id, ssh_user=ssh_user,
                    ssh_key=ssh_key, softnet=softnet)
            if cua_path:
                vm.cua_path = check_path("cua_path", cua_path)
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

    # ---- network exposure ----------------------------------------------- #
    def expose_add(self, vm_id: str, external, internal) -> Vm:
        pair = check_expose(external, internal)
        ext = pair.split(":")[0]
        with self._lock:
            vm = self.get(vm_id)
            for other in self._vms.values():
                for have in other.expose:
                    if have.split(":")[0] == ext:
                        raise VmError("vm_port_taken",
                                      f"외부 포트 {ext}는 이미 {other.id}에 노출되어 있습니다 ({have})")
            if len(vm.expose) >= EXPOSE_MAX:
                raise VmError("vm_bad_request", f"{vm_id}: 노출 포트는 최대 {EXPOSE_MAX}개입니다")
            vm.expose.append(pair)
            self._save()
            return vm

    def expose_remove(self, vm_id: str, external) -> Vm:
        ext = str(_port("external port", external, EXPOSE_EXTERNAL_MIN))
        with self._lock:
            vm = self.get(vm_id)
            left = [p for p in vm.expose if p.split(":")[0] != ext]
            if len(left) == len(vm.expose):
                raise VmError("vm_bad_request", f"{vm_id}는 외부 포트 {ext}를 노출하지 않습니다")
            vm.expose = left
            self._save()
            return vm

    def allow_add(self, vm_id: str, cidr, host_addrs=()) -> Vm:
        value = check_allow(cidr, host_addrs)
        with self._lock:
            vm = self.get(vm_id)
            if value in vm.allow:
                raise VmError("vm_bad_request", f"{vm_id}는 이미 {value}를 허용합니다")
            if len(vm.allow) >= EXPOSE_MAX:
                raise VmError("vm_bad_request", f"{vm_id}: 허용 주소는 최대 {EXPOSE_MAX}개입니다")
            vm.allow.append(value)
            self._save()
            return vm

    def allow_remove(self, vm_id: str, cidr) -> Vm:
        try:
            value = str(ipaddress.ip_network(str(cidr).strip(), strict=False))
        except ValueError:
            raise VmError("vm_bad_request", f"허용 주소 {cidr!r}: IPv4 주소 또는 CIDR이어야 합니다") from None
        with self._lock:
            vm = self.get(vm_id)
            if value not in vm.allow:
                raise VmError("vm_bad_request", f"{vm_id}는 {value}를 허용하지 않습니다")
            vm.allow.remove(value)

    # ---- the lock (the user's) -------------------------------------------- #
    def set_locked(self, vm_id: str, locked: bool) -> Vm:
        """Lock a stopped VM, or unlock any. A running VM is not stopped for
        this: the user shuts it down first."""
        with self._lock:
            vm = self.get(vm_id)
            if locked and vm.state != "stopped":
                raise VmError("vm_lock_needs_stop",
                              f"{vm_id}은(는) {vm.state} 상태입니다. 먼저 정상 종료한 뒤 잠그십시오"
                              "(자동으로 종료하지 않습니다)")
            vm.locked = bool(locked)
            self._save()
            return vm

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

    def set_channel(self, vm_id: str, status: str, error: str = "") -> Vm:
        """The guest channel's state. ``up`` is what makes the cua daemon
        count as ready; every other status clears it."""
        with self._lock:
            vm = self.get(vm_id)
            vm.channel = status
            vm.channel_error = error
            vm.cua_ready = status == "up"
            self._log(vm_id, "", "channel", status=status, error=error)
            return vm

    def adopt(self, vm_id: str, state: str) -> Vm:
        """Set the state tart reports, whatever the record said. For the
        layer that reconciles after a daemon restart; every other move goes
        through :meth:`set_state`."""
        with self._lock:
            vm = self.get(vm_id)
            if state not in STATES or state == "fault":
                raise VmError("vm_bad_state", f"cannot adopt {state!r}")
            if state in OCCUPIED:
                held = self.holders(except_id=vm_id)
                if vm.state not in OCCUPIED and len(held) >= self.slot_limit:
                    raise VmError("vm_slots_full", "more VMs run than the slot limit",
                                  holders=held)
            old, vm.state = vm.state, state
            vm.fault_reason = ""
            vm.ssh_ready = vm.gui_ready = vm.cua_ready = False
            if state != "running":
                self._drop_control(vm, why=f"adopted {state}")
            self._log(vm_id, "", "adopt", frm=old, to=state)
            self._save()
            return vm

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
                vm.ip = check_ip(ip)
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
        if scopes is not None:
            # A malformed list must not revoke less than it says and still
            # answer ok: a string would be read letter by letter.
            if isinstance(scopes, str) or not isinstance(scopes, (list, tuple, set)) or not scopes:
                raise VmError("vm_bad_scope", "scopes must be a non-empty list, or omitted for all")
            scopes = set(scopes)
            unknown = {s for s in scopes if s not in SCOPES}
            if unknown:
                raise VmError("vm_bad_scope", f"unknown scope(s): {sorted(map(str, unknown))}")
        with self._lock:
            vm = self.get(vm_id)
            have = set(vm.grants.get(session, []))
            if scopes is None or VIEW in scopes:
                have = set()
            else:
                have -= scopes
                if CONTROL in scopes:
                    have.discard(EXEC)  # exec runs on top of control
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
            if vm.locked:
                raise locked_error(vm)
            if not vm.ready:
                raise not_ready(vm)
            if vm.human == "driving":
                raise VmError("vm_busy", "the user is driving the VM", retry_after=5)
            if vm.controller and vm.controller != session:
                return {"ok": False, "code": "vm_held",
                        "error": f"vm_held: {vm.controller} holds control of {vm_id}",
                        "held_by": vm.controller, "epoch": vm.epoch}
            if vm.controller != session:
                vm.controller = session
                vm.epoch += 1
                vm.needs_observe = True
                vm.observe_gen += 1
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
            if vm.locked:
                self._log(vm_id, session, "refused", scope=scope, why="locked")
                raise locked_error(vm)
            if scope == VIEW:
                pass
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
                raise not_ready(vm)
            vm.inflight += 1
            self._log(vm_id, session, "call", scope=scope, epoch=vm.epoch)
            return vm

    def end_call(self, vm_id: str, session: str, scope: str, *, ok: bool = True,
                 observe: bool = False, began_at_gen: Optional[int] = None) -> None:
        """``observe``: the call returned a look at the screen. It clears the
        controller's "look first" mark only when it succeeded, came from the
        controller, and began after the mark was last set (``began_at_gen`` is
        the VM's ``observe_gen`` when this very call began)."""
        with self._lock:
            vm = self.get(vm_id)
            vm.inflight = max(0, vm.inflight - 1)
            if (ok and observe and scope == VIEW and vm.needs_observe
                    and (session == vm.controller or session in vm.extra_controllers)
                    and began_at_gen == vm.observe_gen):
                vm.needs_observe = False
            self._log(vm_id, session, "done", scope=scope, ok=ok)

    # ---- the user at the screen ------------------------------------------ #
    def human_driving(self, vm_id: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            if vm.locked:
                raise locked_error(vm)
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
                vm.observe_gen += 1
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
                    "inflight": vm.inflight, "net_pending": vm.net_pending(),
                    "net_applied_known": vm.net_applied is not None,
                    "locked_but_running": vm.locked and vm.state in OCCUPIED}
