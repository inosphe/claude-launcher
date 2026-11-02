"""VM presets (vm-setup-8s6): a stopped tart copy to clone new VMs from, driven by
a fake tart; no VM is cloned or started."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import actors, vm_api, vm_channel, vm_frames, vm_presets, vm_tart
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_channel import World
from test_vm_frames import Capture
from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}


def build(tmp_path, *, free_gb=500, slot_limit=2, names=("src",)):
    fake = Fake()
    fake.tart = {n: "stopped" for n in names}
    fake.sizes = {n: 93 for n in names}
    reg = Registry(slot_limit=slot_limit)
    for n in names:
        reg.add(n, ssh_user="admin")
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn,
                          alive=lambda p: True, sleep=lambda s: None, stop_wait=0.05,
                          boot_wait=0.5, locked=lambda: False)
    path = tmp_path / "vm-presets.json"
    presets = vm_presets.Presets(path, reg, mgr, clock=lambda: 1000.0,
                                 disk_free=lambda: free_gb * 10 ** 9)
    return fake, reg, mgr, presets, path


def clones(fake):
    return [c for c in fake.calls if c[:2] == ["tart", "clone"]]


def deletes(fake):
    return [c for c in fake.calls if c[:2] == ["tart", "delete"]]


# ---- save ------------------------------------------------------------------ #
def test_save_copies_a_stopped_vm_and_keeps_a_private_record(tmp_path):
    fake, reg, _, presets, path = build(tmp_path)
    item = presets.save("src", "gold", "clean install")
    assert clones(fake) == [["tart", "clone", "--", "src", "preset-gold"]]
    assert item.tart_name == "preset-gold" and item.source == "src" and item.size_gb == 93
    assert fake.tart["preset-gold"] == "stopped"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["presets"][0]["name"] == "gold"
    again = vm_presets.Presets(path, reg, presets.manager)
    assert again.get("gold").description == "clean install"


@pytest.mark.parametrize("state,tart_state", [("running", "running"), ("suspended", "suspended"),
                                                ("stopped", "running")])
def test_save_refuses_a_vm_that_is_not_stopped(tmp_path, state, tart_state):
    fake, reg, _, presets, _ = build(tmp_path)
    if state != "stopped":
        reg.claim_slot("src")
        reg.set_state("src", "running")
        if state == "suspended":
            reg.set_state("src", "saving")
            reg.set_state("src", "suspended")
    fake.tart["src"] = tart_state
    with pytest.raises(VmError) as err:
        presets.save("src", "gold")
    assert err.value.code == "vm_bad_state" and not clones(fake)


def test_save_checks_names_room_and_clone_failure(tmp_path):
    fake, _, _, presets, path = build(tmp_path)
    for bad in ("", "a b", "../x", "x" * 58, "-x"):
        with pytest.raises(VmError):
            presets.save("src", bad)
    fake.tart["preset-taken"] = "stopped"
    with pytest.raises(VmError) as err:
        presets.save("src", "taken")
    assert err.value.code == "vm_name_taken"
    assert not clones(fake)

    fake2, _, _, low, _ = build(tmp_path, free_gb=50)
    with pytest.raises(VmError) as err:
        low.save("src", "gold")
    assert err.value.code == "vm_disk_low" and err.value.extra["needed_gb"] == 93
    assert not clones(fake2)

    fake3, _, _, bad_clone, path3 = build(tmp_path)
    fake3.clone_rc = 1
    with pytest.raises(VmError) as err:
        bad_clone.save("src", "gold")
    assert err.value.code == "tart_failed" and bad_clone.list() == []


def test_save_does_not_overwrite_a_preset(tmp_path):
    fake, _, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    with pytest.raises(VmError) as err:
        presets.save("src", "gold")
    assert err.value.code == "vm_preset_exists" and len(clones(fake)) == 1


# ---- delete ---------------------------------------------------------------- #
def test_delete_removes_the_named_copy_only_and_needs_the_name(tmp_path):
    fake, _, _, presets, path = build(tmp_path, names=("src", "other"))
    presets.save("src", "gold")
    with pytest.raises(VmError) as err:
        presets.delete("gold")
    assert err.value.code == "vm_confirm" and not deletes(fake)
    presets.delete("gold", "gold")
    assert deletes(fake) == [["tart", "delete", "--", "preset-gold"]]
    assert set(fake.tart) == {"src", "other"}
    assert json.loads(path.read_text())["presets"] == []


def test_delete_never_follows_a_record_to_another_vm(tmp_path):
    fake, reg, mgr, _, path = build(tmp_path, names=("src", "victim"))
    path.write_text(json.dumps({"presets": [{"name": "gold", "tart_name": "victim"}]}))
    presets = vm_presets.Presets(path, reg, mgr)
    presets.delete("gold", "gold")
    assert "victim" in fake.tart and deletes(fake) == []  # the copy preset-gold was not there


def test_delete_refuses_a_copy_that_is_registered_as_a_vm(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    reg.add("oops", "preset-gold")
    with pytest.raises(VmError) as err:
        presets.delete("gold", "gold")
    assert err.value.code == "vm_held" and not deletes(fake)


def test_a_missing_record_file_deletes_nothing(tmp_path):
    fake, _, _, presets, path = build(tmp_path)
    fake.tart["preset-gold"] = "stopped"  # a copy tart has, that no record names
    assert not path.exists()
    with pytest.raises(VmError) as err:
        presets.delete("gold", "gold")
    assert err.value.code == "vm_unknown_preset" and not deletes(fake)
    assert "preset-gold" in fake.tart


@pytest.mark.parametrize("junk", ["{not json", "[]", "\x00\x01"])
def test_an_unreadable_record_file_deletes_nothing_and_is_left_alone(tmp_path, junk):
    fake, reg, mgr, _, path = build(tmp_path)
    fake.tart["preset-gold"] = "stopped"
    path.write_text(junk)
    presets = vm_presets.Presets(path, reg, mgr)
    for call in (lambda: presets.delete("gold", "gold"),
                 lambda: presets.save("src", "other"),
                 lambda: presets.describe("gold", "x"),
                 lambda: presets.create_vm("gold", "new")):
        with pytest.raises(VmError) as err:
            call()
        assert err.value.code == "vm_presets_unreadable"
    assert not deletes(fake) and not clones(fake)
    assert path.read_text() == junk
    assert "preset-gold" in fake.tart


# ---- new VM ---------------------------------------------------------------- #
def test_create_clones_registers_without_grants_and_pins_softnet(tmp_path):
    fake, reg, mgr, presets, path = build(tmp_path)
    presets.save("src", "gold")
    vm = presets.create_vm("gold", "fresh")
    assert clones(fake)[-1] == ["tart", "clone", "--", "preset-gold", "fresh"]
    assert vm.state == "stopped" and vm.grants == {} and vm.controller == "" and vm.softnet is True
    assert reg.get("fresh").tart_name == "fresh"
    # pinned on even when the daemon-wide setting is off
    off = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, softnet=False)
    assert "--net-softnet" in off.run_argv("fresh", reg.get("fresh").softnet)
    assert "--net-softnet" not in off.run_argv("src", reg.get("src").softnet)
    # and it survives a registry reload
    saved = tmp_path / "vms.json"
    reg2 = Registry(saved)
    reg2.add("fresh", softnet=True)
    reg3 = Registry(saved)
    reg3.load()
    assert reg3.get("fresh").softnet is True


def test_create_refuses_taken_names_missing_copies_and_low_disk(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    for taken in ("src",):
        with pytest.raises(VmError) as err:
            presets.create_vm("gold", taken)
        assert err.value.code == "vm_exists"
    fake.tart["elsewhere"] = "stopped"
    with pytest.raises(VmError) as err:
        presets.create_vm("gold", "elsewhere")
    assert err.value.code == "vm_name_taken"
    for bad in ("", "a b", "../x"):
        with pytest.raises(VmError):
            presets.create_vm("gold", bad)
    with pytest.raises(VmError) as err:
        presets.create_vm("nope", "fresh")
    assert err.value.code == "vm_unknown_preset"
    before = len(clones(fake))
    presets._disk_free = lambda: 10 * 10 ** 9
    with pytest.raises(VmError) as err:
        presets.create_vm("gold", "fresh")
    assert err.value.code == "vm_disk_low" and len(clones(fake)) == before
    presets._disk_free = lambda: 500 * 10 ** 9
    del fake.tart["preset-gold"]
    with pytest.raises(VmError) as err:
        presets.create_vm("gold", "fresh")
    assert err.value.code == "tart_missing"


def test_a_failed_registration_removes_only_the_copy_this_call_made(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    original = reg.add

    def boom(*a, **k):
        raise VmError("vm_exists", "raced")
    reg.add = boom
    with pytest.raises(VmError):
        presets.create_vm("gold", "fresh")
    reg.add = original
    assert deletes(fake) == [["tart", "delete", "--", "fresh"]]
    assert set(fake.tart) == {"src", "preset-gold"}


def test_a_failed_clone_leaves_nothing_to_remove(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    fake.clone_rc = 1
    with pytest.raises(VmError):
        presets.create_vm("gold", "fresh")
    assert not deletes(fake) and "fresh" not in [v.id for v in reg.list()]


def test_the_tart_list_gives_a_preset_copy_its_size_in_gb():
    rows = vm_tart.parse_list(json.dumps([{"Name": "a", "State": "stopped", "Size": 93, "Disk": 140}]))
    assert (rows[0].size_gb, rows[0].disk_gb) == (93, 140)


# ---- the routes ------------------------------------------------------------ #
@contextlib.asynccontextmanager
async def api_for(tmp_path, **kw):
    fake, reg, mgr, presets, _ = build(tmp_path, **kw)
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    frames = vm_frames.Frames(reg, run=Capture(), interval=0.01)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub, frames, presets)
    async with TestClient(TestServer(app)) as client:
        client.fake, client.reg, client.presets = fake, reg, presets
        yield client


def run(coro):
    return asyncio.run(coro)


def test_the_routes_save_list_create_describe_and_delete(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            saved = await api.post("/api/vm-presets", json={"vm": "src", "name": "gold", "description": "d"})
            assert saved.status == 201
            listed = (await (await api.get("/api/vm-presets")).json())["presets"]
            assert listed[0]["name"] == "gold" and listed[0]["present"] is True
            made = await api.post("/api/vm-presets/gold/create", json={"id": "fresh"})
            body = await made.json()
            assert made.status == 201 and body["started"] is True and body["vm"]["state"] == "running"
            assert body["vm"]["softnet"] is True and body["vm"]["grants"] == {}
            assert "--net-softnet" in api.fake.spawned[-1]
            d = await api.post("/api/vm-presets/gold/describe", json={"description": "new text"})
            assert (await d.json())["description"] == "new text"
            no = await api.post("/api/vm-presets/gold/delete", json={})
            assert no.status == 400 and (await no.json())["code"] == "vm_confirm"
            yes = await api.post("/api/vm-presets/gold/delete", json={"confirm": "gold"})
            assert yes.status == 200 and "preset-gold" not in api.fake.tart
            tart_rows = (await (await api.get("/api/vm-tart")).json())["tart"]
            assert {r["name"] for r in tart_rows} >= {"src", "fresh"}
    run(go())


def test_a_full_slot_table_leaves_the_new_vm_registered_and_stopped(tmp_path):
    async def go():
        async with api_for(tmp_path, slot_limit=1, names=("src", "busy")) as api:
            api.fake.tart["busy"] = "running"  # the startup reconcile agrees with the record
            api.reg.claim_slot("busy")
            api.reg.set_state("busy", "running")
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            made = await api.post("/api/vm-presets/gold/create", json={"id": "fresh"})
            body = await made.json()
            assert made.status == 201 and body["started"] is False and "slots" in body["why"]
            assert api.reg.get("fresh").state == "stopped"
            assert not any(c[:2] == ["tart", "run"] for c in api.fake.spawned)
    run(go())


def test_a_start_that_fails_keeps_the_registered_copy_and_says_why(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            api.fake.agent_ok = False
            made = await api.post("/api/vm-presets/gold/create", json={"id": "fresh"})
            body = await made.json()
            assert made.status == 201 and body["started"] is False and body["why"]
            assert api.reg.get("fresh")
            assert "fresh" in api.fake.tart and not deletes(api.fake)
    run(go())


def test_every_preset_route_is_the_users_only(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            calls = [
                api.get("/api/vm-presets", headers=AGENT),
                api.post("/api/vm-presets", json={"vm": "src", "name": "x"}, headers=AGENT),
                api.post("/api/vm-presets/gold/describe", json={"description": "x"}, headers=AGENT),
                api.post("/api/vm-presets/gold/delete", json={"confirm": "gold"}, headers=AGENT),
                api.post("/api/vm-presets/gold/create", json={"id": "fresh"}, headers=AGENT),
                api.post("/api/vm-presets-default", json={"name": "gold"}, headers=AGENT),
            ]
            for call in calls:
                resp = await call
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert "preset-gold" in api.fake.tart and "fresh" not in api.fake.tart
    run(go())


def test_the_tart_list_does_not_offer_a_preset_copy_for_registration(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            rows = {r["name"]: r for r in (await (await api.get("/api/vm-tart")).json())["tart"]}
            assert rows["preset-gold"]["preset"] is True and rows["preset-gold"]["usable"] is False
            assert rows["src"]["usable"] is True
    run(go())


# ---- clone lineage -------------------------------------------------------------- #
def test_a_vm_made_from_a_preset_records_which_preset_and_a_registered_one_records_nothing(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    presets.save("src", "gold")
    vm = presets.create_vm("gold", "fresh")
    assert vm.cloned_from == {"kind": "preset", "preset": "gold", "preset_created_at": 1000.0, "at": vm.cloned_from["at"]}
    assert reg.get("src").cloned_from == {}                     # registered by hand: not guessed
    assert reg.view("fresh")["cloned_from"]["preset"] == "gold" and reg.view("src")["cloned_from"] == {}


def test_the_clone_record_is_saved_and_a_malformed_one_reads_as_no_record(tmp_path):
    saved = tmp_path / "vms.json"
    reg = Registry(saved)
    reg.add("fresh", cloned_from={"kind": "preset", "preset": "gold", "preset_created_at": 5, "at": 6})
    reg.add("bad", cloned_from={"kind": "preset", "preset": "a b"})
    reg.add("odd", cloned_from={"kind": "oci", "preset": "gold"})
    again = Registry(saved)
    again.load()
    assert again.get("fresh").cloned_from == {"kind": "preset", "preset": "gold", "preset_created_at": 5.0, "at": 6.0}
    assert again.get("bad").cloned_from == {} and again.get("odd").cloned_from == {}
    doc = json.loads(saved.read_text())
    doc["vms"][0]["cloned_from"] = "gold"                        # a hand edit
    saved.write_text(json.dumps(doc))
    third = Registry(saved)
    third.load()
    assert third.get("fresh").cloned_from == {}


def test_the_user_page_lists_the_presets_for_the_lineage_and_a_session_gets_no_lineage(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold", "description": "d"})
            await api.post("/api/vm-presets/gold/create", json={"id": "fresh"})
            page = await (await api.get("/api/vm")).json()
            assert [(p["name"], p["source"]) for p in page["presets"]] == [("gold", "src")]
            assert page["presets"][0]["created_at"] and "present" not in page["presets"][0]   # no tart query
            by_id = {v["id"]: v for v in page["vms"]}
            assert by_id["fresh"]["cloned_from"]["preset"] == "gold" and by_id["src"]["cloned_from"] == {}
            mine = await (await api.get("/api/vm?session=s1", headers=AGENT)).json()
            assert all("cloned_from" not in v for v in mine["vms"]) and "presets" not in mine
    run(go())


# ---- a preset's copy is not a VM -------------------------------------------------- #
def test_registering_a_preset_copy_is_refused_by_the_route_with_the_reason(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            for body in ({"id": "base", "tart_name": "preset-gold"}, {"id": "preset-gold"}):
                resp = await api.post("/api/vm", json=body)
                got = await resp.json()
                assert resp.status == 409 and got["code"] == "vm_preset_copy"
                assert "gold" in got["error"] and "프리셋 복사본은 VM으로 등록하지 않습니다" in got["error"]
                assert "preset new gold" in got["error"]
            assert [v.id for v in api.reg.list()] == ["src"]
            fine = await api.post("/api/vm", json={"id": "other", "tart_name": "preset-unrelated"})
            assert fine.status == 201                              # not a recorded copy: not this rule's business
    run(go())


def test_a_copy_registered_before_the_rule_is_marked_a_template_and_starting_it_needs_confirmation(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            api.reg.add("preset-gold", "preset-gold")             # as `vm add` did before the rule
            page = await (await api.get("/api/vm")).json()
            kind = {v["id"]: v["template"] for v in page["vms"]}
            assert kind == {"src": "", "preset-gold": "gold"}
            assert (await (await api.get("/api/vm/preset-gold")).json())["template"] == "gold"
            mine = await (await api.get("/api/vm?session=s1", headers=AGENT)).json()
            assert all("template" not in v for v in mine["vms"])
            refused = await api.post("/api/vm/preset-gold/start", json={})
            got = await refused.json()
            assert refused.status == 409 and got["code"] == "vm_template_confirm" and "gold" in got["error"]
            assert api.reg.get("preset-gold").state == "stopped" and not api.fake.spawned
            also = await api.post("/api/vm/preset-gold/start", json={"confirm_template": "yes"})
            assert also.status == 409                              # only a JSON true confirms
            ok = await api.post("/api/vm/preset-gold/start", json={"confirm_template": True})
            assert ok.status == 200 and (await ok.json())["state"] == "running"
            normal = await api.post("/api/vm/src/start", json={})
            assert normal.status == 200                            # an ordinary VM needs nothing
    run(go())


def test_granting_a_template_warns_and_it_can_still_be_forgotten(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            api.reg.add("preset-gold", "preset-gold")
            grant = await api.post("/api/vm/preset-gold/grant", json={"session": "s1", "scopes": ["view"]})
            got = await grant.json()
            assert grant.status == 200 and got["grants"] == ["view"] and "gold" in got["warning"]
            plain = await api.post("/api/vm/src/grant", json={"session": "s1", "scopes": ["view"]})
            assert "warning" not in await plain.json()
            gone = await api.delete("/api/vm/preset-gold")
            assert gone.status == 200 and "preset-gold" not in [v.id for v in api.reg.list()]
            again = await api.post("/api/vm", json={"id": "preset-gold"})
            assert again.status == 409                              # and it cannot be put back
    run(go())


def test_the_default_preset_is_set_listed_cleared_and_survives_a_reload(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            assert (await (await api.get("/api/vm-presets")).json())["default"] == ""
            bad = await api.post("/api/vm-presets-default", json={"name": "nope"})
            assert bad.status == 404 and (await bad.json())["code"] == "vm_unknown_preset"
            ok = await api.post("/api/vm-presets-default", json={"name": "gold"})
            assert ok.status == 200 and (await ok.json())["default"] == "gold"
            listed = await (await api.get("/api/vm-presets")).json()
            assert listed["default"] == "gold" and listed["presets"][0]["default"] is True
            # a second Presets over the same file reads it back
            again = vm_presets.Presets(tmp_path / "vm-presets.json", api.reg, api.presets.manager)
            assert again.default_info()["default"] == "gold"
            cleared = await api.post("/api/vm-presets-default", json={"name": ""})
            assert (await cleared.json()) == {"default": "", "default_lost": ""}
    run(go())


def test_deleting_the_default_preset_clears_it_and_says_so(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            await api.post("/api/vm-presets-default", json={"name": "gold"})
            await api.post("/api/vm-presets/gold/delete", json={"confirm": "gold"})
            listed = await (await api.get("/api/vm-presets")).json()
            assert listed["default"] == "" and "gold" in listed["default_lost"] and "삭제" in listed["default_lost"]
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold2"})
            await api.post("/api/vm-presets-default", json={"name": "gold2"})
            assert (await (await api.get("/api/vm-presets")).json())["default_lost"] == ""
    run(go())


def test_a_default_whose_preset_vanished_from_the_file_is_cleared_on_load(tmp_path):
    fake, reg, mgr, presets, path = build(tmp_path)
    presets.save("src", "gold")
    presets.set_default("gold")
    doc = json.loads(path.read_text())
    doc["presets"] = []
    path.write_text(json.dumps(doc))
    again = vm_presets.Presets(path, reg, mgr)
    info = again.default_info()
    assert info["default"] == "" and "gold" in info["default_lost"]


def test_the_session_panel_flow_runs_on_user_only_routes_with_a_fake_manager(tmp_path):
    """One click = preset create then grant (view+control); disconnect = revoke that session only."""
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            await api.post("/api/vm-presets-default", json={"name": "gold"})
            # a session cannot make, connect or disconnect a VM
            for call in (api.post("/api/vm-presets/gold/create", json={"id": "s1-vm"}, headers=AGENT),
                         api.post("/api/vm/src/grant", json={"session": "s1", "scopes": ["view"]}, headers=AGENT),
                         api.post("/api/vm/src/revoke", json={"session": "s1"}, headers=AGENT)):
                resp = await call
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert "s1-vm" not in api.fake.tart
            made = await api.post("/api/vm-presets/gold/create", json={"id": "s1-vm"})
            assert made.status == 201
            granted = await api.post("/api/vm/s1-vm/grant", json={"session": "s1", "scopes": ["view", "control"]})
            assert (await granted.json())["grants"] == ["view", "control"]
            await api.post("/api/vm/s1-vm/grant", json={"session": "s2", "scopes": ["view"]})
            await api.post("/api/vm/s1-vm/revoke", json={"session": "s1"})
            vm = [v for v in (await (await api.get("/api/vm")).json())["vms"] if v["id"] == "s1-vm"][0]
            assert vm["grants"] == {"s2": ["view"]} and vm["state"] != "destroyed"
            # the create asks tart for a clone and a start, never a delete of the VM
            assert not deletes(api.fake)
    run(go())
