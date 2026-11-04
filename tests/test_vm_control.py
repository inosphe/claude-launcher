"""One session holds ``control`` of a VM (vm-setup-8s6, s2-vmcontrol): a second
grant is refused naming the holder, the user alone can steal it, and the
victim loses control (and a running controller role) but keeps its other scopes."""
from __future__ import annotations

import pytest

from claude_launcher.daemon.vmhub import Registry, VmError
from test_vm_api import AGENT, OTHER, _up, scenario


def _grant(api, session, scopes, **extra):
    return api.post("/api/vm/a/grant", json={"session": session, "scopes": scopes, **extra})


@scenario
async def test_a_second_control_grant_is_refused_naming_the_holder(api):
    await _up(api)
    assert (await _grant(api, "s1", ["control"])).status == 200
    resp = await _grant(api, "s2", ["control"])
    body = await resp.json()
    assert resp.status == 409 and body["code"] == "vm_control_held"
    assert body["holders"] == ["s1"] and "s1" in body["message"]
    assert "control" not in api.reg.get("a").grants.get("s2", [])
    # view/exec/files stay open to many sessions
    assert (await _grant(api, "s2", ["view", "files"])).status == 200
    assert (await _grant(api, "s1", ["control"])).status == 200  # re-granting the holder is fine


@scenario
async def test_steal_moves_control_and_keeps_the_other_scopes(api):
    await _up(api)
    await _grant(api, "s1", ["control", "files"])
    resp = await _grant(api, "s2", ["control"], steal=True)
    assert resp.status == 200
    grants = api.reg.get("a").grants
    assert grants["s2"] == ["view", "control"]
    assert grants["s1"] == ["view", "files"]
    audit = [r for r in api.reg.audit_log if r["what"] == "steal_control"]
    assert audit and audit[-1]["from"] == "s1" and audit[-1]["to"] == "s2" and audit[-1]["vm"] == "a"


@scenario
async def test_steal_from_must_name_the_holder(api):
    await _up(api)
    await _grant(api, "s1", ["control"])
    resp = await _grant(api, "s2", ["control"], steal_from="s3")
    assert resp.status == 409 and (await resp.json())["code"] == "vm_control_held"
    assert (await _grant(api, "s2", ["control"], steal_from="s1")).status == 200
    assert api.reg.control_holders("a") == ["s2"]


@scenario
async def test_a_session_cannot_grant_or_steal(api):
    await _up(api)
    await _grant(api, "s1", ["control"])
    resp = await api.post("/api/vm/a/grant", json={"session": "s2", "scopes": ["control"], "steal": True},
                          headers=OTHER)
    assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
    assert api.reg.control_holders("a") == ["s1"]


@scenario
async def test_steal_ends_the_running_controller_and_bumps_the_epoch(api):
    await _up(api)
    await _grant(api, "s1", ["control"])
    epoch = (await (await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)).json())["epoch"]
    assert api.reg.get("a").controller == "s1"
    await _grant(api, "s2", ["control"], steal=True)
    vm = api.reg.get("a")
    assert vm.controller == "" and vm.epoch > epoch
    stale = await api.post("/api/vm/a/call", json={"session": "s1", "tool": "click", "epoch": epoch}, headers=AGENT)
    assert stale.status == 403
    got = await api.post("/api/vm/a/acquire", json={"session": "s2"}, headers=OTHER)
    assert got.status == 200


@scenario
async def test_legacy_several_holders_need_a_steal_from_all(api):
    await _up(api)
    vm = api.reg.get("a")
    vm.grants["s1"] = ["view", "control"]
    vm.grants["s2"] = ["view", "control"]
    body = await (await api.get("/api/vm/a")).json()
    assert sorted(s for s, sc in body["grants"].items() if "control" in sc) == ["s1", "s2"]  # left as is
    resp = await _grant(api, "s3", ["control"])
    assert resp.status == 409 and (await resp.json())["holders"] == ["s1", "s2"]
    partial = await _grant(api, "s3", ["control"], steal_from="s1")
    assert partial.status == 409 and "s2" in (await partial.json())["message"]
    assert api.reg.control_holders("a") == ["s1", "s2"]
    assert (await _grant(api, "s3", ["control"], steal=True)).status == 200
    assert api.reg.control_holders("a") == ["s3"]


@scenario
async def test_a_first_control_grant_needs_no_steal(api):
    await _up(api)
    assert (await _grant(api, "s1", ["view", "control"])).status == 200


def test_registry_rejects_a_bad_steal_value():
    reg = Registry()
    reg.add("a")
    reg.grant("a", "s1", ["control"])
    with pytest.raises(VmError) as e:
        reg.grant("a", "s2", ["control"], steal=3)
    assert e.value.code == "vm_bad_request"
