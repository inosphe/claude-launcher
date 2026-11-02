"""The share catalog (vm-setup-8s6, s2-vmshare2): shared folders for all VMs or listed
VMs, merged with a VM's own at start, with where each came from."""
from __future__ import annotations

import json
import os

import pytest

from claude_launcher.daemon import actors, vm_share
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_share import AGENT, S2, Host, api_for, code_of, manager, req, run  # noqa: F401


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def entry(host, name, sub, mode="ro", scope="all", vms=()):
    body = {"name": name, "host_path": str(host.root / sub), "mode": mode, "scope": scope, "vms": list(vms)}
    return vm_share.check_catalog_entry(body, host.roots, host.forbidden, ["a", "b"])


def test_an_entry_follows_the_share_rules_and_a_scope(host):
    got = entry(host, "src", "proj")
    assert got == {"name": "src", "host_path": os.path.realpath(host.root / "proj"), "mode": "ro",
                   "scope": "all", "vms": []}
    assert entry(host, "o", "other", scope="vms", vms=["b", "a", "a"])["vms"] == ["a", "b"]
    for body in ({"scope": "some"}, {"scope": "vms"}, {"scope": "vms", "vms": []}, {"scope": "vms", "vms": ["zz"]},
                 {"scope": "vms", "vms": "a"}):
        with pytest.raises(VmError):
            vm_share.check_catalog_entry({"name": "x", "host_path": str(host.root), **body}, host.roots,
                                         host.forbidden, ["a"])
    assert code_of(vm_share.check_catalog_entry, {"name": "x", "host_path": str(host.outside)}, host.roots,
                   host.forbidden, []) == "vm_share_outside_root"
    assert code_of(vm_share.check_catalog_entry, {"name": "x", "host_path": str(host.home / ".ssh")},
                   [os.path.realpath(host.home)], host.forbidden, []) == "vm_share_forbidden"


def test_the_effective_shares_are_own_first_then_the_catalog_that_applies(tmp_path, host):
    reg = Registry()
    reg.add("a")
    reg.add("b")
    reg.share_add("a", host.check("own", host.root / "proj"))
    reg.catalog_put(entry(host, "everyone", "other"))
    reg.catalog_put(entry(host, "only-b", "proj", scope="vms", vms=["b"]))
    names = lambda v: [s["name"] for s in reg.effective_shares(v)[0]]
    assert names("a") == ["own", "everyone"] and names("b") == ["everyone", "only-b"]
    kinds = {s["name"]: s["origin"]["kind"] for s in reg.effective_shares("a")[0]}
    assert kinds == {"own": "vm", "everyone": "catalog-all"}
    assert {s["name"]: s["origin"]["kind"] for s in reg.effective_shares("b")[0]}["only-b"] == "catalog-vms"


def test_a_name_clash_keeps_the_vms_own_share_and_the_limit_counts_both(tmp_path, host):
    reg = Registry()
    reg.add("a")
    reg.share_add("a", host.check("src", host.root / "proj"))
    reg.catalog_put(entry(host, "src", "other"))
    shares, left_out = reg.effective_shares("a")
    assert [s["host_path"] for s in shares] == [os.path.realpath(host.root / "proj")]
    assert "name_conflict" in left_out["src"]
    reg2 = Registry()
    reg2.add("a")
    for i in range(3):
        (host.root / f"d{i}").mkdir()
        reg2.share_add("a", host.check(f"d{i}", host.root / f"d{i}"))
    reg2.catalog_put(entry(host, "c1", "proj"))
    reg2.catalog_put(entry(host, "c2", "other"))
    shares, left_out = reg2.effective_shares("a")
    assert [s["name"] for s in shares] == ["d0", "d1", "d2", "c1"] and "over_limit" in left_out["c2"]


def test_the_catalog_is_saved_restored_and_a_removed_vm_leaves_its_entries(tmp_path, host):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    reg.add("b")
    reg.catalog_put(entry(host, "one", "proj"))
    reg.catalog_put(entry(host, "two", "other", mode="rw", scope="vms", vms=["a", "b"]))
    doc = json.loads(path.read_text())
    assert [e["name"] for e in doc["share_catalog"]] == ["one", "two"]
    again = Registry(path)
    again.load()
    assert again.catalog == reg.catalog and again.catalog[1]["origin"]["kind"] == "catalog-vms"
    reg.catalog_put(entry(host, "one", "other"), replace=True)                   # change
    assert json.loads(path.read_text())["share_catalog"][0]["host_path"] == os.path.realpath(host.root / "other")
    reg.catalog_remove("one")
    assert [e["name"] for e in json.loads(path.read_text())["share_catalog"]] == ["two"]
    reg.remove("b")                                                              # b is stopped
    assert json.loads(path.read_text())["share_catalog"][0]["vms"] == ["a"]
    reg.catalog_remove("two")
    assert "share_catalog" not in json.loads(path.read_text())
    fresh = Registry(path)
    fresh.load()
    assert fresh.catalog == []


def test_names_are_unique_and_changes_need_an_existing_entry(host):
    reg = Registry()
    reg.add("a")
    reg.catalog_put(entry(host, "one", "proj"))
    assert code_of(reg.catalog_put, entry(host, "one", "other")) == "vm_bad_request"
    assert code_of(reg.catalog_put, entry(host, "nope", "other"), True) == "vm_bad_request"
    assert code_of(reg.catalog_remove, "nope") == "vm_bad_request"
    for i in range(vm_share.CATALOG_MAX - 1):
        reg.catalog_put({**entry(host, "one", "proj"), "name": f"n{i}"})
    assert code_of(reg.catalog_put, {**entry(host, "one", "proj"), "name": "over"}) == "vm_bad_request"


def test_a_hand_edited_catalog_keeps_only_well_formed_entries(tmp_path):
    path = tmp_path / "vms.json"
    ok = {"name": "one", "host_path": "/p/q", "mode": "ro", "scope": "all"}
    path.write_text(json.dumps({"vms": [], "share_catalog": [
        ok, {**ok}, {**ok, "name": "bad name"}, {**ok, "name": "m", "mode": "x"}, {**ok, "name": "s", "scope": "some"},
        {**ok, "name": "v", "scope": "vms", "vms": "a"}, {**ok, "name": "w", "scope": "vms", "vms": ["a"]},
        "text", None, {"origin": 5}]}))
    reg = Registry(path)
    reg.load()
    assert [e["name"] for e in reg.catalog] == ["one", "w"]
    assert reg.catalog[0]["origin"]["kind"] == "catalog-all" and reg.catalog[1]["vms"] == ["a"]


def test_start_mounts_own_and_catalog_shares_and_pending_follows_the_catalog(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.share_add("a", host.check("own", host.root / "proj"))
    reg.catalog_put(entry(host, "cat", "other", mode="rw"))
    vm = mgr.start("a")
    argv = fake.spawned[-1]
    real = os.path.realpath
    assert argv.count("--dir") == 2
    assert f"own:{real(host.root / 'proj')}:ro" in argv and f"cat:{real(host.root / 'other')}" in argv
    assert not vm.shares_pending(reg.effective_shares("a")[0]) and reg.view("a")["shares_pending"] is False
    reg.catalog_put(entry(host, "later", "proj"))
    assert reg.view("a")["shares_pending"] is True
    rows = {r["name"]: r["state"] for r in reg.view("a")["share_rows"]}
    assert rows == {"own": "applied", "cat": "applied", "later": "pending"}
    reg.catalog_remove("later")
    assert reg.view("a")["shares_pending"] is False
    reg.catalog_remove("cat")
    assert reg.view("a")["shares_pending"] is True                               # something mounted is gone


def test_a_catalog_entry_that_fails_the_rules_at_start_is_left_out_with_the_reason(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.catalog_put(entry(host, "good", "proj"))
    reg.catalog_put(entry(host, "moved", "other"))
    os.rename(host.root / "other", host.root / "other.old")
    os.symlink(host.outside, host.root / "other")
    vm = mgr.start("a")
    assert fake.spawned[-1].count("--dir") == 1
    assert "vm_share_outside_root" in vm.shares_skipped["moved"]
    rows = {r["name"]: r for r in reg.view("a")["share_rows"]}
    assert rows["good"]["state"] == "applied" and rows["moved"]["state"] == "left_out" and "why" in rows["moved"]


def test_a_session_is_told_only_the_shares_that_are_mounted(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.catalog_put(entry(host, "cat", "other"))
    reg.catalog_put(entry(host, "clash", "proj"))
    reg.share_add("a", host.check("clash", host.root / "other"))
    assert sorted(r["name"] for r in reg.session_shares("a")) == ["cat", "clash"]
    row = reg.session_shares("a")[0]
    assert row["guest_path"].startswith("/Volumes/My Shared Files/") and row["origin"]["kind"] in ("vm", "catalog-all")


def test_requests_that_were_approved_keep_their_shares_when_the_session_ends(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=("out", str(host.root / "other"), "ro"),
                                                         scopes=("files",)), headers=S2)
            rid = api.reg.request_get("a", "s2")["id"]
            await api.post("/api/vm/a/approve", json={"session": "s2", "id": rid})
            row = api.reg.get("a").shared_dirs[0]
            assert row["origin"]["kind"] == "request" and row["origin"]["by_session"] == "s2" and row["origin"]["at"] > 0
            # a pending request goes with the session; an approved share stays
            await api.post("/api/vm/b/request", json=req(share=None, scopes=("files",)), headers=S2)
            api.reg.session_exited("s2")
            assert api.reg.request_list("b") == [] and api.reg.get("a").shared_dirs[0]["name"] == "out"
            saved = json.loads(api.reg.path.read_text()) if api.reg.path else None
            assert saved is None or saved["vms"][0]["shared_dirs"][0]["origin"]["by_session"] == "s2"
    run(go())


def test_the_catalog_routes_are_the_users_and_rw_for_all_vms_needs_a_confirmation(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            body = {"action": "put", "name": "cat", "host_path": str(host.root / "proj"), "scope": "all"}
            for hdr in (AGENT, S2):
                resp = await api.post("/api/vm-share-catalog", json=body, headers=hdr)
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
                assert (await api.get("/api/vm-share-catalog", headers=hdr)).status == 403
            assert api.reg.catalog == []
            ok = await api.post("/api/vm-share-catalog", json=body)
            got = await ok.json()
            assert ok.status == 200 and got["share_catalog"][0]["origin"]["kind"] == "catalog-all"
            assert got["share_roots"] == [os.path.realpath(host.root)]
            rw = {**body, "name": "rwall", "mode": "rw"}
            refused = await api.post("/api/vm-share-catalog", json=rw)
            assert refused.status == 400 and (await refused.json())["code"] == "vm_share_confirm_rw"
            assert [e["name"] for e in api.reg.catalog] == ["cat"]
            ok = await api.post("/api/vm-share-catalog", json={**rw, "confirm_rw": True})
            assert ok.status == 200
            # rw for listed VMs needs no confirmation
            ok = await api.post("/api/vm-share-catalog", json={**rw, "name": "rwvm", "scope": "vms", "vms": ["a"]})
            assert ok.status == 200
            # change and remove
            ch = await api.post("/api/vm-share-catalog", json={**body, "mode": "rw", "replace": True})
            assert ch.status == 400                                          # rw for all, no confirmation
            ch = await api.post("/api/vm-share-catalog", json={**body, "replace": True, "scope": "vms", "vms": ["b"]})
            assert ch.status == 200 and next(e for e in api.reg.catalog if e["name"] == "cat")["vms"] == ["b"]
            rm = await api.post("/api/vm-share-catalog", json={"action": "rm", "name": "cat"}, headers=AGENT)
            assert rm.status == 403
            rm = await api.post("/api/vm-share-catalog", json={"action": "rm", "name": "cat"})
            assert rm.status == 200 and all(e["name"] != "cat" for e in api.reg.catalog)
            assert (await api.post("/api/vm-share-catalog", json={"action": "move"})).status == 400
            whats = [r.get("what") for r in api.reg.audit_log]
            assert "catalog_put" in whats and "catalog_rm" in whats
            bad = await api.post("/api/vm-share-catalog", json={**body, "name": "e", "host_path": str(host.outside)})
            assert bad.status == 400 and (await bad.json())["code"] == "vm_share_outside_root"
    run(go())


def test_the_user_view_lists_the_catalog_and_rows_and_a_session_sees_neither(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.reg.catalog_put(entry(host, "cat", "proj"))
            api.reg.share_add("a", host.check("own", host.root / "other"))
            api.reg.grant("a", "s1", ["view"])
            user = await (await api.get("/api/vm")).json()
            assert user["share_catalog"][0]["name"] == "cat" and user["share_catalog"][0]["scope"] == "all"
            a = next(v for v in user["vms"] if v["id"] == "a")
            assert {r["name"]: r["origin"]["kind"] for r in a["share_rows"]} == {"own": "vm", "cat": "catalog-all"}
            listed = await (await api.get("/api/vm?session=s1", headers=AGENT)).json()
            assert "share_catalog" not in listed
            row = listed["vms"][0]
            assert "share_rows" not in row and sorted(s["name"] for s in row["shared_dirs"]) == ["cat", "own"]
            assert all(set(s) == {"name", "host_path", "mode", "guest_path"} for s in row["shared_dirs"])
            assert "편집은 호스트 경로" in row["share_guidance"]
    run(go())
