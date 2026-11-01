"""The VM lock (vm-setup-8s6): a locked VM is not started, acquired or called by
anything the daemon runs, and only the user sets or clears it."""
from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher.daemon import actors, vm_tart
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_presets import api_for, build
from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}


def run(coro):
    return asyncio.run(coro)


def running(reg, vm_id="a"):
    reg.claim_slot(vm_id)
    reg.set_state(vm_id, "running")
    reg.set_ready(vm_id, ssh=True, gui=True, cua=True)


# ---- the registry --------------------------------------------------------------- #
def test_only_a_stopped_vm_is_locked_and_any_vm_is_unlocked(tmp_path):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    reg.set_locked("a", True)
    assert reg.get("a").locked and reg.view("a")["locked"] is True
    again = Registry(path)
    again.load()
    assert again.get("a").locked is True                  # survives a restart
    reg.set_locked("a", False)
    running(reg)
    with pytest.raises(VmError) as err:
        reg.set_locked("a", True)
    assert err.value.code == "vm_lock_needs_stop" and "정상 종료" in str(err.value)
    assert reg.get("a").state == "running" and not reg.get("a").locked   # not stopped for it


@pytest.mark.parametrize("value", ["true", 1, "yes", None, [], {"x": 1}, 0, "false"])
def test_a_hand_edited_file_reads_anything_but_true_as_unlocked(tmp_path, value):
    path = tmp_path / "vms.json"
    path.write_text(json.dumps({"vms": [{"id": "a", "locked": value}]}))
    reg = Registry(path)
    reg.load()
    assert reg.get("a").locked is False


def test_a_true_in_the_file_is_a_lock(tmp_path):
    path = tmp_path / "vms.json"
    path.write_text(json.dumps({"vms": [{"id": "a", "locked": True}]}))
    reg = Registry(path)
    reg.load()
    assert reg.get("a").locked is True


def test_a_locked_vm_is_not_acquired_called_or_taken_over(tmp_path):
    reg = Registry()
    reg.add("a")
    reg.grant("a", "s1", ["view", "control", "exec"])
    running(reg)
    reg.get("a").locked = True            # a locked VM that is running (found by reconcile)
    for call in (lambda: reg.acquire("a", "s1"),
                 lambda: reg.begin_call("a", "s1", "view"),
                 lambda: reg.begin_call("a", "s1", "control"),
                 lambda: reg.begin_call("a", "s1", "exec"),
                 lambda: reg.human_driving("a")):
        with pytest.raises(VmError) as err:
            call()
        assert err.value.code == "vm_locked" and "잠겨 있어" in str(err.value)
    assert reg.view("a")["locked_but_running"] is True
    # no grant is still reported as no grant first
    with pytest.raises(VmError) as err:
        reg.acquire("a", "stranger")
    assert err.value.code == "vm_no_grant"
    reg.set_locked("a", False)
    assert reg.acquire("a", "s1")["ok"]


# ---- the runner ----------------------------------------------------------------- #
def test_a_locked_vm_is_not_started_or_resumed(tmp_path):
    fake, reg, mgr, _, _ = build(tmp_path)
    reg.set_locked("src", True)
    with pytest.raises(VmError) as err:
        mgr.start("src")
    assert err.value.code == "vm_locked" and not fake.spawned and reg.get("src").state == "stopped"
    reg.set_locked("src", False)
    assert mgr.start("src").state == "running"


def test_reconcile_records_a_locked_vm_that_runs_and_does_not_stop_it(tmp_path):
    fake, reg, mgr, _, _ = build(tmp_path)
    reg.set_locked("src", True)
    fake.tart["src"] = "running"
    changed = mgr.reconcile()
    assert reg.get("src").state == "running" and reg.get("src").locked
    assert any("locked but running" in c for c in changed)
    assert not any(c[:2] == ["tart", "stop"] for c in fake.calls)
    assert any(r.get("what") == "locked_but_running" for r in reg.audit_log)
    assert reg.view("src")["locked_but_running"] is True


def test_a_locked_vm_may_still_be_stopped_and_saved_as_a_preset(tmp_path):
    fake, reg, mgr, presets, _ = build(tmp_path)
    reg.set_locked("src", True)
    assert presets.save("src", "gold").source == "src"       # stopped: allowed
    fake.tart["src"] = "running"
    mgr.reconcile()
    fake.shutdown_stops_vm = True
    assert mgr.stop("src").state == "stopped"


# ---- the routes ----------------------------------------------------------------- #
def test_lock_and_unlock_are_the_users_and_audited(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            for op in ("lock", "unlock"):
                resp = await api.post(f"/api/vm/src/{op}", json={}, headers=AGENT)
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert not api.reg.get("src").locked
            ok = await api.post("/api/vm/src/lock", json={})
            assert ok.status == 200 and (await ok.json())["locked"] is True
            start = await api.post("/api/vm/src/start", json={})
            assert start.status == 423 and (await start.json())["code"] == "vm_locked"
            api.reg.grant("src", "s1", ["view", "control", "exec"])
            for path in ("/api/vm/src/acquire", "/api/vm/src/exec", "/api/vm/src/call"):
                resp = await api.post(path, json={"session": "s1", "command": "true", "tool": "screenshot"},
                                      headers=AGENT)
                assert resp.status == 423 and (await resp.json())["code"] == "vm_locked", path
            frame = await api.get("/api/vm/src/frame?session=s1", headers=AGENT)
            assert frame.status == 423
            tools = await api.get("/api/vm/src/tools?session=s1", headers=AGENT)
            assert tools.status == 423
            human = await api.post("/api/vm/src/human", json={"action": "driving"})
            assert human.status == 423
            listed = await (await api.get("/api/vm?session=s1", headers=AGENT)).json()
            assert listed["vms"][0]["locked"] is True and "locked_but_running" not in listed["vms"][0]
            assert any(r.get("what") == "lock" for r in api.reg.audit_log)
            await api.post("/api/vm/src/unlock", json={})
            assert api.reg.get("src").locked is False
            assert any(r.get("what") == "unlock" for r in api.reg.audit_log)
    run(go())


def test_lock_refuses_a_running_vm_and_names_the_step(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.fake.tart["src"] = "running"
            running(api.reg, "src")
            bad = await api.post("/api/vm/src/lock", json={})
            body = await bad.json()
            assert bad.status == 409 and body["code"] == "vm_lock_needs_stop" and "정상 종료" in body["error"]
            assert api.reg.get("src").state == "running"
    run(go())


def test_a_locked_vm_that_is_found_running_gets_no_channel(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.reg.set_locked("src", True)
            api.fake.tart["src"] = "running"
            resp = await api.post("/api/vm-reconcile", json={})
            assert resp.status == 200
            await asyncio.sleep(0.3)
            assert api.reg.get("src").state == "running"
            with pytest.raises(VmError) as err:     # no channel was opened to it
                api.server.app["vm_channels"].get("src")
            assert "no guest channel" in str(err.value)
    run(go())
