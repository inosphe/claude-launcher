"""A suspended VM is not resumed while the host screen is locked (d-006)."""
from __future__ import annotations

import pytest

from claude_launcher.daemon import vm_tart
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_tart import Fake


def test_host_locked_reads_the_ioreg_key():
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return 0, '  | {\n  |   "CGSSessionScreenIsLocked" = Yes\n  | }\n', ""

    assert vm_tart.host_locked(run) is True
    assert calls == [["ioreg", "-n", "Root", "-d1"]]
    assert vm_tart.host_locked(lambda a, t: (0, '"IOConsoleLocked" = No\n', "")) is False
    assert vm_tart.host_locked(lambda a, t: (1, "", "boom")) is False


def test_a_locked_host_refuses_the_resume_and_leaves_the_vm_suspended(tmp_path):
    fake = Fake()
    reg = Registry()
    reg.add("a", ssh_user="admin")
    locked = [True]
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5, locked=lambda: locked[0])
    reg.claim_slot("a")
    reg.set_state("a", "running")
    mgr.suspend("a")
    fake.tart["a"] = "suspended"
    spawned = len(fake.spawned) if hasattr(fake, "spawned") else None
    with pytest.raises(VmError) as err:
        mgr.start("a")
    assert err.value.code == "vm_host_locked"
    assert reg.get("a").state == "suspended"
    assert spawned is None or len(fake.spawned) == spawned
    locked[0] = False
    assert mgr.start("a").state == "running"


def test_a_stopped_vm_starts_on_a_locked_host(tmp_path):
    fake = Fake()
    reg = Registry()
    reg.add("a", ssh_user="admin")
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5, locked=lambda: True)
    assert mgr.start("a").state == "running"
