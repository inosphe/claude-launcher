"""The VM registry (vm-setup-8s6): slots, grants, exclusive control with an
epoch, and the user taking the screen."""
from __future__ import annotations

import json

import pytest

from claude_launcher.daemon import paths, vmhub
from claude_launcher.daemon.vmhub import Registry, VmError


def _ready(reg, vm_id="a", ip="192.168.64.2"):
    reg.claim_slot(vm_id)
    reg.set_state(vm_id, "running")
    reg.set_ready(vm_id, ssh=True, gui=True, cua=True, ip=ip)


@pytest.fixture
def reg():
    r = Registry()
    r.add("a")
    return r


def _code(exc_info):
    return exc_info.value.code


# --------------------------------------------------------------------------- #
# slots and state
# --------------------------------------------------------------------------- #
def test_a_third_start_is_refused_with_the_holders():
    r = Registry()
    for name in "abc":
        r.add(name)
    r.claim_slot("a")
    r.claim_slot("b")
    with pytest.raises(VmError) as e:
        r.claim_slot("c")
    assert _code(e) == "vm_slots_full"
    assert sorted(e.value.extra["holders"]) == ["a", "b"]
    assert r.get("c").state == "stopped"


def test_a_suspended_vm_holds_no_slot_and_restores_into_one():
    r = Registry()
    for name in "abc":
        r.add(name)
    _ready(r, "a")
    r.set_state("a", "saving")
    r.set_state("a", "suspended")
    r.claim_slot("b")
    r.claim_slot("c")
    with pytest.raises(VmError) as e:
        r.claim_slot("a")
    assert _code(e) == "vm_slots_full"
    r.set_state("c", "stopping")
    r.set_state("c", "stopped")
    assert r.claim_slot("a").state == "restoring"


def test_a_state_move_that_is_not_allowed_is_refused(reg):
    with pytest.raises(VmError) as e:
        reg.set_state("a", "running")
    assert _code(e) == "vm_bad_state"
    reg.set_state("a", "fault", reason="tart died")
    assert reg.get("a").fault_reason == "tart died"
    reg.set_state("a", "stopped")
    assert reg.get("a").fault_reason == ""


def test_readiness_needs_all_three(reg):
    reg.claim_slot("a")
    reg.set_state("a", "running")
    reg.set_ready("a", ssh=True, gui=True)
    assert not reg.get("a").ready
    reg.set_ready("a", cua=True)
    assert reg.get("a").ready
    reg.set_state("a", "stopping")
    assert not reg.get("a").ready


# --------------------------------------------------------------------------- #
# grants
# --------------------------------------------------------------------------- #
def test_control_and_exec_imply_view_and_nothing_is_granted_by_default(reg):
    assert reg.get("a").grants == {}
    assert reg.grant("a", "s1", ["control"]) == ["view", "control"]
    assert reg.grant("a", "s1", ["exec"]) == ["view", "control", "exec"]
    with pytest.raises(VmError) as e:
        reg.grant("a", "s1", ["root"])
    assert _code(e) == "vm_bad_scope"


def test_exec_is_refused_without_its_own_grant(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    reg.begin_call("a", "s1", "view")
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "exec", epoch)
    assert _code(e) == "vm_no_grant"


def test_revoking_control_ends_the_control_held(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.acquire("a", "s1")
    reg.revoke("a", "s1", ["control"])
    assert reg.get("a").controller == ""
    assert reg.get("a").grants["s1"] == ["view"]


# --------------------------------------------------------------------------- #
# exclusive control
# --------------------------------------------------------------------------- #
def test_acquire_needs_a_grant_and_a_ready_vm(reg):
    with pytest.raises(VmError) as e:
        reg.acquire("a", "s1")
    assert _code(e) == "vm_no_grant"
    reg.grant("a", "s1", ["control"])
    with pytest.raises(VmError) as e:
        reg.acquire("a", "s1")
    assert _code(e) == "vm_not_ready"


def test_one_controller_and_the_other_is_told_who(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.get("a").grants["s2"] = ["view", "control"]  # legacy: several holders
    first = reg.acquire("a", "s1")
    second = reg.acquire("a", "s2")
    assert first["ok"] and not second["ok"]
    assert second["held_by"] == "s1"
    assert reg.acquire("a", "s1")["epoch"] == first["epoch"]  # idempotent


def test_a_call_under_an_old_epoch_is_refused(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.get("a").grants["s2"] = ["view", "control"]
    old = reg.acquire("a", "s1")["epoch"]
    reg.release("a", "s1")
    reg.acquire("a", "s2")
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", old)
    assert _code(e) == "vm_not_controller"
    # even if the loser is made controller again, the old epoch is stale
    reg.release("a", "s2")
    reg.acquire("a", "s1")
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", old)
    assert _code(e) == "vm_stale"


def test_the_first_control_call_after_acquire_must_follow_a_look(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", epoch)
    assert _code(e) == "vm_observe_first"
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s1", "control", epoch)


def test_a_session_that_exits_gives_control_back_and_keeps_its_grant(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.acquire("a", "s1")
    reg.session_exited("s1")
    assert reg.get("a").controller == ""
    assert reg.get("a").grants["s1"] == ["view", "control"]


def test_a_duplicate_controller_needs_a_grant_a_reason_and_a_controller(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.get("a").grants["s2"] = ["view", "control"]
    with pytest.raises(VmError) as e:
        reg.add_extra_controller("a", "s2", "pair")
    assert _code(e) == "vm_no_controller"
    epoch = reg.acquire("a", "s1")["epoch"]
    with pytest.raises(VmError) as e:
        reg.add_extra_controller("a", "s2", " ")
    assert _code(e) == "vm_reason_required"
    reg.add_extra_controller("a", "s2", "reviewing the run")
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s2", "view")
    reg.begin_call("a", "s2", "control", epoch)


def test_stopping_the_vm_drops_control(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.acquire("a", "s1")
    reg.set_state("a", "stopping")
    assert reg.get("a").controller == ""


# --------------------------------------------------------------------------- #
# the user at the screen
# --------------------------------------------------------------------------- #
def test_while_the_user_drives_control_is_refused_and_view_is_not(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.human_driving("a")
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", epoch)
    assert _code(e) == "vm_busy"
    reg.begin_call("a", "s1", "view")


def test_a_call_in_flight_is_waited_for_not_cancelled(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s1", "control", epoch)
    assert reg.human_driving("a")["draining"] is True
    reg.end_call("a", "s1", "control")
    assert reg.view("a")["draining"] is False


def test_handing_back_keeps_control_but_asks_for_a_new_look(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.human_driving("a")
    reg.human_idle("a")
    assert reg.get("a").controller == "s1" and reg.get("a").epoch == epoch
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", epoch)
    assert _code(e) == "vm_observe_first"


def test_the_user_is_handed_back_after_being_still():
    now = [1000.0]
    r = Registry(clock=lambda: now[0])
    r.add("a")
    r.human_driving("a")
    now[0] += 10
    assert r.human_timeout("a", 15) is False
    r.human_touch("a")
    now[0] += 14
    assert r.human_timeout("a", 15) is False
    now[0] += 2
    assert r.human_timeout("a", 15) is True
    assert r.get("a").human == "idle"


def test_a_user_driving_blocks_a_new_acquire(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.human_driving("a")
    with pytest.raises(VmError) as e:
        reg.acquire("a", "s1")
    assert _code(e) == "vm_busy"


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_grants_survive_a_restart_but_control_and_readiness_do_not(home):
    path = paths.vm_json()
    r = Registry(path)
    r.add("a")
    _ready(r)
    r.grant("a", "s1", ["control", "exec"])
    r.acquire("a", "s1")
    before = r.get("a").epoch

    again = Registry(path)
    again.load()
    again.after_restart()
    vm = again.get("a")
    assert vm.grants["s1"] == ["view", "control", "exec"]
    assert vm.controller == "" and not vm.ready
    assert vm.epoch > before


def test_a_torn_file_loads_as_empty(home):
    path = paths.vm_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    r = Registry(path)
    r.load()
    assert r.list() == []


def test_the_audit_record_carries_no_command_text(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s1", "control", epoch)
    kinds = [row["what"] for row in reg.audit_log]
    assert {"grant", "acquire", "call"} <= set(kinds)
    assert all(set(row) <= {"at", "vm", "session", "what", "scope", "epoch", "ok",
                            "why", "scopes", "frm", "to", "reason", "left", "current"}
               for row in reg.audit_log)


# --------------------------------------------------------------------------- #
# review items 5, 6, 11
# --------------------------------------------------------------------------- #
def test_only_the_controllers_successful_look_clears_look_first(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.grant("a", "s2", ["view"])
    epoch = reg.acquire("a", "s1")["epoch"]
    # another session looking, or a look that has not finished, changes nothing
    reg.begin_call("a", "s2", "view")
    reg.end_call("a", "s2", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    assert reg.get("a").needs_observe
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", ok=False, observe=True, began_at_gen=reg.get("a").observe_gen)
    assert reg.get("a").needs_observe
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=False)  # a view call that is not a look
    assert reg.get("a").needs_observe
    with pytest.raises(VmError) as e:
        reg.begin_call("a", "s1", "control", epoch)
    assert _code(e) == "vm_observe_first"
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s1", "control", epoch)


def test_a_look_that_began_before_the_hand_back_does_not_count(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    epoch = reg.acquire("a", "s1")["epoch"]
    began = reg.begin_call("a", "s1", "view").observe_gen
    reg.human_driving("a")
    reg.human_idle("a")  # the screen changed under the look in flight
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=began)
    assert reg.get("a").needs_observe
    reg.begin_call("a", "s1", "view")
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=reg.get("a").observe_gen)
    reg.begin_call("a", "s1", "control", epoch)


@pytest.mark.parametrize("bad", ["view", "control", [], ["root"], ["view", 3], 7, {"view": 1}])
def test_a_malformed_revoke_changes_nothing_and_says_so(reg, bad):
    reg.grant("a", "s1", ["control", "exec"])
    with pytest.raises(VmError) as e:
        reg.revoke("a", "s1", bad)
    assert _code(e) == "vm_bad_scope"
    assert reg.get("a").grants["s1"] == ["view", "control", "exec"]


def test_revoking_control_takes_exec_with_it(reg):
    reg.grant("a", "s1", ["exec"])
    assert reg.revoke("a", "s1", ["control"]) == ["view"]
    assert reg.get("a").grants["s1"] == ["view"]


def test_a_record_the_checks_reject_stays_in_the_file(home):
    """Dropping it on the next save lost its grants for good."""
    path = paths.vm_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = {"id": "b", "ssh_user": "-oProxyCommand=x", "grants": {"s1": ["view", "control"]}}
    path.write_text(json.dumps({"vms": [{"id": "a"}, bad]}), encoding="utf-8")
    reg = Registry(path)
    reg.load()
    reg.add("c")                                     # any save
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert [v["id"] for v in doc["vms"]] == ["a", "c"]
    assert doc["rejected"][0]["record"]["grants"] == {"s1": ["view", "control"]}
    again = Registry(path)
    again.load()
    again.add("d")
    assert json.loads(path.read_text(encoding="utf-8"))["rejected"][0]["record"]["id"] == "b"


def test_a_look_is_judged_by_the_generation_its_own_call_began_in(reg):
    _ready(reg)
    reg.grant("a", "s1", ["control"])
    reg.acquire("a", "s1")
    first = reg.begin_call("a", "s1", "view").observe_gen
    reg.human_driving("a")
    reg.human_idle("a")
    second = reg.begin_call("a", "s1", "view").observe_gen
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=first)
    assert reg.get("a").needs_observe                # the older look does not count
    reg.end_call("a", "s1", "view", observe=True, began_at_gen=second)
    assert not reg.get("a").needs_observe
