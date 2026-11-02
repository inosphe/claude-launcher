"""VM presets (vm-setup-8s6): a stopped tart VM kept as a template, and new VMs
cloned from it.

A preset is a tart copy named ``preset-<name>`` plus a record here (description,
the VM it was saved from, when). The copy is made by ``tart clone`` from a
registered VM that is stopped, so nothing that runs is copied. A new VM is a
``tart clone`` of the preset under the new VM's name, registered with no grants
and no controller, and pinned to softnet so that turning the daemon-wide setting
off does not open it.

What a clone keeps: the guest's accounts, the admin password and keys are the
source's (a preset changes nothing inside the guest), and so is the guest DNS the
source was left with. The softnet block makes the gateway's DNS unusable, so the
source must have public DNS set before it is saved (docs/vm-hub.md).

Every tart call goes through the Manager's runner, so a test drives all of it
with a fake. Deleting is limited to the copy a preset record names: its tart
name must be ``preset-<name>``, must not be any registered VM's, and the record
must exist. Nothing here removes any other VM or image.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .. import atomic
from . import vm_tart, vmhub
from .vmhub import Registry, VmError, check_name

log = logging.getLogger(__name__)

PREFIX = "preset-"
DESCRIPTION_MAX = 500


def host_disk_free() -> int:
    """Bytes free where tart keeps its VMs."""
    home = Path(os.environ.get("TART_HOME") or Path.home() / ".tart")
    while not home.exists() and home != home.parent:
        home = home.parent
    return shutil.disk_usage(home).free


@dataclass
class Preset:
    name: str
    tart_name: str
    description: str = ""
    source: str = ""
    created_at: float = 0.0
    size_gb: int = 0
    ssh_user: str = "admin"
    cua_path: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, raw: dict) -> "Preset":
        name = check_name("preset", str(raw["name"]))
        return cls(
            name=name, tart_name=PREFIX + name,
            description=vmhub._plain(raw.get("description"))[:DESCRIPTION_MAX],
            source=str(raw.get("source") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            size_gb=int(raw.get("size_gb") or 0),
            ssh_user=vmhub.check_user(str(raw.get("ssh_user") or "admin")),
            cua_path=str(raw.get("cua_path") or ""),
        )


def check_preset_name(value: str) -> str:
    check_name("preset", value)
    if len(PREFIX + value) > 64:
        raise VmError("vm_bad_request", f"preset {value!r}: up to {64 - len(PREFIX)} characters")
    return value


class Presets:
    def __init__(self, path: Optional[Path], registry: Registry, manager: vm_tart.Manager, *,
                 clock: Callable[[], float] = time.time,
                 disk_free: Callable[[], int] = host_disk_free) -> None:
        self.path = path
        self.registry = registry
        self.manager = manager
        self._clock = clock
        self._disk_free = disk_free
        self._items: Dict[str, Preset] = {}
        #: Why the record file could not be read ("" when it can). A file that
        #: exists and is unreadable is left as it is: no preset is known, so
        #: nothing is deleted, and nothing is written over it.
        self._unreadable = ""
        #: One preset operation at a time: they share tart names.
        self._lock = threading.RLock()
        self.load()

    # ---- the record file ------------------------------------------------ #
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            self._unreadable = str(exc)
            log.warning("vm presets file %s is unreadable: %s", self.path, exc)
            return
        for raw in doc.get("presets") or []:
            try:
                item = Preset.from_dict(raw)
            except (KeyError, TypeError, ValueError, VmError) as exc:
                log.warning("vm preset record %r rejected: %s", raw, exc)
                continue
            self._items[item.name] = item

    def _writable(self) -> None:
        if self._unreadable:
            raise VmError("vm_presets_unreadable",
                          f"the presets file {self.path} cannot be read ({self._unreadable}); "
                          "it is left untouched. Fix or move it, then restart the daemon")

    def _save(self) -> None:
        if self.path is None:
            return
        self._writable()
        doc = {"presets": [p.to_dict() for p in self._items.values()]}
        with atomic.scratch(self.path) as tmp:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            atomic.replace(tmp, self.path)

    # ---- reading --------------------------------------------------------- #
    def list(self) -> List[dict]:
        with self._lock:
            held = {vm.tart_name for vm in self.registry.list()}
            try:
                present = {row.name for row in self.manager.tart_list()}
            except VmError:
                present = None
            rows = []
            for item in sorted(self._items.values(), key=lambda p: p.name):
                row = item.to_dict()
                row["present"] = None if present is None else item.tart_name in present
                row["registered_as_vm"] = item.tart_name in held
                rows.append(row)
            return rows

    def lineage_rows(self) -> List[dict]:
        """The presets as the lineage tree needs them: no ``tart list`` (that is
        ``list``'s ``present``), so the VM page can read it with every poll."""
        with self._lock:
            return [{"name": p.name, "source": p.source, "created_at": p.created_at,
                     "description": p.description} for p in sorted(self._items.values(), key=lambda p: p.name)]

    def get(self, name: str) -> Preset:
        item = self._items.get(name)
        if item is None:
            raise VmError("vm_unknown_preset", f"no preset named {name!r}")
        return item

    # ---- checks ----------------------------------------------------------- #
    def _need_room(self, size_gb: int, what: str) -> None:
        """Refuse a clone the disk cannot hold. ``size_gb`` is what tart reports
        the source uses; an APFS clone starts out sharing it, so this is the
        room the copy may grow into, not what is written now."""
        free_gb = self._disk_free() // 10 ** 9
        if size_gb and free_gb < size_gb:
            raise VmError("vm_disk_low",
                          f"{what}: {free_gb} GB free, {size_gb} GB needed (the source's size)",
                          free_gb=free_gb, needed_gb=size_gb)

    def _free_tart_name(self, name: str) -> None:
        if self.manager.tart_find(name) is not None:
            raise VmError("vm_name_taken", f"tart already has a VM named {name!r}")

    # ---- save ------------------------------------------------------------- #
    def save(self, vm_id: str, name: str, description: str = "") -> Preset:
        check_preset_name(name)
        description = vmhub._plain(description)[:DESCRIPTION_MAX]
        with self._lock:
            self._writable()
            vm = self.registry.get(vm_id)
            if name in self._items:
                raise VmError("vm_preset_exists", f"preset {name!r} exists; delete it first")
            if vm.state != "stopped":
                raise VmError("vm_bad_state",
                              f"{vm_id} is {vm.state}: a preset is made from a stopped VM "
                              "(shut it down first; a suspended VM is not copied)")
            row = self.manager.tart_find(vm.tart_name)
            if row is None:
                raise VmError("tart_missing", f"tart has no VM named {vm.tart_name!r}")
            if row.state != "stopped":
                raise VmError("vm_bad_state", f"tart reports {vm.tart_name} as {row.state}")
            tart_name = PREFIX + name
            self._free_tart_name(tart_name)
            self._need_room(row.size_gb, "preset")
            self.manager.tart_clone(vm.tart_name, tart_name)
            item = Preset(name=name, tart_name=tart_name, description=description, source=vm_id,
                          created_at=self._clock(), size_gb=row.size_gb,
                          ssh_user=vm.ssh_user, cua_path=vm.cua_path)
            self._items[name] = item
            try:
                self._save()
            except Exception:
                del self._items[name]
                self._remove_copy(tart_name)
                raise
            self.registry.note(vm_id, "preset_saved", preset=name)
            return item

    def describe(self, name: str, description: str) -> Preset:
        with self._lock:
            self._writable()
            item = self.get(name)
            item.description = vmhub._plain(description)[:DESCRIPTION_MAX]
            self._save()
            return item

    # ---- delete ----------------------------------------------------------- #
    def delete(self, name: str, confirm: str = "") -> None:
        """Remove a preset and its tart copy. ``confirm`` must be the preset's
        name: the caller names what is about to go."""
        with self._lock:
            self._writable()
            item = self.get(name)
            if confirm != name:
                raise VmError("vm_confirm", f"say which preset to delete: confirm must be {name!r}")
            if item.tart_name != PREFIX + name or not item.tart_name.startswith(PREFIX):
                raise VmError("vm_bad_request", f"{name}: the record does not name a preset copy")
            if item.tart_name in {vm.tart_name for vm in self.registry.list()}:
                raise VmError("vm_held", f"{item.tart_name} is registered as a VM; it is not a preset copy to delete")
            row = self.manager.tart_find(item.tart_name)
            if row is not None:
                if row.state != "stopped":
                    raise VmError("vm_bad_state", f"tart reports {item.tart_name} as {row.state}")
                self.manager.tart_delete(item.tart_name)
            del self._items[name]
            self._save()
            self.registry.note("", "preset_deleted", preset=name)

    def _remove_copy(self, tart_name: str) -> None:
        """Take back the copy a save made when its record could not be written
        (the name was free before the clone, so the copy is ours)."""
        try:
            self.manager.tart_delete(tart_name)
        except VmError as exc:
            log.warning("could not remove the copy %s: %s", tart_name, exc)

    # ---- new VM ----------------------------------------------------------- #
    def create_vm(self, name: str, vm_id: str) -> vmhub.Vm:
        """Clone the preset as ``vm_id`` and register it (stopped, no grants,
        softnet pinned on). Starting it is the caller's."""
        check_name("id", vm_id)
        with self._lock:
            self._writable()
            item = self.get(name)
            if vm_id in {vm.id for vm in self.registry.list()}:
                raise VmError("vm_exists", f"{vm_id} is already registered")
            if vm_id in {vm.tart_name for vm in self.registry.list()}:
                raise VmError("vm_exists", f"{vm_id} is the tart name of a registered VM")
            row = self.manager.tart_find(item.tart_name)
            if row is None:
                raise VmError("tart_missing", f"the preset's copy {item.tart_name!r} is gone from tart")
            if row.state != "stopped":
                raise VmError("vm_bad_state", f"tart reports {item.tart_name} as {row.state}")
            self._free_tart_name(vm_id)
            self._need_room(row.size_gb or item.size_gb, f"new VM {vm_id}")
            self.manager.tart_clone(item.tart_name, vm_id)
            try:
                vm = self.registry.add(vm_id, vm_id, ssh_user=item.ssh_user,
                                       softnet=True, cua_path=item.cua_path,
                                       cloned_from={"kind": "preset", "preset": name,
                                                    "preset_created_at": item.created_at, "at": time.time()})
            except Exception:
                # The copy was made by this call: only it is taken back.
                try:
                    self.manager.tart_delete(vm_id)
                except VmError as exc:
                    log.warning("could not remove the copy %s: %s", vm_id, exc)
                raise
            self.registry.note(vm_id, "created_from_preset", preset=name)
            return vm
