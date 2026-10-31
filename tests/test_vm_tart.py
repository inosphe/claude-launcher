"""The VM runner (vm-setup-8s6, step 2), driven by a fake tart and ssh: no VM
is started and no guest is reached."""
from __future__ import annotations

import json

import pytest

from claude_launcher.daemon import vm_tart
from claude_launcher.daemon.vmhub import Registry, VmError


class Fake:
    """Answers the commands the runner makes, and keeps what it was asked."""

    def __init__(self):
        self.tart = {"a": "stopped", "b": "stopped", "c": "stopped"}
        self.calls = []
        self.spawned = []
        self.ip = "192.168.64.2"
        self.ssh_ok = True
        self.console = "admin"
        self.shutdown_stops_vm = True
        self.suspend_rc = 0
        #: polls of ``tart list`` before a suspend shows as done (-1: never)
        self.suspend_lag = 0
        self._suspending = {}
        self.execs = []
        self.exec_result = (0, "ok\n", "")

    def run(self, argv, timeout):
        argv = list(argv)
        self.calls.append(argv)
        # Every tart call ends its options before the VM name; the checks
        # below look at the call without that marker.
        if argv[0] == "tart" and len(argv) > 2 and argv[2] == "--":
            assert argv[1] in ("ip", "stop", "suspend", "exec"), argv
            argv = argv[:2] + argv[3:]
        if argv[:2] == ["tart", "list"]:
            for name, left in list(self._suspending.items()):
                if left == 0:
                    self.tart[name] = "suspended"
                    del self._suspending[name]
                elif left > 0:
                    self._suspending[name] = left - 1
            rows = [{"Name": n, "State": s, "Running": s == "running", "Source": "Local"}
                    for n, s in self.tart.items()]
            return 0, json.dumps(rows), ""
        if argv[:2] == ["tart", "ip"]:
            return (0, self.ip + "\n", "") if self.tart.get(argv[2]) == "running" else (1, "", "no ip")
        if argv[:2] == ["tart", "suspend"]:
            if self.suspend_rc == 0:
                self._suspending[argv[2]] = self.suspend_lag
            return self.suspend_rc, "", "boom" if self.suspend_rc else ""
        if argv[:2] == ["tart", "stop"]:
            self.tart[argv[2]] = "stopped"
            return 0, "", ""
        if argv[:2] == ["tart", "exec"]:
            self.execs.append(argv)
            return self.exec_result
        if argv[0] == "ssh":
            cmd = argv[-1]
            if not self.ssh_ok:
                return 255, "", "refused"
            if "shutdown" in cmd:
                if self.shutdown_stops_vm:
                    for n, s in self.tart.items():
                        if s == "running":
                            self.tart[n] = "stopped"
                return 255, "", ""
            if "/dev/console" in cmd:
                return 0, self.console + "\n", ""
            return 0, "", ""
        raise AssertionError(argv)

    def spawn(self, argv, log):
        self.spawned.append(list(argv))
        self.tart[argv[-1]] = "running"
        return 4242


@pytest.fixture
def env(tmp_path):
    fake = Fake()
    reg = Registry()
    for name in "abc":
        reg.add(name, ssh_user="admin", ssh_key="/keys/k")
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5)
    return fake, reg, mgr


def test_start_runs_suspendable_and_waits_for_ssh_and_a_console_login(env):
    fake, reg, mgr = env
    vm = mgr.start("a")
    assert fake.spawned == [["tart", "run", "--suspendable", "--", "a"]]
    assert vm.state == "running" and vm.ip == "192.168.64.2"
    assert vm.ssh_ready and vm.gui_ready and not vm.cua_ready and not vm.ready
    ssh = [c for c in fake.calls if c[0] == "ssh"][0]
    assert "-i" in ssh and "/keys/k" in ssh and "admin@192.168.64.2" in ssh


def test_no_graphics_is_an_option_not_the_default(env, tmp_path):
    fake, reg, _ = env
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, graphics=False)
    assert mgr.run_argv("a") == ["tart", "run", "--suspendable", "--no-graphics", "--", "a"]


def test_start_that_finds_no_console_login_ends_in_fault(env):
    fake, reg, mgr = env
    fake.console = "root"
    with pytest.raises(VmError) as e:
        mgr.start("a")
    assert e.value.code == "vm_boot_timeout"
    assert reg.get("a").state == "fault"
    assert "vm_boot_timeout" in reg.get("a").fault_reason


def test_a_third_vm_is_not_started(env):
    fake, reg, mgr = env
    mgr.start("a")
    mgr.start("b")
    with pytest.raises(VmError) as e:
        mgr.start("c")
    assert e.value.code == "vm_slots_full"
    assert len(fake.spawned) == 2 and reg.get("c").state == "stopped"


def test_start_of_a_vm_tart_does_not_have_gives_it_back(env):
    fake, reg, mgr = env
    del fake.tart["a"]
    with pytest.raises(VmError) as e:
        mgr.start("a")
    assert e.value.code == "tart_missing"
    assert reg.get("a").state == "fault"


def test_stop_syncs_inside_the_guest_and_never_calls_tart_stop(env):
    fake, reg, mgr = env
    mgr.start("a")
    vm = mgr.stop("a")
    assert vm.state == "stopped" and not vm.ready
    cmds = [c[-1] for c in fake.calls if c[0] == "ssh" and "shutdown" in c[-1]]
    assert cmds and cmds[0].startswith("sync;")
    assert ["tart", "stop", "--", "a"] not in fake.calls


def test_a_guest_that_does_not_stop_is_left_stopping_not_forced(env):
    fake, reg, mgr = env
    mgr.start("a")
    fake.shutdown_stops_vm = False
    with pytest.raises(VmError) as e:
        mgr.stop("a")
    assert e.value.code == "vm_stop_timeout"
    assert reg.get("a").state == "stopping"
    assert ["tart", "stop", "--", "a"] not in fake.calls


def test_force_stop_is_recorded_as_possible_data_loss(env):
    fake, reg, mgr = env
    mgr.start("a")
    fake.shutdown_stops_vm = False
    with pytest.raises(VmError):
        mgr.stop("a")
    mgr.force_stop("a")
    assert reg.get("a").state == "stopped"
    assert any(r["what"] == "forced_stop" and r["data_loss_possible"] for r in reg.audit_log)


def test_a_stop_the_guest_cannot_start_is_reported(env):
    fake, reg, mgr = env
    mgr.start("a")

    def run(argv, timeout):
        if argv[0] == "ssh" and "shutdown" in argv[-1]:
            return 1, "", "sudo: a password is required"
        return fake.run(argv, timeout)

    mgr._run = run
    with pytest.raises(VmError) as e:
        mgr.stop("a")
    assert e.value.code == "vm_stop_failed"
    assert "password" in str(e.value)


def test_suspend_then_start_resumes_in_a_slot(env):
    fake, reg, mgr = env
    mgr.start("a")
    assert mgr.suspend("a").state == "suspended"
    mgr.start("b")
    mgr.start("c")
    with pytest.raises(VmError) as e:
        mgr.start("a")
    assert e.value.code == "vm_slots_full"
    mgr.stop("c")
    # tart reports the suspended VM as suspended; run restores it
    fake.tart["a"] = "suspended"
    assert mgr.start("a").state == "running"
    assert fake.spawned[-1] == ["tart", "run", "--suspendable", "--", "a"]


def test_a_failed_suspend_leaves_the_vm_running(env):
    fake, reg, mgr = env
    mgr.start("a")
    fake.suspend_rc = 1
    with pytest.raises(VmError) as e:
        mgr.suspend("a")
    assert e.value.code == "tart_failed"
    assert reg.get("a").state == "running"


def test_reconcile_adopts_a_running_vm_and_releases_a_gone_one(env):
    fake, reg, mgr = env
    fake.tart["a"] = "running"
    reg.claim_slot("b")
    reg.set_state("b", "running")
    fake.tart["b"] = "stopped"
    fake.tart["c"] = "suspended"
    changed = mgr.reconcile()
    assert reg.get("a").state == "running" and not reg.get("a").ready
    assert reg.get("b").state == "stopped"
    assert reg.get("c").state == "suspended"
    assert len(changed) == 3
    assert mgr.reconcile() == []


def test_tart_list_failure_is_an_error_not_an_empty_fleet(env):
    fake, reg, mgr = env
    mgr._run = lambda argv, timeout: (1, "", "tart: not found")
    with pytest.raises(VmError) as e:
        mgr.reconcile()
    assert e.value.code == "tart_failed"


def test_exec_goes_through_tart_exec_and_is_cut_at_the_cap(env):
    fake, reg, mgr = env
    mgr.start("a")
    fake.exec_result = (0, "x" * (vm_tart.OUTPUT_CAP + 5), "")
    out = mgr.exec("a", "ls /Applications")
    assert fake.execs == [["tart", "exec", "a", "/bin/sh", "-c", "ls /Applications"]]
    assert out["rc"] == 0 and len(out["stdout"]) == vm_tart.OUTPUT_CAP and out["truncated"]
    assert not any(c[0] == "ssh" and "ls /Applications" in c[-1] for c in fake.calls)


def test_exec_on_a_vm_that_is_not_running_is_refused(env):
    fake, reg, mgr = env
    with pytest.raises(VmError) as e:
        mgr.exec("a", "true")
    assert e.value.code == "vm_not_ready"


def test_a_resume_that_tart_cannot_do_leaves_the_vm_suspended_and_fails_fast(env, tmp_path):
    fake, reg, mgr = env
    mgr.start("a")
    mgr.suspend("a")
    mgr.start("b")
    mgr.start("c")
    mgr.stop("c")

    def spawn(argv, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Error: VZErrorDomain Code=6 (resume)\n")
        return 99  # tart run exits at once; the VM stays suspended

    fake.tart["a"] = "suspended"
    mgr._spawn = spawn
    mgr._alive = lambda pid: False
    with pytest.raises(VmError) as e:
        mgr.start("a")
    assert e.value.code == "tart_exited" and "VZErrorDomain" in str(e.value)
    assert reg.get("a").state == "suspended"
    assert any(r["what"] == "resume_failed" for r in reg.audit_log)


def test_a_fresh_start_whose_tart_exits_is_a_fault(env):
    fake, reg, mgr = env
    mgr._spawn = lambda argv, log: 99
    mgr._alive = lambda pid: False
    with pytest.raises(VmError) as e:
        mgr.start("a")
    assert e.value.code == "tart_exited"
    assert reg.get("a").state == "fault"


def test_suspend_waits_for_tart_to_report_suspended(env):
    fake, reg, mgr = env
    mgr.start("a")
    fake.suspend_lag = 3
    vm = mgr.suspend("a")
    assert vm.state == "suspended" and fake.tart["a"] == "suspended"


def test_a_suspend_tart_never_finishes_keeps_the_slot(env):
    fake, reg, mgr = env
    mgr.start("a")
    mgr.suspend_wait = 0.05
    fake.suspend_lag = -1
    with pytest.raises(VmError) as e:
        mgr.suspend("a")
    assert e.value.code == "vm_suspend_timeout"
    assert reg.get("a").state == "saving"
    assert "a" in reg.holders()
