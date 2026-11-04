"""Deleting a VM's tart disk for good, and the difference from unregistering it
(vm-setup-8s6, s2-vmdelete). tart is the fake runner: nothing real is ever deleted."""
from __future__ import annotations

import asyncio

import pytest

from claude_launcher.daemon import actors

from test_vm_presets import AGENT, api_for, deletes, run  # noqa: F401

# The VM routes answer vm_disabled until the feature is switched on.
pytestmark = pytest.mark.usefixtures("vm_on")


def two_vms(api):
    api.reg.add("other")
    api.fake.tart["other"] = "stopped"
    api.fake.sizes["other"] = 12


async def destroy(api, vm="src", **body):
    return await api.post(f"/api/vm/{vm}/destroy", json={"confirm": vm, **body})


def test_only_the_user_can_look_or_delete_and_nothing_goes_without_the_id_typed(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            for hdr in (AGENT, {actors.HEADER: "cflow:wf"}):
                for resp in (await api.get("/api/vm/src/destroy", headers=hdr),
                             await api.post("/api/vm/src/destroy", json={"confirm": "src"}, headers=hdr)):
                    assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            for body in ({}, {"confirm": "other"}, {"confirm": ""}, {"confirm": True}):
                resp = await api.post("/api/vm/src/destroy", json=body)
                assert resp.status == 400 and (await resp.json())["code"] == "vm_confirm"
            assert not deletes(api.fake) and "src" in [v.id for v in api.reg.list()]
    run(go())


def test_the_preview_reports_size_and_nothing_is_changed(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            plan = await (await api.get("/api/vm/src/destroy")).json()
            assert plan["id"] == "src" and plan["tart_name"] == "src" and plan["size_gb"] == 93
            assert plan["blockers"] == [] and plan["warnings"] == [] and plan["tart_present"] is True
            assert not deletes(api.fake) and api.fake.tart["src"] == "stopped"
    run(go())


def test_a_delete_removes_the_tart_vm_and_the_records_and_is_audited(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            two_vms(api)
            api.reg.catalog_put({"name": "c", "host_path": "/p", "mode": "ro", "scope": "vms", "vms": ["src", "other"],
                                 "origin": {"kind": "catalog-vms", "by_session": "", "at": 1}})
            api.reg.request_add("src", "s2", ["view"], [], "x")
            resp = await destroy(api)
            got = await resp.json()
            assert resp.status == 200 and got["destroyed"] == "src" and got["size_gb"] == 93
            assert deletes(api.fake) == [["tart", "delete", "--", "src"]]
            assert "src" not in [v.id for v in api.reg.list()] and "other" in [v.id for v in api.reg.list()]
            assert api.reg.catalog[0]["vms"] == ["other"] and api.reg.request_list("src") == []
            rows = [r for r in api.reg.audit_log if r.get("what") in ("destroy", "destroyed")]
            assert [r["what"] for r in rows] == ["destroy", "destroyed"]
            assert rows[0]["vm"] == "src" and rows[0]["tart_name"] == "src" and rows[0]["size_gb"] == 93
            assert rows[0]["actor"] and rows[0]["actor"] != "unknown" and rows[0].get("at")
            gone = await api.get("/api/vm/src/destroy")
            assert gone.status == 404
    run(go())


@pytest.mark.parametrize("setup,code,status", [
    ("running", "vm_bad_state", 409), ("suspended", "vm_bad_state", 409), ("fault", "vm_bad_state", 409),
    ("locked", "vm_locked", 423), ("granted", "vm_destroy_granted", 409), ("shared", "vm_held", 409),
    ("tart_running", "vm_bad_state", 409)])
def test_a_vm_that_is_not_stopped_unlocked_and_unheld_is_refused_and_untouched(tmp_path, setup, code, status):
    async def go():
        async with api_for(tmp_path) as api:
            await asyncio.sleep(0.2)            # the daemon's startup reconcile has run
            vm = api.reg.get("src")
            if setup in ("running", "suspended", "fault"):
                vm.state = setup
            elif setup == "locked":
                vm.locked = True
            elif setup == "granted":
                api.reg.grant("src", "s1", ["view"])
            elif setup == "shared":
                api.reg.add("twin", "src")
            elif setup == "tart_running":
                api.fake.tart["src"] = "running"
            plan = await (await api.get("/api/vm/src/destroy")).json()
            assert plan["blockers"] and plan["blockers"][0]["code"] == code
            resp = await destroy(api)
            got = await resp.json()
            assert resp.status == status and got["code"] == code and got["message"]
            assert not deletes(api.fake) and "src" in [v.id for v in api.reg.list()]
    run(go())


def test_presets_descendants_and_a_template_are_warned_about_not_blocked(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm-presets", json={"vm": "src", "name": "gold"})
            await api.post("/api/vm-presets/gold/create", json={"id": "kid"})
            plan = await (await api.get("/api/vm/src/destroy")).json()
            assert plan["presets"] == ["gold"] and plan["descendants"] == ["kid"] and plan["blockers"] == []
            text = " ".join(plan["warnings"])
            assert "gold" in text and "kid" in text
            api.reg.add("preset-gold", "preset-gold")                        # the template, registered earlier
            plan = await (await api.get("/api/vm/preset-gold/destroy")).json()
            assert plan["template"] == "gold" and "프리셋 템플릿" in " ".join(plan["warnings"])
            assert plan["blockers"] == []
            resp = await destroy(api, "src")
            assert resp.status == 200 and ["tart", "delete", "--", "src"] in deletes(api.fake)
            assert api.reg.get("kid").cloned_from["preset"] == "gold"        # the descendant is left alone
    run(go())


def test_when_tart_fails_the_registration_stays_and_the_failure_is_audited(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.fake.delete_rc = 1
            resp = await destroy(api)
            got = await resp.json()
            assert resp.status >= 400 and got["code"] == "tart_failed"
            assert "src" in [v.id for v in api.reg.list()] and api.reg.get("src").state == "stopped"
            assert [r["what"] for r in api.reg.audit_log if r["what"].startswith("destroy")] == ["destroy", "destroy_failed"]
            assert "vm_destroying" not in api.server.app or not api.server.app["vm_destroying"]
            api.fake.delete_rc = 0
            assert (await destroy(api)).status == 200                          # and it can be tried again
    run(go())


def test_a_vm_tart_no_longer_lists_loses_its_record_without_a_delete_call(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            del api.fake.tart["src"]
            plan = await (await api.get("/api/vm/src/destroy")).json()
            assert plan["tart_present"] is False and "tart에" in " ".join(plan["warnings"])
            assert (await destroy(api)).status == 200
            assert not deletes(api.fake) and "src" not in [v.id for v in api.reg.list()]
    run(go())


def test_unregistering_keeps_the_tart_vm_and_says_so(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            resp = await api.delete("/api/vm/src")
            got = await resp.json()
            assert resp.status == 200 and got["unregistered"] == "src" and got["tart_kept"] == "src"
            assert not deletes(api.fake) and api.fake.tart["src"] == "stopped"
            assert any(r["what"] == "unregister" for r in api.reg.audit_log)
            denied = await api.delete("/api/vm/other", headers=AGENT)
            assert denied.status in (403, 404)
    run(go())


def test_a_start_is_refused_while_the_delete_is_running(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.server.app["vm_destroying"] = {"src"}
            resp = await api.post("/api/vm/src/start", json={})
            assert resp.status == 409 and (await resp.json())["code"] == "vm_busy_state"
            assert not api.fake.spawned
            plan = await (await api.get("/api/vm/src/destroy")).json()
            assert plan["blockers"][0]["code"] == "vm_busy_state"
    run(go())
