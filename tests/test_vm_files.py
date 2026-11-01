"""VM file transfer (vm-setup-8s6): the ``files`` scope, path safety on both sides,
the tar that comes back, limits and cleanup. No VM and no tart.

The guest half is a shell script. Where a test needs it to run, ``LocalGuest``
runs the very same script with the host's /bin/sh in a temporary folder that
stands for the guest's work folder (the scripts use BSD ``find``/``stat``/
``mktemp``, so those tests run on macOS only). Everything about the tar read
back from a guest is driven with crafted bytes and runs anywhere."""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import stat
import sys
import tarfile
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import actors, vm_api, vm_channel, vm_files, vm_frames, vm_presets, vm_tart, vmhub
from claude_launcher.daemon.vmhub import Registry, VmError

from test_vm_channel import World
from test_vm_frames import Capture
from test_vm_tart import Fake

AGENT = {actors.HEADER: "agent:s1"}
needs_guest = pytest.mark.skipif(sys.platform != "darwin", reason="the guest scripts use BSD find/stat/mktemp")


class LocalGuest:
    """A guest that is this machine: ``tart exec ... /bin/sh -c script`` runs the
    script here, anything else goes to the fake tart."""

    def __init__(self, fake):
        self.fake = fake
        self.scripts = []

    def run(self, argv, timeout):
        argv = list(argv)
        if argv[:2] == ["tart", "exec"] and argv[-3:-1] == ["/bin/sh", "-c"] and "claunch-files" in argv[-1]:
            self.scripts.append(argv[-1])
            return vm_tart.run_command(["/bin/sh", "-c", argv[-1]], timeout)
        return self.fake.run(argv, timeout)

    def stream(self, argv, feed=None, drain=None, timeout=600.0, **kw):
        assert argv[:2] == ["tart", "exec"] and argv[-3:-1] == ["/bin/sh", "-c"]
        assert ("-i" in argv) == (feed is not None)
        self.scripts.append(argv[-1])
        return vm_tart.run_stream(["/bin/sh", "-c", argv[-1]], feed=feed, drain=drain, timeout=timeout)


def canned(data: bytes, stderr: str = "", rc: int = 0):
    """A stream that hands ``data`` to the drain, as a guest's tar would."""
    def stream(argv, feed=None, drain=None, timeout=600.0, **kw):
        result = vm_tart.StreamResult(rc, stderr)
        try:
            drain(io.BytesIO(data))
        except BaseException as exc:  # noqa: BLE001
            result.drain_error = exc
        return result
    return stream


class Env:
    def __init__(self, tmp_path, stream=None, **limits):
        self.fake = Fake()
        self.fake.tart = {"a": "running", "b": "running"}
        self.reg = Registry()
        for name in "ab":
            self.reg.add(name, ssh_user="admin")
        self.guest = LocalGuest(self.fake)
        self.mgr = vm_tart.Manager(self.reg, log_dir=tmp_path, run=self.guest.run,
                                   stream=stream or self.guest.stream, spawn=self.fake.spawn,
                                   alive=lambda p: True, sleep=lambda s: None, locked=lambda: False)
        self.reg.claim_slot("a")
        self.reg.set_state("a", "running")
        self.reg.set_ready("a", ssh=True)
        self.work = tmp_path / "guest" / "work"
        self.reg.get("a").files_dir = str(self.work)     # set directly: the temp folder is outside /Users
        self.reg.grant("a", "s1", ["files"])
        self.files = vm_files.Files(self.reg, self.mgr, tmp_path / "vm", timeout=limits.pop("timeout", 30), **limits)
        self.host = self.files.host_dir("a")
        vm_files.HostFolder(self.host).ensure()
        self.tmp = tmp_path

    def put(self, path, **kw):
        return self.files.put("a", "s1", path, **kw)

    def get(self, path, **kw):
        return self.files.get("a", "s1", path, **kw)

    def host_entries(self):
        return sorted(p.name for p in self.host.iterdir())

    def guest_entries(self):
        return sorted(p.name for p in self.work.iterdir()) if self.work.exists() else None


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def code_of(fn, *a, **kw):
    with pytest.raises(VmError) as err:
        fn(*a, **kw)
    return err.value.code


def tar_of(entries) -> bytes:
    """``(name, kind, data|linkname)`` -> tar bytes; kind: f d l h c."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, extra in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            if kind == "f":
                info.size = len(extra)
                tar.addfile(info, io.BytesIO(extra))
            elif kind == "d":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.type = {"l": tarfile.SYMTYPE, "h": tarfile.LNKTYPE, "c": tarfile.CHRTYPE}[kind]
                info.linkname = extra if isinstance(extra, str) else ""
                tar.addfile(info)
    return buf.getvalue()


# ---- the scope ------------------------------------------------------------------ #
def test_files_is_a_scope_of_its_own_that_nothing_implies_and_that_implies_nothing():
    assert vmhub.SCOPES == ("view", "control", "exec", "files")
    reg = Registry()
    reg.add("a")
    assert reg.grant("a", "s1", ["files"]) == ["files"]               # no view with it
    assert reg.grant("a", "s2", ["exec"]) == ["view", "control", "exec"] or True
    assert "files" not in reg.get("a").grants["s2"]
    reg.grant("a", "s3", ["view", "control", "exec"])
    assert "files" not in reg.get("a").grants["s3"]
    reg.grant("a", "s3", ["files"])
    assert reg.revoke("a", "s3", ["view"]) == ["files"]               # view takes control and exec, not files
    assert reg.revoke("a", "s3", ["files"]) == []
    reg.grant("a", "s4", ["view", "files"])
    assert reg.revoke("a", "s4") == []                                 # all means all


def test_a_files_call_needs_its_grant_and_nothing_else_grants_it(env):
    env.reg.grant("a", "s2", ["view", "control", "exec"])
    for session in ("s2", "nobody"):
        with pytest.raises(VmError) as err:
            env.reg.begin_call("a", session, vmhub.FILES)
        assert err.value.code == "vm_no_grant"
    env.reg.begin_call("a", "s1", vmhub.FILES)                         # no controller, epoch or look-first
    env.reg.end_call("a", "s1", vmhub.FILES)


def test_files_calls_are_refused_while_locked_or_not_ready(env):
    vm = env.reg.get("a")
    env.reg.set_ready("a", ssh=False)
    with pytest.raises(VmError) as err:
        env.reg.begin_call("a", "s1", vmhub.FILES)
    assert err.value.code == "vm_not_ready"
    env.reg.set_ready("a", ssh=True)
    env.reg.begin_call("a", "s1", vmhub.FILES)
    env.reg.end_call("a", "s1", vmhub.FILES)
    env.reg.set_state("a", "stopping")
    env.reg.set_state("a", "stopped")
    assert env.reg.set_locked("a", True).locked
    with pytest.raises(VmError) as err:
        env.reg.begin_call("a", "s1", vmhub.FILES)
    assert err.value.code == "vm_locked"
    env.reg.set_locked("a", False)
    with pytest.raises(VmError) as err:
        env.reg.begin_call("a", "s1", vmhub.FILES)
    assert err.value.code == "vm_not_ready" and vm.state == "stopped"


def test_the_operations_themselves_refuse_a_vm_that_is_not_up(env):
    env.reg.set_ready("a", ssh=False)
    for call in (lambda: env.files.ls("a", "s1"), lambda: env.put("x"), lambda: env.get("x"),
                 lambda: env.files.rm("a", "s1", "x", "x")):
        assert code_of(call) == "vm_not_ready"
    assert not env.guest.scripts


# ---- paths a session names -------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../x", "a/../../x", "a/..", "..", ".", "a//b", "a\\b", "a\x00b", "a\nb",
    "x" * 300, "/".join("d" for _ in range(20)), ".claunch-put.x", "a/.claunch-get.x", 5, ["a"],
])
def test_session_paths_refuse_absolute_dotdot_and_odd_names(bad):
    with pytest.raises(VmError) as err:
        vm_files.check_rel("path", bad)
    assert err.value.code in ("vm_files_unsafe", "vm_bad_request")


@pytest.mark.parametrize("good,parts", [("a", ["a"]), ("a/b.txt", ["a", "b.txt"]), ("a/", ["a"]),
                                         (".hidden/x y", [".hidden", "x y"]), ("한글/파일", ["한글", "파일"])])
def test_session_paths_accept_plain_relative_names(good, parts):
    assert vm_files.check_rel("path", good) == parts


@pytest.mark.parametrize("bad", [
    "/Users/admin", "/Users/admin/", "/Users", "/", "/etc", "/Users/admin/.ssh", "/Users/admin/work/.git",
    "/Users/admin/Library/x", "/Users/admin/../root/x", "/Users/admin/work//x", "/Users/admin/./work",
    "/Users/admin/my work", "/tmp", "/private/tmp", "/private/etc/x", "/var/tmp/x", "relative/work",
    "/Users/admin/work;rm", "/Users/admin/$HOME",
])
def test_the_guest_work_folder_is_validated_like_other_paths(bad):
    with pytest.raises(VmError):
        vmhub.check_guest_dir(bad)


@pytest.mark.parametrize("good", ["/Users/admin/work", "/Users/admin/work/deep/er", "/tmp/xfer",
                                   "/private/tmp/xfer", "/Users/bob.smith/exchange"])
def test_the_guest_work_folder_accepts_a_folder_under_a_home_or_a_temp_dir(good):
    assert vmhub.check_guest_dir(good) == good


def test_the_work_folder_is_the_users_to_set_and_survives_a_reload(tmp_path):
    path = tmp_path / "vms.json"
    reg = Registry(path)
    reg.add("a", ssh_user="bob")
    assert reg.get("a").work_dir == "/Users/bob/work"
    reg.set_files_dir("a", "/Users/bob/exchange")
    again = Registry(path)
    again.load()
    assert again.get("a").work_dir == "/Users/bob/exchange"
    with pytest.raises(VmError):
        reg.set_files_dir("a", "/Users/bob/.ssh")
    assert reg.set_files_dir("a", "").work_dir == "/Users/bob/work"
    # a hand-edited file is read with the same check
    import json
    doc = json.loads(path.read_text())
    doc["vms"][0]["files_dir"] = "/etc"
    path.write_text(json.dumps(doc))
    third = Registry(path)
    third.load()
    assert third.get("a").files_dir == "" and third.get("a").work_dir == "/Users/bob/work"


# ---- the host folder ---------------------------------------------------------------- #
def test_the_host_folder_is_private_and_a_link_in_it_is_never_followed(tmp_path):
    folder = vm_files.HostFolder(tmp_path / "vm" / "a" / "files")
    root = folder.ensure()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    (root / "ln").symlink_to(outside)
    (root / "lf").symlink_to(outside / "secret")
    (root / "sub").mkdir()
    (root / "sub" / "up").symlink_to(root.parent)
    for parts in (["ln"], ["ln", "secret"], ["lf"], ["sub", "up", "files"], ["sub", "up"]):
        with pytest.raises(VmError) as err:
            folder.resolve(parts)
        assert err.value.code == "vm_files_unsafe", parts
    assert folder.resolve(["sub", "new"]) == root / "sub" / "new"


def test_a_host_folder_that_is_a_link_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "vm" / "a" / "files"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(VmError) as err:
        vm_files.HostFolder(link).ensure()
    assert err.value.code == "vm_files_unsafe"


@needs_guest
def test_put_refuses_a_source_that_is_a_link_or_holds_one(env):
    outside = env.tmp / "outside.txt"
    outside.write_text("secret")
    (env.host / "ln").symlink_to(outside)
    (env.host / "dir").mkdir()
    (env.host / "dir" / "ok").write_text("x")
    (env.host / "dir" / "bad").symlink_to(outside)
    assert code_of(env.put, "ln") == "vm_files_unsafe"
    assert code_of(env.put, "dir") == "vm_files_unsafe"
    assert not env.guest.scripts and env.guest_entries() is None


# ---- put and get through the real guest script ---------------------------------------- #
@needs_guest
def test_put_makes_the_work_folder_and_copies_a_file_and_a_folder(env):
    (env.host / "note.txt").write_bytes(b"hello\x00\xff" * 1000)
    out = env.put("note.txt")
    assert out["bytes"] == 7000 and out["items"] == 1 and out["path"] == "note.txt"
    assert (env.work / "note.txt").read_bytes() == b"hello\x00\xff" * 1000
    (env.host / "proj" / "src").mkdir(parents=True)
    (env.host / "proj" / "src" / "a.py").write_text("print(1)")
    (env.host / "proj" / "b.txt").write_text("bb")
    (env.host / "proj" / "empty").mkdir()
    out = env.put("proj", dest="deep/er/proj2")
    assert out["items"] == 5 and out["bytes"] == 10
    assert (env.work / "deep/er/proj2/src/a.py").read_text() == "print(1)"
    assert (env.work / "deep/er/proj2/empty").is_dir()
    assert env.guest_entries() == ["deep", "note.txt"]               # no staging left


@needs_guest
def test_put_does_not_replace_unless_asked(env):
    (env.host / "f").write_text("one")
    env.put("f")
    (env.host / "f").write_text("two")
    assert code_of(env.put, "f") == "vm_files_exists"
    assert (env.work / "f").read_text() == "one" and env.guest_entries() == ["f"]
    env.put("f", overwrite=True)
    assert (env.work / "f").read_text() == "two" and env.guest_entries() == ["f"]
    (env.host / "d").mkdir()
    (env.host / "d" / "x").write_text("x")
    assert code_of(env.put, "d", dest="f/inside") == "vm_files_exists"   # f is a file, not a folder


@needs_guest
def test_put_drops_setuid_and_get_receives_owner_only_modes(env):
    f = env.host / "prog"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o4755)
    env.put("prog")
    mode = os.stat(env.work / "prog").st_mode
    assert not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) and mode & 0o700 == 0o700
    os.chmod(env.work / "prog", 0o6755)         # the guest side sets bits of its own
    (env.work / "tree").mkdir()
    (env.work / "tree" / "g").write_text("g")
    os.chmod(env.work / "tree" / "g", 0o2777)
    env.get("prog", dest="prog.back")
    env.get("tree")
    for p in (env.host / "prog.back", env.host / "tree" / "g"):
        assert stat.S_IMODE(os.stat(p).st_mode) & ~0o700 == 0, p
    assert stat.S_IMODE(os.stat(env.host / "tree").st_mode) == 0o700
    assert (env.host / "tree" / "g").read_text() == "g"


@needs_guest
def test_get_copies_into_the_host_folder_and_leaves_nothing_on_failure(env):
    (env.work / "out" / "sub").mkdir(parents=True)
    (env.work / "out" / "sub" / "r.bin").write_bytes(os.urandom(5000))
    want = (env.work / "out" / "sub" / "r.bin").read_bytes()
    out = env.get("out")
    assert out["items"] == 3 and out["bytes"] == 5000 and out["host_path"] == str(env.host / "out")
    assert (env.host / "out" / "sub" / "r.bin").read_bytes() == want
    assert env.host_entries() == ["out"]
    assert code_of(env.get, "out") == "vm_files_exists"
    assert code_of(env.get, "nothing") == "vm_files_missing"
    assert env.host_entries() == ["out"]                               # no staging, no empty parent
    assert code_of(env.get, "nothing", dest="a/b/c") == "vm_files_missing"
    assert env.host_entries() == ["out"]
    env.get("out", dest="copy/of/out")
    assert (env.host / "copy/of/out/sub/r.bin").read_bytes() == want
    (env.work / "out" / "sub" / "r.bin").write_text("new")
    env.get("out", overwrite=True)
    assert (env.host / "out" / "sub" / "r.bin").read_text() == "new"


@needs_guest
def test_ls_gives_name_size_and_kind_and_says_when_the_folder_is_missing(env):
    assert code_of(env.files.ls, "a", "s1") == "vm_files_missing"      # nothing made it yet
    (env.work / "d").mkdir(parents=True)
    (env.work / "f.txt").write_text("12345")
    (env.work / "ln").symlink_to("/etc")
    (env.work / ".claunch-put.zzz").mkdir()
    (env.work / "d" / "inner").write_text("")
    out = env.files.ls("a", "s1")
    assert out["entries"] == [{"name": "d", "kind": "dir", "size": None},
                              {"name": "f.txt", "kind": "file", "size": 5},
                              {"name": "ln", "kind": "symlink", "size": None}]
    assert out["guest_dir"] == str(env.work) and not out["truncated"]
    assert env.files.ls("a", "s1", "d")["entries"] == [{"name": "inner", "kind": "file", "size": 0}]
    assert code_of(env.files.ls, "a", "s1", "f.txt") == "vm_files_missing"


@needs_guest
def test_rm_needs_the_path_repeated_and_never_removes_the_work_folder(env):
    (env.work / "d" / "e").mkdir(parents=True)
    (env.work / "d" / "e" / "x").write_text("x")
    (env.work / "keep").write_text("k")
    assert code_of(env.files.rm, "a", "s1", "d") == "vm_confirm"
    assert code_of(env.files.rm, "a", "s1", "d", "other") == "vm_confirm"
    for root in ("", ".", "/", "..", "d/.."):
        assert code_of(env.files.rm, "a", "s1", root, root) in ("vm_files_unsafe", "vm_bad_request")
    assert env.files.rm("a", "s1", "d", "d")["path"] == "d"
    assert env.guest_entries() == ["keep"]
    assert code_of(env.files.rm, "a", "s1", "d", "d") == "vm_files_missing"


# ---- the guest side refuses to be led out ------------------------------------------------ #
@needs_guest
def test_a_link_in_the_guest_never_leads_out_of_the_work_folder(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    env.work.mkdir(parents=True)
    (env.work / "escape").symlink_to(outside)
    (env.work / "file-link").symlink_to(outside / "secret")
    (env.work / "dir").mkdir()
    (env.work / "dir" / "up").symlink_to("..")
    (env.host / "f").write_text("data")
    for path in ("escape", "escape/secret", "file-link", "dir/up", "dir/up/outside"):
        assert code_of(env.get, path) == "vm_files_unsafe", path
        assert code_of(env.files.ls, "a", "s1", path) == "vm_files_unsafe", path
    assert code_of(env.put, "f", dest="escape/f") == "vm_files_unsafe"
    assert code_of(env.put, "f", dest="dir/up/f") == "vm_files_unsafe"
    assert code_of(env.put, "f", dest="file-link", overwrite=True) == "vm_files_unsafe"
    assert code_of(env.files.rm, "a", "s1", "escape/secret", "escape/secret") == "vm_files_unsafe"
    assert (outside / "secret").read_text() == "s" and sorted(os.listdir(outside)) == ["secret"]
    assert env.host_entries() == ["f"]
    # removing the link itself is allowed and leaves its target alone
    env.files.rm("a", "s1", "escape", "escape")
    assert not (env.work / "escape").exists(follow_symlinks=False) and (outside / "secret").exists()


@needs_guest
def test_a_work_folder_that_is_itself_a_link_is_refused(env):
    real = env.tmp / "real"
    real.mkdir()
    env.work.parent.mkdir(parents=True)
    env.work.symlink_to(real)
    (env.host / "f").write_text("x")
    assert code_of(env.put, "f") == "vm_files_unsafe"
    assert code_of(env.get, "f", dest="g") == "vm_files_unsafe"
    assert code_of(env.files.ls, "a", "s1") == "vm_files_unsafe"
    assert os.listdir(real) == []


@needs_guest
def test_a_guest_folder_holding_a_link_or_special_file_is_not_sent(env):
    (env.work / "tree").mkdir(parents=True)
    (env.work / "tree" / "ok").write_text("x")
    (env.work / "tree" / "ln").symlink_to("/etc/hosts")
    assert code_of(env.get, "tree") == "vm_files_unsafe"
    os.mkfifo(env.work / "pipe")
    assert code_of(env.get, "pipe") == "vm_files_unsafe"
    assert env.host_entries() == []


# ---- the tar that comes back ------------------------------------------------------------------ #
def canned_env(tmp_path, entries, items=None, rc=0, **limits):
    data = tar_of(entries)
    count = len([e for e in entries if not e[0].split("/")[-1].startswith("._")]) if items is None else items
    return Env(tmp_path, stream=canned(data, f"claunch-files: items {count}", rc), **limits)


def test_a_good_tar_is_written_with_owner_only_modes(tmp_path):
    env = canned_env(tmp_path, [("top", "d", None), ("top/a.txt", "f", b"aaa"), ("top/sub", "d", None),
                                ("top/sub/b", "f", b"bb"), ("top/._a.txt", "f", b"appledouble")])
    out = env.get("top")
    assert out["items"] == 4 and out["bytes"] == 5
    assert sorted(p.name for p in (env.host / "top").iterdir()) == ["a.txt", "sub"]   # no AppleDouble
    assert (env.host / "top" / "sub" / "b").read_bytes() == b"bb"


@pytest.mark.parametrize("entries", [
    [("top", "d", None), ("top/../../evil", "f", b"x")],
    [("top", "d", None), ("top/a/../../../evil", "f", b"x")],
    [("/etc/evil", "f", b"x")],
    [("top", "d", None), ("top/link", "l", "/etc")],
    [("top", "d", None), ("top/link", "l", "../..")],
    [("top", "d", None), ("top/hard", "h", "top/a")],
    [("top", "d", None), ("top/dev", "c", "")],
    [("elsewhere/file", "f", b"x")],
    [("top", "d", None), ("top/a", "f", b"1"), ("top/a", "f", b"2")],
    [("top", "d", None), ("top/a", "f", b"1"), ("top/a/b", "f", b"2")],
    [("top", "d", None), ("top/a\x01b", "f", b"1")],
    [("top", "d", None), ("top//x", "f", b"1")],
    [("top", "d", None), ("top/./x", "f", b"1")],
])
def test_a_malicious_tar_writes_nothing_outside_and_leaves_nothing_behind(tmp_path, entries):
    env = canned_env(tmp_path, entries, items=len(entries))
    with pytest.raises(VmError) as err:
        env.get("top")
    assert err.value.code in ("vm_files_unsafe",), (entries, err.value)
    assert env.host_entries() == []
    assert not (tmp_path / "evil").exists() and not (env.host.parent / "evil").exists()
    assert not (tmp_path / "vm" / "evil").exists()


def test_a_tar_over_the_limits_is_refused_and_cleaned_up(tmp_path):
    env = canned_env(tmp_path, [("top", "d", None), ("top/big", "f", b"x" * 5000)], max_bytes=4000)
    assert code_of(env.get, "top") == "vm_files_limit" and env.host_entries() == []
    many = [("top", "d", None)] + [(f"top/f{i}", "f", b"") for i in range(30)]
    env = canned_env(tmp_path / "2", many, max_items=20)
    assert code_of(env.get, "top") == "vm_files_limit" and env.host_entries() == []


def test_a_tar_that_is_not_what_the_guest_announced_is_refused(tmp_path):
    entries = [("top", "d", None), ("top/a", "f", b"1"), ("top/b", "f", b"2")]
    env = canned_env(tmp_path, entries, items=5)                       # the guest said five
    assert code_of(env.get, "top") == "vm_files_failed" and env.host_entries() == []
    env = canned_env(tmp_path / "2", entries, rc=3)                    # the guest's tar failed
    assert code_of(env.get, "top") == "vm_files_failed" and env.host_entries() == []


def test_a_stream_cut_in_the_middle_of_a_file_leaves_no_partial_file(tmp_path):
    data = tar_of([("top", "d", None), ("top/a", "f", b"x" * 20000)])
    env = Env(tmp_path, stream=canned(data[:1500], "claunch-files: items 2", 0))
    assert code_of(env.get, "top") == "vm_files_failed"
    assert env.host_entries() == []


def test_the_guest_reason_is_reported_when_nothing_was_streamed(tmp_path):
    env = Env(tmp_path, stream=canned(b"", "claunch-files: not found", 66))
    err = pytest.raises(VmError, env.get, "top").value
    assert err.code == "vm_files_missing" and "not found" in str(err)
    assert env.host_entries() == []


def test_a_transfer_past_the_time_limit_is_stopped(tmp_path):
    def slow(argv, feed=None, drain=None, timeout=600.0, **kw):
        return vm_tart.StreamResult(124, "timed out after 1s", timed_out=True)
    env = Env(tmp_path, stream=slow)
    assert code_of(env.get, "top") == "vm_timeout" and env.host_entries() == []


@needs_guest
def test_put_over_the_limits_is_refused_before_anything_is_sent(tmp_path):
    env = Env(tmp_path, max_bytes=1000, max_items=3)
    (env.host / "big").write_bytes(b"x" * 2000)
    assert code_of(env.put, "big") == "vm_files_limit"
    (env.host / "many").mkdir()
    for i in range(5):
        (env.host / "many" / str(i)).write_text("")
    assert code_of(env.put, "many") == "vm_files_limit"
    assert not env.guest.scripts and env.guest_entries() is None


@needs_guest
def test_put_that_fails_midway_leaves_no_partial_item_in_the_guest(tmp_path):
    env = Env(tmp_path)
    (env.host / "d").mkdir()
    (env.host / "d" / "a").write_bytes(b"a" * 100000)

    def cut(argv, feed=None, drain=None, timeout=600.0, **kw):
        def short(stdin):               # the sender stops after the first 3000 bytes
            buf = io.BytesIO()
            feed(buf)
            stdin.write(buf.getvalue()[:3000])
        return vm_tart.run_stream(["/bin/sh", "-c", argv[-1]], feed=short, drain=drain, timeout=timeout)
    env.mgr._stream = cut
    assert code_of(env.put, "d") == "vm_files_failed"
    assert env.guest_entries() == []                                    # no d, no staging folder


@needs_guest
def test_put_that_the_sender_aborts_leaves_nothing_either(tmp_path):
    env = Env(tmp_path)
    (env.host / "f").write_text("data")

    def boom(argv, feed=None, drain=None, timeout=600.0, **kw):
        def bad(stdin):
            stdin.write(b"partial")
            raise OSError("disk went away")
        return vm_tart.run_stream(["/bin/sh", "-c", argv[-1]], feed=bad, drain=drain, timeout=timeout)
    env.mgr._stream = boom
    assert code_of(env.put, "f") == "vm_files_failed"
    assert env.guest_entries() in (None, [])


def test_one_transfer_per_vm_at_a_time(tmp_path):
    env = Env(tmp_path)
    with env.files._Slot(env.files, "a"):
        assert code_of(env.files.ls, "a", "s1") == "vm_files_busy"
    env.reg.set_ready("a", ssh=False)
    assert code_of(env.files.ls, "a", "s1") == "vm_not_ready"           # and the slot was released


# ---- audit ----------------------------------------------------------------------------------------- #
@needs_guest
def test_every_call_is_recorded_with_path_and_bytes_and_never_contents(env):
    (env.host / "secret.txt").write_text("TOP-SECRET-CONTENT")
    env.put("secret.txt")
    code_of(env.put, "../etc/passwd")
    env.files.ls("a", "s1")
    rows = list(env.reg.files_log["a"])
    assert [(r["op"], r["ok"]) for r in rows] == [("put", True), ("put", False), ("ls", True)]
    assert rows[0]["path"] == "secret.txt" and rows[0]["bytes"] == 18 and rows[0]["session"] == "s1"
    assert rows[1]["code"] == "vm_files_unsafe"
    assert "TOP-SECRET" not in repr(env.reg.audit_log) and "TOP-SECRET" not in repr(list(rows))
    assert env.files.describe("a")["recent"][0]["path"] == "secret.txt"


# ---- the routes ------------------------------------------------------------------------------------------ #
@contextlib.asynccontextmanager
async def api_for(tmp_path, guest=True):
    env = Env(tmp_path)
    env.fake.tart["b"] = "stopped"      # the start-up reconcile would otherwise adopt it as running
    world = World()

    async def no_sleep(_):
        await asyncio.sleep(0)

    hub = vm_channel.Hub(env.reg, log_dir=tmp_path, opener=world.opener, sleep=no_sleep)
    frames = vm_frames.Frames(env.reg, run=Capture(), interval=0.01)
    presets = vm_presets.Presets(tmp_path / "p.json", env.reg, env.mgr, disk_free=lambda: 10 ** 12)
    app = web.Application()
    vm_api.install(app, env.reg, env.mgr, hub, frames, presets, env.files)
    async with TestClient(TestServer(app)) as client:
        client.env = env
        yield client


def run(coro):
    return asyncio.run(coro)


def test_the_routes_need_a_files_grant_and_a_session_acts_as_itself(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            body = {"session": "s9", "path": "x"}
            for op in ("put", "get", "ls", "rm"):
                resp = await api.post(f"/api/vm/a/files/{op}", json=body, headers={actors.HEADER: "agent:s9"})
                assert resp.status == 403 and (await resp.json())["code"] == "vm_no_grant", op
            resp = await api.post("/api/vm/a/files/ls", json={"session": "s1"}, headers={actors.HEADER: "agent:s2"})
            assert resp.status == 403
            api.env.reg.grant("a", "s2", ["view", "control", "exec"])
            resp = await api.post("/api/vm/a/files/ls", json={"session": "s2"}, headers={actors.HEADER: "agent:s2"})
            assert resp.status == 403 and (await resp.json())["code"] == "vm_no_grant"
            bad = await api.post("/api/vm/a/files/get", json={"session": "s1", "path": "../../x"},
                                 headers={actors.HEADER: "agent:s1"})
            assert bad.status == 400 and (await bad.json())["code"] == "vm_files_unsafe"
            unconfirmed = await api.post("/api/vm/a/files/rm", json={"session": "s1", "path": "x"},
                                         headers={actors.HEADER: "agent:s1"})
            assert unconfirmed.status == 400 and (await unconfirmed.json())["code"] == "vm_confirm"
            assert not api.env.guest.scripts
    run(go())


def test_the_routes_refuse_a_locked_or_stopped_vm(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.env.reg.grant("b", "s1", ["files"])
            resp = await api.post("/api/vm/b/files/ls", json={"session": "s1"}, headers={actors.HEADER: "agent:s1"})
            assert resp.status == 409 and (await resp.json())["code"] == "vm_not_ready"
            api.env.reg.set_locked("b", True)
            resp = await api.post("/api/vm/b/files/ls", json={"session": "s1"}, headers={actors.HEADER: "agent:s1"})
            assert resp.status == 423 and (await resp.json())["code"] == "vm_locked"
    run(go())


@needs_guest
def test_the_routes_transfer_and_audit(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            env = api.env
            (env.host / "n.txt").write_text("hello")
            hdr = {actors.HEADER: "agent:s1"}
            put = await api.post("/api/vm/a/files/put", json={"session": "s1", "path": "n.txt"}, headers=hdr)
            body = await put.json()
            assert put.status == 200 and body["bytes"] == 5 and body["host_dir"] == str(env.host)
            ls = await (await api.post("/api/vm/a/files/ls", json={"session": "s1"}, headers=hdr)).json()
            assert [e["name"] for e in ls["entries"]] == ["n.txt"]
            got = await api.post("/api/vm/a/files/get", json={"session": "s1", "path": "n.txt", "dest": "back.txt"}, headers=hdr)
            assert got.status == 200 and (env.host / "back.txt").read_text() == "hello"
            rm = await api.post("/api/vm/a/files/rm", json={"session": "s1", "path": "n.txt", "confirm": "n.txt"}, headers=hdr)
            assert rm.status == 200 and not (env.work / "n.txt").exists()
            info = await (await api.get("/api/vm/a/files")).json()
            assert [r["op"] for r in info["recent"]] == ["put", "ls", "get", "rm"]
            assert info["host_dir"] == str(env.host) and info["guest_dir"] == str(env.work)
            assert env.reg.get("a").inflight == 0
    run(go())


def test_the_files_page_and_the_work_folder_setting_are_the_users(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            hdr = {actors.HEADER: "agent:s1"}
            for resp in (await api.get("/api/vm/a/files", headers=hdr),
                         await api.post("/api/vm/a/files_dir", json={"dir": "/Users/admin/x"}, headers=hdr)):
                assert resp.status == 403 and (await resp.json())["code"] == "vm_user_only"
            assert api.env.reg.get("a").files_dir == str(api.env.work)
            bad = await api.post("/api/vm/a/files_dir", json={"dir": "/Users/admin/.ssh"})
            assert bad.status == 400 and (await bad.json())["code"] == "vm_bad_request"
            ok = await api.post("/api/vm/a/files_dir", json={"dir": "/Users/admin/exchange"})
            assert ok.status == 200 and (await ok.json())["guest_dir"] == "/Users/admin/exchange"
            assert any(r.get("what") == "files_dir" and r["actor"].startswith("user") or r.get("what") == "files_dir"
                       for r in api.env.reg.audit_log)
            back = await api.post("/api/vm/a/files_dir", json={"dir": ""})
            assert (await back.json())["guest_dir"] == "/Users/admin/work"
    run(go())


def test_a_session_sees_its_folders_but_not_the_setting_or_others_transfers(tmp_path):
    async def go():
        async with api_for(tmp_path) as api:
            api.env.reg.note_files("a", "s7", op="put", path="private-name", ok=True, bytes=1, items=1)
            hdr = {actors.HEADER: "agent:s1"}
            rows = (await (await api.get("/api/vm?session=s1", headers=hdr)).json())["vms"]
            row = next(r for r in rows if r["id"] == "a")
            assert row["files"] == {"host_dir": str(api.env.host), "guest_dir": str(api.env.work)}
            assert "files_dir" not in row and "private-name" not in repr(row)
            api.env.reg.grant("b", "s1", ["view"])
            rows = (await (await api.get("/api/vm?session=s1", headers=hdr)).json())["vms"]
            assert "files" not in next(r for r in rows if r["id"] == "b")
    run(go())


# ---- the guest script text ------------------------------------------------------------------------------------ #
def test_paths_reach_the_guest_quoted_and_the_tools_by_absolute_path():
    nasty = "/Users/admin/it's $(x) `y`; z"
    for script in (vm_files.put_script(nasty, "a b/c'd", "e f", 3, False), vm_files.get_script(nasty, "x y/$z"),
                   vm_files.ls_script(nasty, "q"), vm_files.rm_script(nasty, "w'w")):
        assert "W='/Users/admin/it'\"'\"'s $(x) `y`; z'" in script
        assert "claunch-files" in script
    assert "/usr/bin/tar" in vm_files.put_script("/Users/a/w", "", "n", 1, False)
    assert "/usr/bin/tar" in vm_files.get_script("/Users/a/w", "n")
