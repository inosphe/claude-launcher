"""Shared host folders (vm-setup-8s6, s2-vmshare): ``tart run --dir``, the allowed
roots and the never-shared list, the user-only settings, what a session is told,
and the one-step access request. A fake tart; no VM is started."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import actors, vm_api, vm_channel, vm_frames, vm_presets, vm_share, vm_tart, vmhub
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_channel import World
from test_vm_frames import Capture
from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}
S2 = {actors.HEADER: "agent:s2"}


class Host:
    """A made-up home folder: the places a share may and may not come from."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.state = self.home / ".claude-launcher" / "daemon"
        for sub in (".ssh", ".gnupg", ".aws", "Library/Keychains", ".claude", ".claude-launcher/daemon", "works/proj/src",
                    "works/other", "elsewhere"):
            (self.home / sub).mkdir(parents=True)
        self.root = self.home / "works"
        self.outside = self.home / "elsewhere"
        self.forbidden = vm_share.forbidden_paths(self.home, self.state)

    @property
    def roots(self):
        return vm_share.clean_roots([str(self.root)], self.forbidden)

    def check(self, name, path, mode=None):
        return vm_share.check_share(name, str(path), mode, self.roots, self.forbidden)


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def code_of(fn, *a, **kw):
    with pytest.raises(VmError) as err:
        fn(*a, **kw)
    return err.value.code


# ---- the rules ---------------------------------------------------------------------- #
def test_a_share_is_read_only_unless_the_user_wrote_rw(host):
    got = host.check("src", host.root / "proj")
    assert got == {"name": "src", "host_path": os.path.realpath(host.root / "proj"), "mode": "ro"}
    assert host.check("src", host.root / "proj", "rw")["mode"] == "rw"
    assert host.check("src", host.root / "proj", "")["mode"] == "ro"
    for bad in ("RW", "w", "read", 1, True):
        assert code_of(host.check, "src", host.root / "proj", bad) == "vm_bad_request"


def test_nothing_is_shared_while_no_root_is_set(host):
    assert code_of(vm_share.check_share, "src", str(host.root / "proj"), "ro", [], host.forbidden) == "vm_share_no_roots"
    assert vm_share.clean_roots([], host.forbidden) == [] and vm_share.clean_roots(None, host.forbidden) == []


def test_a_path_outside_the_roots_is_refused_and_a_link_out_is_judged_by_where_it_leads(host):
    assert code_of(host.check, "x", host.outside) == "vm_share_outside_root"
    assert code_of(host.check, "x", host.root.parent) == "vm_share_outside_root"
    (host.root / "link").symlink_to(host.outside)                       # inside the root, leads out
    assert code_of(host.check, "x", host.root / "link") == "vm_share_outside_root"
    (host.root / "inner").symlink_to(host.root / "proj")                # leads somewhere allowed
    assert host.check("x", host.root / "inner")["host_path"] == os.path.realpath(host.root / "proj")
    (host.root / "up").symlink_to(host.root / "proj" / ".." / ".." / "elsewhere")
    assert code_of(host.check, "x", host.root / "up") == "vm_share_outside_root"
    assert code_of(host.check, "x", host.root / "proj" / ".." / ".." / "elsewhere") == "vm_share_outside_root"


@pytest.mark.parametrize("rel", [".", ".ssh", ".gnupg", ".aws", "Library", "Library/Keychains", ".claude",
                                  ".claude-launcher", ".claude-launcher/daemon"])
def test_the_home_itself_and_the_credential_and_state_folders_are_never_shared(tmp_path, rel):
    host = Host(tmp_path)
    # even with the home as a root: a root that holds one of them is not used
    assert vm_share.clean_roots([str(host.home)], host.forbidden) == []
    roots = [os.path.realpath(host.home)]                               # forced: the rule still stands
    path = host.home / rel
    assert code_of(vm_share.check_share, "x", str(path), "ro", roots, host.forbidden) == "vm_share_forbidden"
    sub = path / "deeper"
    sub.mkdir(parents=True, exist_ok=True)
    if rel != ".":
        assert code_of(vm_share.check_share, "x", str(sub), "ro", roots, host.forbidden) == "vm_share_forbidden"


def test_a_link_into_a_forbidden_folder_is_refused_too(host):
    roots = [os.path.realpath(host.home)]
    (host.home / "works" / "key").symlink_to(host.home / ".ssh")
    assert code_of(vm_share.check_share, "x", str(host.home / "works" / "key"), "ro", roots,
                   host.forbidden) == "vm_share_forbidden"


@pytest.mark.parametrize("bad", ["rel/path", "", "/a:b", "/a\nb", "x" * 2000, None, 5])
def test_a_path_must_be_absolute_without_the_separator_or_control_characters(host, bad):
    assert code_of(vm_share.check_share, "x", bad, "ro", host.roots, host.forbidden) == "vm_bad_request"


def test_a_missing_path_or_a_file_is_refused(host):
    (host.root / "f.txt").write_text("x")
    assert code_of(host.check, "x", host.root / "nothing") == "vm_bad_request"
    assert code_of(host.check, "x", host.root / "f.txt") == "vm_bad_request"


@pytest.mark.parametrize("name", ["", "a b", "../x", "-x", "x" * 70, "a/b", None, 3])
def test_a_share_name_follows_the_name_rules(host, name):
    with pytest.raises(VmError):
        host.check(name, host.root / "proj")


def test_roots_that_are_not_folders_or_hold_a_forbidden_path_are_left_out(host):
    (host.root / "afile").write_text("x")
    got = vm_share.clean_roots([str(host.root), str(host.root / "afile"), "relative", 5, "/",
                                str(host.home), str(host.home / ".ssh"), str(host.root)], host.forbidden)
    assert got == [os.path.realpath(host.root)]


def test_the_tart_argument_is_name_path_and_ro_only_for_read_only():
    assert vm_share.tart_arg({"name": "src", "host_path": "/p/q", "mode": "ro"}) == "src:/p/q:ro"
    assert vm_share.tart_arg({"name": "out", "host_path": "/p/q r", "mode": "rw"}) == "out:/p/q r"


def test_the_guest_path_and_the_guidance_a_session_reads():
    assert vm_share.guest_path("src") == "/Volumes/My Shared Files/src"
    text = vm_share.guidance([{"name": "src", "host_path": "/p", "mode": "ro"}])
    for part in ("편집은 호스트 경로", "vm_exec", "로컬 사본", "rsync -a --delete '/Volumes/My Shared Files/src/'",
                 "공유 폴더에서 직접 빌드하거나 git을 사용하지 않는다", "swiftc", "modified during the build"):
        assert part in text, part
    assert vm_share.guidance([]) == ""


# ---- the registry --------------------------------------------------------------------- #
def test_shares_are_saved_and_come_back_and_a_change_returns_the_record(tmp_path, host):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    got = reg.share_add("a", host.check("src", host.root / "proj"))
    assert [s["name"] for s in got.shared_dirs] == ["src"]                 # the record, not None
    assert [s["name"] for s in reg.view("a")["shared_dirs"]] == ["src"]
    again = Registry(path)
    again.load()
    assert again.get("a").shared_dirs == reg.get("a").shared_dirs
    removed = reg.share_remove("a", "src")
    assert removed.shared_dirs == [] and json.loads(path.read_text())["vms"][0]["shared_dirs"] == []
    fresh = Registry(path)
    fresh.load()
    assert fresh.get("a").shared_dirs == []


def test_names_are_unique_and_four_at_most(tmp_path, host):
    reg = Registry()
    reg.add("a")
    for i in range(4):
        (host.root / f"d{i}").mkdir()
        reg.share_add("a", host.check(f"d{i}", host.root / f"d{i}"))
    (host.root / "d5").mkdir()
    assert code_of(reg.share_add, "a", host.check("d5", host.root / "d5")) == "vm_bad_request"
    assert code_of(reg.share_add, "a", host.check("d0", host.root / "d1")) == "vm_bad_request"
    assert code_of(reg.share_remove, "a", "nope") == "vm_bad_request"


def test_a_hand_edited_file_keeps_only_well_formed_shares(tmp_path):
    path = tmp_path / "vms.json"
    good = {"name": "src", "host_path": "/p/q", "mode": "ro"}
    rows = [good, {"name": "src", "host_path": "/other", "mode": "rw"},        # duplicate name
            {"name": "bad name", "host_path": "/p", "mode": "ro"},
            {"name": "m", "host_path": "/p", "mode": "wx"},
            {"name": "c", "host_path": "/p:q", "mode": "ro"},
            {"name": "r", "host_path": "relative", "mode": "ro"},
            "text", None, {"name": "n"}] + [{"name": f"x{i}", "host_path": f"/p{i}", "mode": "ro"} for i in range(6)]
    path.write_text(json.dumps({"vms": [{"id": "a", "tart_name": "a", "shared_dirs": rows},
                                        {"id": "b", "tart_name": "b", "shared_dirs": "not a list"}]}))
    reg = Registry(path)
    reg.load()
    assert reg.get("a").shared_dirs[0] == {**good, "origin": {"kind": "vm", "by_session": "", "at": 0.0}} and len(reg.get("a").shared_dirs) == 4
    assert reg.get("b").shared_dirs == []


# ---- starting ------------------------------------------------------------------------------ #
def manager(tmp_path, host, roots=True):
    fake = Fake()
    reg = Registry()
    reg.add("a", ssh_user="admin")
    mgr = vm_tart.Manager(reg, log_dir=tmp_path, run=fake.run, spawn=fake.spawn, alive=lambda p: True,
                          sleep=lambda s: None, stop_wait=0.05, boot_wait=0.5, locked=lambda: False,
                          share_roots=(lambda: [str(host.root)]) if roots else None,
                          share_forbidden=lambda: host.forbidden)
    return fake, reg, mgr


def test_run_argv_passes_each_share_as_a_dir(tmp_path, host):
    _, _, mgr = manager(tmp_path, host)
    shares = [{"name": "src", "host_path": "/p/q", "mode": "ro"}, {"name": "out", "host_path": "/p/r s", "mode": "rw"}]
    argv = mgr.run_argv("a", True, shares=shares)
    assert argv[argv.index("--dir") + 1] == "src:/p/q:ro"
    assert ["--dir", "out:/p/r s"] == argv[argv.index("out:/p/r s") - 1: argv.index("out:/p/r s") + 1]
    assert argv.count("--dir") == 2 and argv[-2:] == ["--", "a"]
    assert "--dir" not in mgr.run_argv("a", True)                              # no shares: the argv is what it was


def test_start_mounts_the_saved_shares_and_remembers_what_it_applied(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.share_add("a", host.check("src", host.root / "proj"))
    reg.share_add("a", host.check("out", host.root / "other", "rw"))
    vm = mgr.start("a")
    argv = fake.spawned[-1]
    real = os.path.realpath
    assert ["--dir", f"src:{real(host.root / 'proj')}:ro"] == argv[argv.index("--dir"):argv.index("--dir") + 2]
    assert f"out:{real(host.root / 'other')}" in argv
    assert vm.shares_applied == vm.shared_dirs and not vm.shares_pending() and vm.shares_skipped == {}


def test_a_change_to_a_running_vm_is_pending_until_it_restarts(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    mgr.start("a")
    assert reg.view("a")["shares_pending"] is False
    reg.share_add("a", host.check("src", host.root / "proj"))
    assert reg.view("a")["shares_pending"] is True
    reg.share_remove("a", "src")
    assert reg.view("a")["shares_pending"] is False
    reg.share_add("a", host.check("src", host.root / "proj"))
    mgr.stop("a")
    assert reg.view("a")["shares_pending"] is False                            # stopped: nothing to restart
    mgr.start("a")
    assert reg.view("a")["shares_pending"] is False


def test_an_adopted_running_vm_with_shares_reads_as_pending(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.share_add("a", host.check("src", host.root / "proj"))
    reg.adopt("a", "running")                                                  # found running: what it was given is not known
    assert reg.get("a").shares_applied is None and reg.view("a")["shares_pending"] is True


def test_a_share_that_fails_the_rules_at_start_is_left_out_and_said_so(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.share_add("a", host.check("src", host.root / "proj"))
    reg.share_add("a", host.check("link", host.root / "other"))
    reg.share_add("a", host.check("gone", host.root / "proj" / "src"))
    # the folder behind "link" is replaced by a link out of the root; "gone" is deleted
    os.rmdir(host.root / "proj" / "src")
    os.rename(host.root / "other", host.root / "other.old")
    os.symlink(host.outside, host.root / "other")
    vm = mgr.start("a")
    argv = fake.spawned[-1]
    assert argv.count("--dir") == 1 and any(a.startswith("src:") for a in argv)
    assert set(vm.shares_skipped) == {"link", "gone"}
    assert any(r.get("what") == "share_skipped" and r["share"] == "link" for r in reg.audit_log)
    assert vm.state == "running"


def test_with_the_roots_removed_nothing_is_mounted(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host, roots=False)
    reg.share_add("a", host.check("src", host.root / "proj"))
    vm = mgr.start("a")
    assert "--dir" not in fake.spawned[-1] and "src" in vm.shares_skipped
    assert "vm_share_no_roots" in vm.shares_skipped["src"]


def test_a_locked_vm_does_not_start_with_shares_either(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.share_add("a", host.check("src", host.root / "proj"))
    reg.set_locked("a", True)
    assert code_of(mgr.start, "a") == "vm_locked" and not fake.spawned


# ---- the routes --------------------------------------------------------------------------------- #
@contextlib.asynccontextmanager
async def api_for(tmp_path, host):
    fake, reg, mgr = manager(tmp_path, host)
    reg.add("b")
    fake.tart["b"] = "stopped"
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    frames = vm_frames.Frames(reg, run=Capture(), interval=0.01)
    presets = vm_presets.Presets(tmp_path / "p.json", reg, mgr, disk_free=lambda: 10 ** 12)
    app = web.Application()
    vm_api.install(app, reg, mgr, hub, frames, presets)
    async with TestClient(TestServer(app)) as client:
        client.fake, client.reg, client.mgr = fake, reg, mgr
        yield client


def run(coro):
    return asyncio.run(coro)


def test_the_user_adds_and_removes_a_share_and_a_session_cannot(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            body = {"action": "add", "name": "src", "host_path": str(host.root / "proj")}
            for hdr in (AGENT, {actors.HEADER: "cflow:wf"}):
                resp = await api.post("/api/vm/a/share", json=body, headers=hdr)
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert api.reg.get("a").shared_dirs == []
            ok = await api.post("/api/vm/a/share", json=body)
            row = await ok.json()
            assert ok.status == 200 and row["shared_dirs"][0]["mode"] == "ro"
            assert row["shared_dirs"][0]["guest_path"] == "/Volumes/My Shared Files/src"
            assert any(r.get("what") == "share_add" and r["name"] == "src" for r in api.reg.audit_log)
            denied = await api.post("/api/vm/a/share", json={"action": "rm", "name": "src"}, headers=AGENT)
            assert denied.status == 403 and len(api.reg.get("a").shared_dirs) == 1
            gone = await api.post("/api/vm/a/share", json={"action": "rm", "name": "src"})
            assert gone.status == 200 and (await gone.json())["shared_dirs"] == []
            assert any(r.get("what") == "share_rm" for r in api.reg.audit_log)
    run(go())


def test_the_routes_refuse_what_the_rules_refuse(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            for path, status, code in ((host.outside, 400, "vm_share_outside_root"),
                                       (host.home, 400, "vm_share_outside_root"),
                                       (host.root / "nothing", 400, "vm_bad_request")):
                resp = await api.post("/api/vm/a/share", json={"action": "add", "name": "x", "host_path": str(path)})
                assert resp.status == status and (await resp.json())["code"] == code, path
            bad = await api.post("/api/vm/a/share", json={"action": "add", "name": "x", "host_path": str(host.root), "mode": "rwx"})
            assert bad.status == 400
            unknown = await api.post("/api/vm/a/share", json={"action": "move"})
            assert unknown.status == 400
            assert api.reg.get("a").shared_dirs == []
        api.mgr._share_roots = lambda: []                                  # nothing allowed
    run(go())
    async def none():
        fake, reg, mgr = manager(tmp_path / "2", host, roots=False) if (tmp_path / "2").mkdir() is None else None
        app = web.Application()
        vm_api.install(app, reg, mgr, vm_channel.Hub(reg, log_dir=tmp_path), vm_frames.Frames(reg, run=Capture(), interval=0.01),
                       vm_presets.Presets(tmp_path / "p2.json", reg, mgr))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/vm/a/share", json={"action": "add", "name": "x", "host_path": str(host.root)})
            assert resp.status == 409 and (await resp.json())["code"] == "vm_share_no_roots"
    run(none())


def test_a_session_sees_its_shares_and_the_guidance_and_the_user_sees_the_roots(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.reg.share_add("a", host.check("src", host.root / "proj"))
            api.reg.grant("a", "s1", ["view"])
            listed = (await (await api.get("/api/vm?session=s1", headers=AGENT)).json())
            row = listed["vms"][0]
            assert row["shared_dirs"][0] == {"name": "src", "host_path": os.path.realpath(host.root / "proj"),
                                             "mode": "ro", "guest_path": "/Volumes/My Shared Files/src"}
            assert "편집은 호스트 경로" in row["share_guidance"] and "share_roots" not in listed
            assert row["shares_pending"] is False
            mine = await (await api.get("/api/vm/a?session=s1", headers=AGENT)).json()
            assert mine["share_guidance"]
            user = await (await api.get("/api/vm")).json()
            assert user["share_roots"] == [os.path.realpath(host.root)]
            nogrant = (await (await api.get("/api/vm?session=s9", headers={actors.HEADER: "agent:s9"})).json())["vms"]
            assert nogrant == []
    run(go())


def test_acquire_hands_the_shares_and_the_guidance_to_the_session(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.reg.share_add("a", host.check("src", host.root / "proj", "rw"))
            api.reg.grant("a", "s1", ["view", "control"])
            api.reg.claim_slot("a")
            api.reg.set_state("a", "running")
            api.reg.set_ready("a", ssh=True, gui=True, cua=True)
            got = await (await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)).json()
            assert got["ok"] and got["shared_dirs"][0]["name"] == "src" and got["shared_dirs"][0]["mode"] == "rw"
            assert got["shared_dirs"][0]["guest_path"] == "/Volumes/My Shared Files/src"
            assert "로컬 사본" in got["share_guidance"]
            api.reg.release("a", "s1")
            api.reg.share_remove("a", "src")
            again = await (await api.post("/api/vm/a/acquire", json={"session": "s1"}, headers=AGENT)).json()
            assert "shared_dirs" not in again and "share_guidance" not in again
    run(go())


# ---- the request: scopes and shares in one step -------------------------------------------------------- #
def req(share=("out", "other", "ro"), scopes=("view", "files"), **extra):
    return {"session": "s2", "scopes": list(scopes), "reason": "build it",
            "shares": [{"name": share[0], "host_path": share[1], "mode": share[2]}] if share else [], **extra}


def test_a_request_grants_nothing_and_waits_for_the_user(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            body = req(share=("out", str(host.root / "other"), "rw"))
            resp = await api.post("/api/vm/a/request", json=body, headers=S2)
            got = await resp.json()
            assert resp.status == 200 and got["pending"] and got["requested"]["scopes"] == ["view", "files"]
            assert got["requested"]["shares"][0]["mode"] == "rw"
            assert api.reg.get("a").grants == {} and api.reg.get("a").shared_dirs == []
            seen = (await (await api.get("/api/vm")).json())["vms"][0]["requests"][0]
            assert seen["session"] == "s2" and seen["reason"] == "build it" and seen["shares"][0]["name"] == "out"
            # others' requests are not for sessions to read
            api.reg.grant("a", "s1", ["view"])
            row = (await (await api.get("/api/vm?session=s1", headers=AGENT)).json())["vms"][0]
            assert "requests" not in row
            assert api.reg.request_get("a", "s2") is not None
    run(go())


def test_a_session_acts_as_itself_when_it_asks(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            resp = await api.post("/api/vm/a/request", json=req(), headers=AGENT)         # s1 says it is s2
            assert resp.status == 403 and api.reg.request_list("a") == []
    run(go())


def test_the_user_approves_scopes_and_shares_in_one_step(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=("out", str(host.root / "other"), "rw"),
                                                         scopes=("control", "files")), headers=S2)
            rid = api.reg.request_get("a", "s2")["id"]
            for path in ("approve", "deny"):
                denied = await api.post(f"/api/vm/a/{path}", json={"session": "s2", "id": rid}, headers=S2)
                assert denied.status == 403 and (await denied.json())["code"] == "vm_user_only"
            assert api.reg.get("a").grants == {} and api.reg.request_get("a", "s2") is not None
            ok = await api.post("/api/vm/a/approve", json={"session": "s2", "id": rid})
            row = await ok.json()
            assert ok.status == 200 and row["grants"] == {"s2": ["view", "control", "files"]}
            assert row["shared_dirs"][0]["name"] == "out" and row["shared_dirs"][0]["mode"] == "rw"
            assert row["requests"] == []
            assert any(r.get("what") == "user_approve" and r["to"] == "s2" for r in api.reg.audit_log)
            assert (await api.post("/api/vm/a/approve", json={"session": "s2", "id": rid})).status == 400   # once only
    run(go())


def test_an_answer_is_for_the_request_the_user_saw(tmp_path, host):
    """A session that asks again replaces its request; approving by session alone
    would grant what the user never saw."""
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=None, scopes=("files",)), headers=S2)
            seen = (await (await api.get("/api/vm")).json())["vms"][0]["requests"][0]
            await api.post("/api/vm/a/request", json=req(share=("out", str(host.root / "other"), "rw"),
                                                         scopes=("exec", "files")), headers=S2)
            assert api.reg.request_get("a", "s2")["id"] != seen["id"]
            for path in ("approve", "deny"):
                for body in ({"session": "s2", "id": seen["id"]}, {"session": "s2"}, {"session": "s2", "id": 5}):
                    resp = await api.post(f"/api/vm/a/{path}", json=body)
                    assert resp.status == 409 and (await resp.json())["code"] == "vm_request_changed", (path, body)
            assert api.reg.get("a").grants == {} and api.reg.get("a").shared_dirs == []
            assert api.reg.request_get("a", "s2") is not None
    run(go())


def test_a_denied_request_changes_nothing(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=("out", str(host.root / "other"), "ro")), headers=S2)
            rid = api.reg.request_get("a", "s2")["id"]
            denied = await api.post("/api/vm/a/deny", json={"session": "s2", "id": rid})
            assert denied.status == 200
            assert api.reg.get("a").grants == {} and api.reg.get("a").shared_dirs == []
            assert any(r.get("what") == "user_deny" for r in api.reg.audit_log)
            assert (await api.post("/api/vm/a/deny", json={"session": "s2", "id": rid})).status == 400
    run(go())


def test_a_request_for_a_share_the_rules_refuse_is_refused_at_once(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            for path, code in ((host.home / ".ssh", "vm_share_outside_root"), (host.outside, "vm_share_outside_root"),
                               (host.home, "vm_share_outside_root")):
                resp = await api.post("/api/vm/a/request", json=req(share=("x", str(path), "ro")), headers=S2)
                assert resp.status == 400 and (await resp.json())["code"] == code, path
            assert api.reg.request_list("a") == []
            for bad in ({"scopes": "view"}, {"scopes": [], "shares": []}, {"scopes": ["root"]},
                        {"shares": ["x"], "scopes": []}, {"scopes": ["view"], "shares": [{}] * 5}):
                resp = await api.post("/api/vm/a/request", json={"session": "s2", **bad}, headers=S2)
                assert resp.status == 400, bad
    run(go())


def test_approval_rechecks_the_share_and_applies_nothing_when_it_fails(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=("out", str(host.root / "other"), "rw")), headers=S2)
            os.rename(host.root / "other", host.root / "other.old")
            os.symlink(host.outside, host.root / "other")                  # now leads out of the root
            rid = api.reg.request_get("a", "s2")["id"]
            resp = await api.post("/api/vm/a/approve", json={"session": "s2", "id": rid})
            assert resp.status == 400 and (await resp.json())["code"] == "vm_share_outside_root"
            assert api.reg.get("a").grants == {} and api.reg.get("a").shared_dirs == []
            assert api.reg.request_get("a", "s2") is not None            # still there for the user to deny
    run(go())


def test_a_request_replaces_the_sessions_earlier_one_and_asks_only_for_what_is_missing(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.reg.grant("a", "s2", ["view"])
            await api.post("/api/vm/a/request", json=req(share=None, scopes=("view", "exec")), headers=S2)
            assert api.reg.request_get("a", "s2")["scopes"] == ["exec"]
            await api.post("/api/vm/a/request", json=req(share=None, scopes=("files",)), headers=S2)
            assert api.reg.request_get("a", "s2")["scopes"] == ["files"] and len(api.reg.request_list("a")) == 1
            nothing = await api.post("/api/vm/a/request", json=req(share=None, scopes=("view",)), headers=S2)
            assert nothing.status == 400                                    # already held
    run(go())


def test_a_locked_vm_takes_no_requests_and_the_pending_ones_are_bounded(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            api.reg.set_locked("a", True)
            resp = await api.post("/api/vm/a/request", json=req(share=None), headers=S2)
            assert resp.status == 423 and (await resp.json())["code"] == "vm_locked"
            api.reg.set_locked("a", False)
            for i in range(vmhub.REQUESTS_MAX):
                sess = f"q{i}"
                ok = await api.post("/api/vm/a/request", json={**req(share=None), "session": sess},
                                    headers={actors.HEADER: f"agent:{sess}"})
                assert ok.status == 200
            over = await api.post("/api/vm/a/request", json=req(share=None), headers=S2)
            assert over.status == 400
    run(go())


def test_a_session_that_exits_loses_its_requests(tmp_path, host):
    async def go():
        async with api_for(tmp_path, host) as api:
            await api.post("/api/vm/a/request", json=req(share=None), headers=S2)
            api.reg.session_exited("s2")
            assert api.reg.request_list("a") == []
    run(go())


def test_requests_are_not_saved_and_a_removed_vm_drops_them(tmp_path):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a")
    reg.request_add("a", "s2", ["files"], [], "why")
    assert "requests" not in path.read_text()
    again = Registry(path)
    again.load()
    assert again.request_list("a") == []
    reg.remove("a")
    assert "a" not in reg.requests


# ---- the tool and the commands --------------------------------------------------------------------------- #
def test_the_request_tool_and_the_guidance_pack_exist():
    from claude_launcher import prompt_packs, vm_mcp
    tool = next(t for t in vm_mcp.TOOLS if t["name"] == "vm_request")
    assert tool["inputSchema"]["required"] == ["vm"]
    assert tool["inputSchema"]["properties"]["shares"]["maxItems"] == 4
    body = prompt_packs.shipped("mcp-vm").body
    for part in ("vm_request", "rsync -a --delete", "/Volumes/My Shared Files/", "Do not build or use `git` inside the shared folder",
                 "swiftc", "next starts"):
        assert part in body, part
    assert not prompt_packs.shipped("mcp-vm").on_demand


def test_the_request_tool_posts_the_session_scopes_and_shares(monkeypatch):
    from claude_launcher import vm_mcp
    calls = []

    class Client:
        def post(self, path, body, timeout=0):
            calls.append((path, body))
            return {"ok": True}
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    monkeypatch.setattr(vm_mcp, "_client", lambda: Client())
    vm_mcp.call_tool("vm_request", {"vm": "a", "scopes": ["files"], "reason": "r",
                                     "shares": [{"name": "src", "host_path": "/p", "mode": "ro"}]})
    assert calls == [("/api/vm/a/request", {"session": "s7", "scopes": ["files"], "reason": "r",
                                             "shares": [{"name": "src", "host_path": "/p", "mode": "ro"}]})]


def test_the_requests_and_file_routes_are_never_queued_for_later():
    from claude_launcher import request_spool
    for path in ("/api/vm/a/request", "/api/vm/a/files/put", "/api/vm/a/files/get", "/api/vm/a/files/rm",
                 "/api/vm/a/share", "/api/vm/a/approve"):
        assert not request_spool.spoolable("POST", path), path
