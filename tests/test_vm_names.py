"""What may reach a command line or a file name (vm-setup-8s6, review item 1):
ids, tart names, ssh users and paths are checked when a record is made and
when one is read back, and every ssh/tart call ends its options first."""
from __future__ import annotations

import json

import pytest

from claude_launcher.daemon import paths, vm_channel, vm_frames, vm_tart, vmhub
from claude_launcher.daemon.vmhub import Registry, VmError

BAD_NAMES = ["-oProxyCommand=x", "a/b", "../x", "a b", "a;b", "", "a*", "a\nb", "x" * 70, "a..b",
             "-"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_bad_id_or_tart_name_is_refused(name):
    reg = Registry()
    with pytest.raises(VmError):
        reg.add(name)
    if name:  # no tart name means the id
        with pytest.raises(VmError):
            reg.add("ok", tart_name=name)


@pytest.mark.parametrize("user", ["-oProxyCommand=x", "a b", "a@b", "", "a;b", "a/b"])
def test_a_bad_ssh_user_is_refused(user):
    with pytest.raises(VmError):
        Registry().add("a", ssh_user=user)


@pytest.mark.parametrize("key", ["-i", "keys/k", "/k/../../etc/x", "/k k", "/k;x", "/k*", "/k\n"])
def test_a_bad_key_path_is_refused(key):
    with pytest.raises(VmError):
        Registry().add("a", ssh_key=key)


def test_a_plain_record_passes():
    reg = Registry()
    reg.add("agent-mac-01", ssh_user="admin", ssh_key="/Users/me/.ssh/agent_mac_01_ed25519")
    reg.add("t.03_x", tart_name="agent-mac-t03")


def test_a_hostile_record_in_the_file_is_dropped_when_read(home):
    path = paths.vm_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"id": "a", "tart_name": "a"}
    rows = [good,
            {"id": "b", "ssh_user": "-oProxyCommand=touch x"},
            {"id": "c", "cua_path": "/x/../../bin/sh"},
            {"id": "d", "ip": "1.2.3.4 -oProxyCommand=x"},
            {"id": "../e"}]
    path.write_text(json.dumps({"vms": rows}), encoding="utf-8")
    reg = Registry(path)
    reg.load()
    assert [v.id for v in reg.list()] == ["a"]


def test_an_address_must_be_an_ip():
    reg = Registry()
    reg.add("a")
    assert reg.set_ready("a", ip="192.168.64.9").ip == "192.168.64.9"
    with pytest.raises(VmError) as e:
        reg.set_ready("a", ip="-oProxyCommand=x")
    assert e.value.code == "vm_bad_address"


def _vm():
    reg = Registry()
    vm = reg.add("a", ssh_key="/k/key")
    vm.ip = "192.168.64.2"
    return vm


def test_every_guest_call_ends_its_options_before_the_vm_name():
    vm = _vm()
    for argv in (vm_channel.exec_argv(vm), vm_frames.capture_argv(vm)):
        assert argv[argv.index("--") + 1] == "a" and argv[0] == "tart"
    mgr = vm_tart.Manager(Registry(), log_dir=None, run=lambda a, t: (0, "", ""))
    seen = []
    mgr._run = lambda argv, timeout: seen.append(argv) or (0, "", "")
    mgr._guest(vm, "true")
    assert seen[0][:4] == ["tart", "exec", "--", "a"]


def test_every_tart_call_ends_its_options_before_the_name():
    reg = Registry()
    reg.add("a")
    reg.get("a").state = "running"
    mgr = vm_tart.Manager(reg, log_dir=None, run=lambda a, t: (0, "", ""))
    calls = []
    mgr._run = lambda argv, timeout: calls.append(argv) or (0, "", "")
    mgr.exec("a", "true")
    assert calls[-1][:4] == ["tart", "exec", "--", "a"]
    assert mgr.run_argv("a")[-2:] == ["--", "a"]
