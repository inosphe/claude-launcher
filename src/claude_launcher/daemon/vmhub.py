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
  (a guest shell) and ``files`` (copy files between a host folder and the
  guest's work folder, ``daemon/vm_files.py``). ``control`` and ``exec`` imply
  ``view``: driving a screen needs seeing it. ``files`` implies nothing and
  nothing implies it. Nothing is granted by default, and only the user grants.
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
import uuid
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import atomic

log = logging.getLogger(__name__)

SLOT_LIMIT = 2

VIEW, CONTROL, EXEC, FILES = "view", "control", "exec", "files"
SCOPES = (VIEW, CONTROL, EXEC, FILES)

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
REQUESTS_MAX = 10
FILES_LOG_KEEP = 50
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


#: Where a VM's guest work folder may be: under a user's home, or under a temp
#: directory. ``(prefix parts, parts needed below the prefix)``: a home needs
#: the account and one folder in it, so ``/Users/admin`` itself is not one.
_WORK_ROOTS = ((("Users",), 2), (("private", "tmp"), 1), (("tmp",), 1))
FILES_DIR_DEFAULT = "/Users/{user}/work"


def check_guest_dir(value: str) -> str:
    """The folder in the guest that file transfers are limited to. Absolute,
    plain characters (``check_path``), written canonically (no '//', no '.',
    no trailing '/'), under ``/Users/<account>/``, ``/private/tmp/`` or
    ``/tmp/``, and no hidden folder or ``Library`` on the way: a transfer into
    ``~/.ssh`` or a home itself is refused here, not left to the user's care."""
    text = str(value or "")
    check_path("guest work folder", text)
    parts = text.split("/")[1:]
    if text == "/" or any(p in ("", ".") for p in parts):
        raise VmError("vm_bad_request", f"게스트 작업 폴더 {text!r}: '//', '.', 끝의 '/' 없이 정규 형태로 써야 합니다")
    for prefix, below in _WORK_ROOTS:
        if tuple(parts[:len(prefix)]) == prefix and len(parts) >= len(prefix) + below:
            rest = parts[len(prefix):]
            if any(p.startswith(".") for p in rest) or (prefix == ("Users",) and rest[1].lower() == "library"):
                raise VmError("vm_bad_request",
                              f"게스트 작업 폴더 {text!r}: 숨김 폴더나 Library 아래는 쓸 수 없습니다")
            return text
    raise VmError("vm_bad_request",
                  f"게스트 작업 폴더 {text!r}: /Users/<계정>/<폴더>, /private/tmp/<폴더>, /tmp/<폴더> 아래만 쓸 수 있습니다")


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
    #: Where this VM's disk was cloned from, when the daemon did the cloning:
    #: ``{"kind": "preset", "preset": name, "preset_created_at": ts, "at": ts}``
    #: (a VM made from a preset). ``{}`` is "no record": registered VMs, and
    #: everything made before this field, are not guessed at. Saved.
    cloned_from: dict = field(default_factory=dict)
    #: The guest folder file transfers are limited to ("": the default,
    #: ``/Users/<ssh_user>/work``). Saved; only the user sets it.
    files_dir: str = ""
    #: Host folders mounted in the guest (``daemon/vm_share.py``): records
    #: ``{name, host_path, mode}``, set by the user only, applied at the next
    #: start (``tart run --dir``).
    shared_dirs: List[dict] = field(default_factory=list)
    #: What the running ``tart run`` was given (None: not known). Not persisted.
    shares_applied: Optional[list] = None
    #: Shares left out of the last start, name -> why. Not persisted.
    shares_skipped: Dict[str, str] = field(default_factory=dict)
    #: Bumped each time the controller is asked to look again; a look counts
    #: only if it began after the last bump (not persisted).
    observe_gen: int = 0
    #: Calls running right now (not persisted).
    inflight: int = 0

    @property
    def ready(self) -> bool:
        return self.state == "running" and self.ssh_ready and self.gui_ready and self.cua_ready

    @property
    def work_dir(self) -> str:
        return self.files_dir or FILES_DIR_DEFAULT.format(user=self.ssh_user)

    @property
    def files_ready(self) -> bool:
        """The guest agent answers, which is all a file transfer needs (no
        screen, no cua daemon)."""
        return self.state == "running" and self.ssh_ready

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
            "files_dir": self.files_dir, "shared_dirs": [dict(s) for s in self.shared_dirs],
            "cloned_from": dict(self.cloned_from),
        }

    def shares_pending(self, effective: Optional[list] = None) -> bool:
        """The shares this VM would get now (its own, plus the catalog's when the
        registry passes ``effective``) differ from what the running VM was given."""
        if self.state not in ("starting", "restoring", "running"):
            return False
        now = [vm_share.mount_key(s) for s in (self.shared_dirs if effective is None else effective)]
        if self.shares_applied is None:
            return bool(now)
        return [vm_share.mount_key(s) for s in self.shares_applied] != now

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
        vm.shared_dirs = vm_share.load_shares(raw.get("shared_dirs"))
        vm.cloned_from = clean_cloned_from(raw.get("cloned_from"))
        vm.locked = raw.get("locked") is True   # anything but a JSON true reads as unlocked
        try:
            vm.files_dir = check_guest_dir(raw["files_dir"]) if raw.get("files_dir") else ""
        except VmError:
            log.warning("%s: files_dir %r is not acceptable; the default is used", vm.id, raw.get("files_dir"))
        vm.epoch = int(raw.get("epoch") or 0)
        vm.grants = {
            str(s): [x for x in SCOPES if x in (v or [])]
            for s, v in (raw.get("grants") or {}).items()
        }
        return vm


def clean_cloned_from(raw) -> dict:
    """A stored clone record, or ``{}`` when it is not a well-formed preset record."""
    if not isinstance(raw, dict) or raw.get("kind") != "preset":
        return {}
    name = raw.get("preset")
    try:
        check_name("preset", name)
        return {"kind": "preset", "preset": name,
                "preset_created_at": float(raw.get("preset_created_at") or 0.0), "at": float(raw.get("at") or 0.0)}
    except (TypeError, ValueError, VmError):
        return {}


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
        #: Recent file transfers per VM (path and byte count, never contents).
        self.files_log: Dict[str, deque] = {}
        #: Pending requests for access, vm id -> session -> request. A request
        #: grants nothing: the user approves it (or not). Not saved: a restarted
        #: daemon has none, and the session asks again.
        self.requests: Dict[str, Dict[str, dict]] = {}
        #: Shared folders for many VMs (``vm_share.load_catalog``): entries
        #: ``{name, host_path, mode, scope, vms, origin}``, the user's, saved.
        self.catalog: List[dict] = []

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
            self.catalog = vm_share.load_catalog(doc.get("share_catalog"))
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
        if self.catalog:
            doc["share_catalog"] = self.catalog
        with atomic.scratch(self.path) as tmp:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            atomic.replace(tmp, self.path)

    def note(self, vm: str, what: str, **extra) -> None:
        """An audit row from the layers above (forced stop, adoption)."""
        with self._lock:
            self._log(vm, "", what, **extra)

    def note_files(self, vm: str, session: str, **row) -> None:
        """One file transfer (or refusal) for the VM's recent-transfer list and
        the audit log."""
        with self._lock:
            self._log(vm, session, "files", **row)
            self.files_log.setdefault(vm, deque(maxlen=FILES_LOG_KEEP)).append(self.audit_log[-1])

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
            ssh_key: str = "", softnet: Optional[bool] = None, cua_path: str = "",
            cloned_from: Optional[dict] = None) -> Vm:
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
            vm.cloned_from = clean_cloned_from(cloned_from)
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
            self.requests.pop(vm_id, None)
            for entry in self.catalog:
                if vm_id in entry["vms"]:
                    entry["vms"] = [v for v in entry["vms"] if v != vm_id]
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
            self._save()
            return vm

    # ---- shared folders (the user's) ---------------------------------------- #
    def share_add(self, vm_id: str, share: dict) -> Vm:
        """Store a share that ``vm_share.check_share`` accepted."""
        with self._lock:
            vm = self.get(vm_id)
            if any(s["name"] == share["name"] for s in vm.shared_dirs):
                raise VmError("vm_bad_request", f"{vm_id}에는 이미 공유 {share['name']}가 있습니다")
            share = {**share, "origin": share.get("origin") or vm_share.make_origin(vm_share.ORIGIN_VM)}
            if len(vm.shared_dirs) >= vm_share.SHARE_MAX:
                raise VmError("vm_bad_request", f"{vm_id}: 공유 폴더는 최대 {vm_share.SHARE_MAX}개입니다")
            vm.shared_dirs.append(dict(share))
            self._save()
            return vm

    # ---- the share catalog (the user's) ------------------------------------- #
    def catalog_put(self, entry: dict, replace: bool = False, by_session: str = "") -> dict:
        """Add (or, with ``replace``, change) a catalog entry that
        ``vm_share.check_catalog_entry`` accepted."""
        with self._lock:
            have = next((e for e in self.catalog if e["name"] == entry["name"]), None)
            if have is not None and not replace:
                raise VmError("vm_bad_request", f"카탈로그에 이미 {entry['name']}가 있습니다")
            if have is None and replace:
                raise VmError("vm_bad_request", f"카탈로그에 {entry['name']}가 없습니다")
            if have is None and len(self.catalog) >= vm_share.CATALOG_MAX:
                raise VmError("vm_bad_request", f"카탈로그는 최대 {vm_share.CATALOG_MAX}개입니다")
            kind = vm_share.ORIGIN_ALL if entry["scope"] == vm_share.SCOPE_ALL else vm_share.ORIGIN_VMS
            row = {**{k: entry[k] for k in ("name", "host_path", "mode", "scope", "vms")},
                   "origin": vm_share.make_origin(kind, by_session)}
            if have is None:
                self.catalog.append(row)
            else:
                self.catalog[self.catalog.index(have)] = row
            self._save()
            return dict(row)

    def catalog_remove(self, name: str) -> None:
        with self._lock:
            left = [e for e in self.catalog if e["name"] != name]
            if len(left) == len(self.catalog):
                raise VmError("vm_bad_request", f"카탈로그에 {name!s}가 없습니다")
            self.catalog = left
            self._save()

    def effective_shares(self, vm_id: str) -> tuple:
        """``(shares, left_out)``: the VM's own shares followed by the catalog
        entries that apply to it. A catalog entry whose name is already taken by
        the VM's own share (the VM's wins) or that would be the fifth is left out,
        with the reason. The path rules are applied at start, not here."""
        with self._lock:
            vm = self.get(vm_id)
            shares = [dict(s) for s in vm.shared_dirs]
            left_out: Dict[str, str] = {}
            for entry in self.catalog:
                if not vm_share.applies(entry, vm_id):
                    continue
                if any(s["name"] == entry["name"] for s in shares):
                    left_out[entry["name"]] = "name_conflict: 이 VM 자체의 공유가 같은 이름을 쓰고 있습니다(VM 자체 항목이 우선합니다)"
                elif len(shares) >= vm_share.SHARE_MAX:
                    left_out[entry["name"]] = f"over_limit: 한 VM에는 공유 폴더가 최대 {vm_share.SHARE_MAX}개입니다"
                else:
                    shares.append({k: entry[k] for k in ("name", "host_path", "mode", "origin")})
            return shares, left_out

    def share_remove(self, vm_id: str, name: str) -> Vm:
        with self._lock:
            vm = self.get(vm_id)
            left = [s for s in vm.shared_dirs if s["name"] != name]
            if len(left) == len(vm.shared_dirs):
                raise VmError("vm_bad_request", f"{vm_id}에는 공유 {name!s}가 없습니다")
            vm.shared_dirs = left
            self._save()
            return vm

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

    def set_files_dir(self, vm_id: str, path: str) -> Vm:
        """The guest work folder ("" back to the default)."""
        value = check_guest_dir(path) if path else ""
        with self._lock:
            vm = self.get(vm_id)
            vm.files_dir = value
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

    def has_grant(self, session: str) -> bool:
        """Whether ``session`` holds any scope on any VM."""
        with self._lock:
            return any(vm.grants.get(session) for vm in self._vms.values())

    def revoke(self, vm_id: str, session: str, scopes=None) -> List[str]:
        """Drop ``scopes`` (all when omitted). Losing ``control`` ends the
        control the session holds; losing ``view`` loses control and exec too
        (files is independent and is dropped only by naming it)."""
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
            if scopes is None:
                have = set()
            else:
                have -= scopes
                if VIEW in scopes:
                    have -= {CONTROL, EXEC}  # they ride on view; files does not
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

    # ---- requests for access (the session asks, the user decides) ----------- #
    def request_add(self, vm_id: str, session: str, scopes, shares, reason: str) -> dict:
        """A session's ask for scopes and shared folders in one go. ``shares`` are
        records ``vm_share.check_share`` already accepted. Asking again replaces
        the session's earlier request."""
        with self._lock:
            vm = self.get(vm_id)
            if vm.locked:
                raise locked_error(vm)
            wanted = _normal(scopes)
            held = set(vm.grants.get(session, []))
            missing = [s for s in wanted if s not in held]
            have = {s["name"] for s in vm.shared_dirs}
            fresh = [s for s in shares if s["name"] not in have]
            if not missing and not fresh:
                raise VmError("vm_bad_request", "요청한 권한과 공유가 이미 모두 있습니다")
            if len(vm.shared_dirs) + len(fresh) > vm_share.SHARE_MAX:
                raise VmError("vm_bad_request", f"공유 폴더는 VM당 최대 {vm_share.SHARE_MAX}개입니다")
            pending = self.requests.setdefault(vm_id, {})
            if session not in pending and len(pending) >= REQUESTS_MAX:
                raise VmError("vm_bad_request", f"{vm_id}에 대기 중인 요청이 너무 많습니다. 사용자가 처리한 뒤 다시 요청하십시오")
            row = {"id": uuid.uuid4().hex[:12], "session": session, "scopes": missing, "shares": [dict(s) for s in fresh],
                   "reason": _plain(reason)[:200], "at": self._clock()}
            pending[session] = row
            self._log(vm_id, session, "request", scopes=missing, shares=[s["name"] for s in fresh])
            return row

    def request_get(self, vm_id: str, session: str) -> Optional[dict]:
        with self._lock:
            self.get(vm_id)
            return self.requests.get(vm_id, {}).get(session)

    def request_drop(self, vm_id: str, session: str) -> None:
        with self._lock:
            self.requests.get(vm_id, {}).pop(session, None)

    def request_list(self, vm_id: str) -> List[dict]:
        with self._lock:
            return [dict(r) for r in self.requests.get(vm_id, {}).values()]

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
            for pending in self.requests.values():
                pending.pop(session, None)
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
            if scope in (VIEW, FILES):
                pass    # no screen, no input: no controller, epoch or look-first
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
            if not (vm.files_ready if scope == FILES else vm.ready):
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

    def _share_rows(self, vm: Vm) -> List[dict]:
        """Every share this VM has or would have, with where it came from and
        whether the running VM has it: ``applied`` (mounted), ``pending`` (added
        since the start, or the VM is not running) or ``left_out`` with the
        reason (a rule at the last start, a name conflict, the limit)."""
        shares, left_out = self.effective_shares(vm.id)
        running = vm.state in ("starting", "restoring", "running")
        have = {vm_share.mount_key(s) for s in vm.shares_applied or []}
        rows = []
        for s in shares:
            row = vm_share.public(s)
            if s["name"] in vm.shares_skipped:
                row.update(state="left_out", why=vm.shares_skipped[s["name"]])
            elif running and vm_share.mount_key(s) in have:
                row["state"] = "applied"
            else:
                row["state"] = "pending"
            rows.append(row)
        for entry in self.catalog:
            if entry["name"] in left_out:
                rows.append({**vm_share.public(entry), "state": "left_out", "why": left_out[entry["name"]]})
        for name, why in vm.shares_skipped.items():
            if all(r["name"] != name for r in rows):
                rows.append({"name": name, "state": "left_out", "why": why})
        return rows

    def session_shares(self, vm_id: str) -> List[dict]:
        """The shares a session is told about: those that are (or, for a VM that is
        not running, will be) mounted, without the ones left out."""
        with self._lock:
            vm = self.get(vm_id)
            return [r for r in self._share_rows(vm) if r["state"] != "left_out" and r.get("host_path")]

    def view(self, vm_id: str) -> dict:
        with self._lock:
            vm = self.get(vm_id)
            return {**vm.to_dict(), "ready": vm.ready, "draining": vm.draining,
                    "inflight": vm.inflight, "net_pending": vm.net_pending(),
                    "net_applied_known": vm.net_applied is not None,
                    "locked_but_running": vm.locked and vm.state in OCCUPIED,
                    "shared_dirs": [vm_share.public(s) for s in vm.shared_dirs],
                    "share_rows": self._share_rows(vm),
                    "shares_pending": vm.shares_pending(self.effective_shares(vm_id)[0]),
                    "shares_skipped": dict(vm.shares_skipped),
                    "requests": [{**r, "shares": [vm_share.public(s) for s in r["shares"]]}
                                 for r in self.request_list(vm_id)]}


# vm_share raises VmError and uses check_name, so it is imported once those exist.
from . import vm_share  # noqa: E402
