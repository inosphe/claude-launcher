"""A steal is a loan (vm-setup-8s6, s2-vmlease): the control a session took is
given back when it ends or its quick-fork merges, along the chain of steals."""
from __future__ import annotations

import json

import pytest

from claude_launcher.daemon import vm_api
from claude_launcher.daemon.vmhub import Registry, VmError
from test_vm_api import OTHER, _up, scenario

pytestmark = pytest.mark.usefixtures("vm_on")


def _reg(path=None, alive=None):
    reg = Registry(path)
    reg.add("a")
    if alive is not None:
        reg.session_alive = alive
    return reg


def _holders(reg):
    return reg.control_holders("a")


def test_a_steal_is_recorded_and_the_session_end_gives_it_back():
    reg = _reg()
    reg.grant("a", "A", ["control", "exec"])
    reg.grant("a", "B", ["control"], steal=True)
    vm = reg.get("a")
    assert vm.leases == [{"from": "A", "to": "B", "scopes": ["control", "exec"], "at": vm.leases[0]["at"]}]
    assert vm.grants["A"] == ["view"]
    reg.session_exited("B")
    assert _holders(reg) == ["A"] and "exec" in vm.grants["A"]       # control and exec come back together
    assert vm.grants["B"] == ["view"] and vm.leases == []
    log = [r for r in reg.audit_log if r["what"] == "steal_return"][-1]
    assert (log["from"], log["to"], log["reason"], log["vm"]) == ("B", "A", "kill", "a")


def test_the_chain_a_b_c_goes_back_one_link_at_a_time():
    reg = _reg()
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.grant("a", "C", ["control"], steal=True)
    reg.session_exited("C")
    assert _holders(reg) == ["B"]
    reg.session_exited("B")
    assert _holders(reg) == ["A"]


def test_the_chain_skips_an_ended_owner():
    ended = {"B"}
    reg = _reg(alive=lambda n: n not in ended)
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.grant("a", "C", ["control"], steal=True)
    reg.session_exited("B")                     # B holds nothing now: nothing to give back
    assert _holders(reg) == ["C"] and len(reg.get("a").leases) == 2
    ended.add("C")
    reg.session_exited("C")
    assert _holders(reg) == ["A"] and reg.get("a").leases == []
    assert [r["to"] for r in reg.audit_log if r["what"] == "steal_return"] == ["A"]


def test_control_is_left_empty_when_everyone_on_the_way_has_ended():
    ended = {"A", "B"}
    reg = _reg(alive=lambda n: n not in ended)
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.grant("a", "C", ["control"], steal=True)
    reg.session_exited("C")
    assert _holders(reg) == [] and reg.get("a").leases == []


def test_a_lost_link_gives_nothing_to_a_guessed_owner():
    ended = {"B"}
    reg = _reg(alive=lambda n: n not in ended)
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.grant("a", "C", ["control"], steal=True)
    reg.get("a").leases = [x for x in reg.get("a").leases if x["to"] != "B"]    # A -> B lost
    reg.session_exited("C")
    assert _holders(reg) == []


def test_damaged_records_are_dropped_on_load_and_nothing_is_returned(tmp_path):
    path = tmp_path / "vm.json"
    reg = Registry(path)
    reg.add("a")
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    raw = path.read_text()
    assert '"from": "A"' in raw or '"from":"A"' in raw
    path.write_text(raw.replace('"from": "A"', '"from": 7').replace('"from":"A"', '"from":7'))
    again = Registry(path)
    again.load()
    assert again.get("a").leases == []
    again.session_exited("B")
    assert again.control_holders("a") == ["B"]          # it keeps what it has; nothing is guessed


def test_the_records_survive_a_restart_and_a_restart_returns_nothing(tmp_path):
    path = tmp_path / "vm.json"
    reg = Registry(path)
    reg.add("a")
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    again = Registry(path)
    again.load()
    assert again.get("a").leases[0]["to"] == "B" and again.control_holders("a") == ["B"]
    again.session_exited("B")                           # a real exit later still returns
    assert again.control_holders("a") == ["A"]


def test_the_user_revoking_or_reassigning_by_hand_cancels_the_record():
    reg = _reg()
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.revoke("a", "B", ["control"])
    assert reg.get("a").leases == []
    reg.grant("a", "D", ["control"])                    # a plain grant on an empty control
    reg.session_exited("B")
    assert _holders(reg) == ["D"]


def test_the_original_owner_ending_first_keeps_the_link():
    reg = _reg()
    reg.grant("a", "A", ["control"])
    reg.grant("a", "B", ["control"], steal=True)
    reg.grant("a", "C", ["control"], steal=True)
    reg.session_exited("A")                             # the original owner ends
    assert _holders(reg) == ["C"] and len(reg.get("a").leases) == 2


def test_return_leases_by_hand_and_when_there_is_none():
    reg = _reg()
    reg.grant("a", "A", ["control"])
    with pytest.raises(VmError) as e:
        reg.return_leases("a", "A")
    assert e.value.code == "vm_no_lease"
    reg.grant("a", "B", ["control"], steal=True)
    reg.return_leases("a", "B", "manual")
    assert _holders(reg) == ["A"]
    assert [r for r in reg.audit_log if r["what"] == "steal_return"][-1]["reason"] == "manual"


def test_the_merge_hook_returns_only_for_a_merge():
    app = {"vm_registry": _reg()}
    reg = app["vm_registry"]
    reg.grant("a", "A", ["control"])
    reg.grant("a", "A-qf1", ["control"], steal=True)
    hook = vm_api.merge_hook(app)
    hook(None, "A-qf1", {"kind": "handoff"}, "")
    assert _holders(reg) == ["A-qf1"]
    hook(None, "A-qf1", {"kind": "merge"}, "")
    assert _holders(reg) == ["A"]
    assert [r for r in reg.audit_log if r["what"] == "steal_return"][-1]["reason"] == "merge"


@scenario
async def test_the_return_button_route_is_the_users_and_returns_now(api):
    await _up(api)
    await api.post("/api/vm/a/grant", json={"session": "s1", "scopes": ["control"]})
    await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["control"], "steal": True})
    body = await (await api.get("/api/vm/a")).json()
    assert body["leases"][0]["from"] == "s1" and body["leases"][0]["to"] == "s2"
    no = await api.post("/api/vm/a/return_steal", json={"session": "s2"}, headers=OTHER)
    assert no.status == 403
    seen = await (await api.get("/api/vm/a", headers=OTHER)).json()
    assert "leases" not in seen
    ok = await api.post("/api/vm/a/return_steal", json={"session": "s2"})
    assert ok.status == 200 and api.reg.control_holders("a") == ["s1"]
    again = await api.post("/api/vm/a/return_steal", json={"session": "s2"})
    assert again.status == 409 and (await again.json())["code"] == "vm_no_lease"
