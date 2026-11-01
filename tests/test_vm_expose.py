"""Per-VM port exposure and allowed ranges (vm-setup-8s6): what tart run is told,
what is refused, and who may change it."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient

from claude_launcher.daemon import actors, vm_tart, vmhub
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_presets import api_for, build, clones
from test_vm_tart import Fake

HOST = ["192.168.1.89", "127.0.0.1"]
AGENT = {actors.HEADER: "agent:s1"}


def run(coro):
    return asyncio.run(coro)


# ---- the validators ------------------------------------------------------------ #
@pytest.mark.parametrize("ext,inner,ok", [
    (1024, 22, True), ("8080", "80", True), (65535, 65535, True), (" 2222 ", 22, True),
    (1023, 22, False), (80, 80, False), (65536, 22, False), (2222, 0, False), (2222, 65536, False),
    ("22 22", 22, False), ("０８０８０", 22, False), (True, 22, False), (None, 22, False), (2222.5, 22, False),
    ("-1", 22, False), ("", 22, False), ("2222", "", False),
])
def test_expose_ports(ext, inner, ok):
    if ok:
        assert vmhub.check_expose(ext, inner) == f"{int(str(ext).strip())}:{int(str(inner).strip())}"
    else:
        with pytest.raises(VmError):
            vmhub.check_expose(ext, inner)


@pytest.mark.parametrize("value,expect", [
    ("192.168.1.213", "192.168.1.213/32"),      # the nginx host: not the host's own address
    ("192.168.1.213/32", "192.168.1.213/32"),
    ("10.0.0.0/9", "10.0.0.0/9"),                # inside a private block, narrower than it
    ("1.2.3.0/24", "1.2.3.0/24"),
    ("0.0.0.0/0", "too_wide"), ("0.0.0.0/1", "too_wide"), ("8.0.0.0/7", "too_wide"),
    ("10.0.0.0/8", "too_wide"), ("172.16.0.0/12", "too_wide"), ("192.168.0.0/16", "too_wide"),
    ("172.0.0.0/8", "too_wide"), ("192.168.0.0/15", "too_wide"), ("128.0.0.0/4", "too_wide"),
    ("192.168.1.0/24", "holds_host"), ("192.168.1.88/29", "holds_host"), ("127.0.0.0/8", "holds_host"),
    ("::1", "bad"), ("2001:db8::/32", "bad"), ("abc", "bad"), ("", "bad"), ("1.2.3.4/33", "bad"),
])
def test_allow_ranges(value, expect):
    if expect in ("too_wide", "holds_host", "bad"):
        with pytest.raises(VmError) as err:
            vmhub.check_allow(value, HOST)
        assert err.value.code == {"too_wide": "vm_allow_too_wide", "holds_host": "vm_allow_holds_host",
                                  "bad": "vm_bad_request"}[expect]
    else:
        assert vmhub.check_allow(value, HOST) == expect


def test_the_hosts_addresses_are_read_from_ifconfig():
    assert vm_tart.host_ipv4s(Fake().run) == ["127.0.0.1", "192.168.1.89"]
    assert vm_tart.host_ipv4s(lambda a, t: (1, "", "no")) == []


def test_refusals_are_written_for_the_user_in_korean():
    for value in ("0.0.0.0/0", "192.168.1.0/24", "abc"):
        with pytest.raises(VmError) as err:
            vmhub.check_allow(value, HOST)
        assert "허용 주소" in str(err.value)


# ---- the registry -------------------------------------------------------------- #
def test_expose_is_unique_across_vms_limited_and_saved(tmp_path):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    reg.add("b")
    reg.expose_add("a", 2222, 22)
    with pytest.raises(VmError) as err:
        reg.expose_add("b", 2222, 80)
    assert err.value.code == "vm_port_taken"
    with pytest.raises(VmError):
        reg.expose_add("a", 2222, 22)
    for n in range(vmhub.EXPOSE_MAX - 1):
        reg.expose_add("a", 3000 + n, 80)
    with pytest.raises(VmError):
        reg.expose_add("a", 4000, 80)
    reg.allow_add("a", "192.168.1.213", HOST)
    with pytest.raises(VmError):
        reg.allow_add("a", "192.168.1.213/32", HOST)
    again = Registry(path)
    again.load()
    assert again.get("a").expose[0] == "2222:22" and again.get("a").allow == ["192.168.1.213/32"]
    reg.expose_remove("a", 2222)
    reg.allow_remove("a", "192.168.1.213")
    assert "2222:22" not in reg.get("a").expose and reg.get("a").allow == []
    with pytest.raises(VmError):
        reg.expose_remove("a", 2222)


def test_a_hand_edited_file_does_not_lift_the_isolation(tmp_path):
    path = tmp_path / "vms.json"
    path.write_text(json.dumps({"vms": [
        {"id": "a", "expose": ["2222:22", "80:80", "x", "2222:22", "3000:99999"],
         "allow": ["0.0.0.0/0", "10.0.0.0/8", "192.168.1.213/32", "junk", 5, "::/0"]},
        {"id": "b", "expose": ["2222:22", "2223:22"], "allow": "0.0.0.0/0"},
    ]}))
    reg = Registry(path)
    reg.load()
    assert reg.get("a").expose == ["2222:22"] and reg.get("a").allow == ["192.168.1.213/32"]
    assert reg.get("b").expose == ["2223:22"] and reg.get("b").allow == []


def test_the_view_says_when_the_running_vm_has_other_settings(tmp_path):
    reg = Registry()
    reg.add("a")
    assert reg.view("a")["net_pending"] is False
    reg.claim_slot("a")
    reg.set_state("a", "running")
    assert reg.view("a")["net_pending"] is False and reg.view("a")["net_applied_known"] is False
    reg.expose_add("a", 2222, 22)
    assert reg.view("a")["net_pending"] is True      # unknown what it was started with
    reg.get("a").net_applied = {"expose": ["2222:22"], "allow": []}
    assert reg.view("a")["net_pending"] is False and reg.view("a")["net_applied_known"] is True
    reg.allow_add("a", "1.2.3.4")
    assert reg.view("a")["net_pending"] is True


# ---- tart run ------------------------------------------------------------------- #
def test_run_argv_adds_the_lists_only_with_softnet(tmp_path):
    reg = Registry()
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=Fake().run, spawn=Fake().spawn)
    on = mgr.run_argv("a", True, ["2222:22", "8080:80"], ["192.168.1.213/32"])
    assert on[:3] == ["tart", "run", "--suspendable"]
    assert "--net-softnet-allow=192.168.1.213/32" in on and "--net-softnet-expose=2222:22,8080:80" in on
    assert on.index("--net-softnet") < on.index("--net-softnet-allow=192.168.1.213/32")
    assert on[-2:] == ["--", "a"]
    off = vm_tart.Manager(reg, log_dir=tmp_path, run=Fake().run, spawn=Fake().spawn, softnet=False)
    assert not [a for a in off.run_argv("a", None, ["2222:22"], ["1.2.3.4/32"]) if "softnet" in a]
    assert not [a for a in mgr.run_argv("a", True) if a.startswith(("--net-softnet-allow", "--net-softnet-expose"))]


def _mgr(tmp_path, host):
    fake = Fake()
    reg = Registry()
    reg.add("a")
    fake.tart = {"a": "stopped"}
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5, locked=lambda: False,
                          host_addrs=lambda: host)
    return fake, reg, mgr


def test_start_passes_the_settings_and_remembers_them(tmp_path):
    fake, reg, mgr = _mgr(tmp_path, HOST)
    reg.expose_add("a", 2222, 22)
    reg.allow_add("a", "192.168.1.213/32", HOST)
    mgr.start("a")
    spawned = fake.spawned[-1]
    assert "--net-softnet-expose=2222:22" in spawned and "--net-softnet-allow=192.168.1.213/32" in spawned
    assert reg.get("a").net_applied == {"expose": ["2222:22"], "allow": ["192.168.1.213/32"]}
    assert reg.view("a")["net_pending"] is False


def test_start_checks_the_host_addresses_again_and_changes_nothing_when_it_refuses(tmp_path):
    fake, reg, mgr = _mgr(tmp_path, ["10.1.2.3"])
    reg.allow_add("a", "192.168.1.0/24", ["10.1.2.3"])   # fine when it was saved
    mgr._host_addrs = lambda: ["192.168.1.89"]            # the host moved into that range
    with pytest.raises(VmError) as err:
        mgr.start("a")
    assert err.value.code == "vm_allow_holds_host"
    assert reg.get("a").state == "stopped" and not fake.spawned


def test_a_start_that_fails_reports_the_failure_and_keeps_the_saved_settings(tmp_path):
    fake, reg, mgr = _mgr(tmp_path, HOST)
    reg.expose_add("a", 2222, 22)
    (tmp_path / "a").mkdir()
    fake.agent_ok = False
    with pytest.raises(VmError) as err:
        mgr.start("a")
    assert err.value.code in ("vm_boot_timeout", "tart_exited")
    assert reg.get("a").expose == ["2222:22"]


def test_a_vm_from_a_preset_starts_without_the_sources_lists(tmp_path):
    fake, reg, _, presets, _ = build(tmp_path)
    reg.expose_add("src", 2222, 22)
    reg.allow_add("src", "192.168.1.213", HOST)
    presets.save("src", "gold")
    fresh = presets.create_vm("gold", "fresh")
    assert fresh.expose == [] and fresh.allow == []


# ---- the routes ----------------------------------------------------------------- #
def test_the_routes_add_list_and_remove(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.reg.get("src")
            ok = await api.post("/api/vm/src/expose", json={"action": "add", "external": 2222, "internal": 22})
            body = await ok.json()
            assert ok.status == 200 and body["expose"] == ["2222:22"]
            ok = await api.post("/api/vm/src/allow", json={"action": "add", "cidr": "192.168.1.213"})
            assert (await ok.json())["allow"] == ["192.168.1.213/32"]
            bad = await api.post("/api/vm/src/expose", json={"action": "add", "external": 80, "internal": 80})
            assert bad.status == 400 and "외부 포트" in (await bad.json())["error"]
            for cidr in ("0.0.0.0/0", "10.0.0.0/8", "192.168.0.0/16"):
                wide = await api.post("/api/vm/src/allow", json={"action": "add", "cidr": cidr})
                assert wide.status == 400 and (await wide.json())["code"] == "vm_allow_too_wide"
            dup = await api.post("/api/vm/src/expose", json={"action": "add", "external": 2222, "internal": 80})
            assert dup.status == 409 and (await dup.json())["code"] == "vm_port_taken"
            odd = await api.post("/api/vm/src/expose", json={"action": "boom"})
            assert odd.status == 400
            gone = await api.post("/api/vm/src/expose", json={"action": "rm", "external": 2222})
            assert (await gone.json())["expose"] == []
            gone = await api.post("/api/vm/src/allow", json={"action": "rm", "cidr": "192.168.1.213"})
            assert (await gone.json())["allow"] == []
    run(go())


def test_a_range_holding_this_hosts_address_is_refused_by_the_route(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            app = api.server.app
            app["vm_manager"]._host_addrs = lambda: ["192.168.1.89"]
            no = await api.post("/api/vm/src/allow", json={"action": "add", "cidr": "192.168.1.0/24"})
            assert no.status == 400 and (await no.json())["code"] == "vm_allow_holds_host"
            yes = await api.post("/api/vm/src/allow", json={"action": "add", "cidr": "192.168.1.213/32"})
            assert yes.status == 200
    run(go())


def test_a_session_can_neither_change_nor_see_the_settings(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            await api.post("/api/vm/src/expose", json={"action": "add", "external": 2222, "internal": 22})
            api.reg.grant("src", "s1", ["view", "control"])
            for path, body in (("/api/vm/src/expose", {"action": "add", "external": 2223, "internal": 22}),
                               ("/api/vm/src/allow", {"action": "add", "cidr": "1.2.3.4"})):
                resp = await api.post(path, json=body, headers=AGENT)
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            row = await (await api.get("/api/vm/src?session=s1", headers=AGENT)).json()
            assert "expose" not in row and "allow" not in row and "net_pending" not in row
            listed = (await (await api.get("/api/vm?session=s1", headers=AGENT)).json())["vms"][0]
            assert "expose" not in listed and "allow" not in listed
            assert api.reg.get("src").expose == ["2222:22"]
            user = await (await api.get("/api/vm/src")).json()
            assert user["expose"] == ["2222:22"]
    run(go())


def _reloaded(path):
    again = Registry(path)
    again.load()
    return again


def test_every_network_and_lock_change_is_saved_and_returns_the_record(tmp_path):
    """Each setter saves before it returns and hands back the VM. The earlier
    tests read the in-memory record, and a route answers from ``view()``, so a
    setter that forgot ``_save()`` (or its ``return``) went unnoticed until a
    rebase left ``allow_remove`` without them."""
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    steps = [
        (lambda: reg.expose_add("a", 2222, 22), lambda r: r.get("a").expose == ["2222:22"]),
        (lambda: reg.allow_add("a", "192.168.1.213", HOST), lambda r: r.get("a").allow == ["192.168.1.213/32"]),
        (lambda: reg.expose_remove("a", 2222), lambda r: r.get("a").expose == []),
        (lambda: reg.allow_remove("a", "192.168.1.213"), lambda r: r.get("a").allow == []),
        (lambda: reg.set_locked("a", True), lambda r: r.get("a").locked is True),
        (lambda: reg.set_locked("a", False), lambda r: r.get("a").locked is False),
    ]
    for change, saved in steps:
        got = change()
        assert got is reg.get("a"), change
        assert saved(_reloaded(path)), change
