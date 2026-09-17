"""Peer operations: reading a member's checkout and coordinating on keys.

Scenario matrix (docs/mesh-design.md "Peer operations"):

A. file (pure)
   A1 a relative path under the session cwd is read; the reply carries size,
      sha256 of the whole file, and the cwd-relative path
   A2 ``../`` and an absolute path outside the cwd are refused before a read
   A3 a symlink that points outside the cwd is refused
   A4 max_bytes cuts the content, marks it truncated, and the hash is still
      the whole file's
   A5 binary content comes back base64
   A6 a directory / a missing file are refused

B. git (pure)
   B1 only the five whitelisted ops build an argv; anything else is refused
   B2 an argument the op does not take is refused (not ignored)
   B3 revisions and paths that look like options are refused
   B4 a real query runs in a temp repo and returns rc 0 + output
   B5 an unknown ref returns rc != 0 with git's message, not an exception

C. lease registry (pure)
   C1 acquire on a free key grants; a second holder is refused with the
      lease; the same holder re-acquiring renews
   C2 an expired lease is reclaimable by anyone
   C3 only the holder may release; releasing a free key is a no-op
   C4 renew on a key you do not hold is refused
   C5 to_dict/from_dict round-trips; release_all drops a holder's keys
   C6 ttl is clamped to the max

D. through the mesh (two daemons, in-process peer transport)
   D1 a guest member reads a file from the primary member's cwd over the
      link (local on the primary, forwarded from the guest)
   D2 the member graph is the ACL: a cut member edge refuses the read
   D3 a peer call with a bad link token is refused
   D4 a peer may only read sessions that are members of that mesh
   D5 leases are granted by the authority: the guest's acquire is forwarded,
      the primary's own acquire is local, and they conflict on the same key
   D6 a member leaving drops its leases; leases survive a reload
   D7 git status over the link returns the porcelain output

F. addressing a peer (two daemons)
   F1 a member's SESSION name resolves to that member and routes to its
      daemon; the reply names the member, session and machine it hit
   F2 a handle still wins over a different member's session of that name
   F3 one session name on two daemons is refused as ambiguous, and
      <machine>/<session> settles it either way
   F4 an address that is neither handle nor session is refused

G. which daemon a sender speaks from (delivery block)
   G1 a sender whose session is local maps to "", one on another daemon
      to its machine name, and an external sender is left out
   G2 format_delivery writes 'local' and '<machine> (remote)', and writes
      no machine line at all when it was given no origins
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from claude_launcher import store
from claude_launcher.daemon import mesh_ops
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import (
    MeshError,
    MeshManager,
    format_delivery,
)
from claude_launcher.daemon.mesh_ops import LeaseHeld, LeaseRegistry, OpsError

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


# --------------------------------------------------------------------------- #
# A. file
# --------------------------------------------------------------------------- #
def test_read_file_under_cwd(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hi')\n", encoding="utf-8", newline="\n")
    out = mesh_ops.read_file(str(tmp_path), "src/a.py")
    assert out["content"] == "print('hi')\n"
    assert out["path"] == "src/a.py"
    assert out["size"] == 12
    assert out["truncated"] is False
    assert out["encoding"] == "utf-8"
    assert len(out["sha256"]) == 64
    # an absolute path INSIDE the cwd is fine too (copied from git output)
    same = mesh_ops.read_file(str(tmp_path), str(tmp_path / "src" / "a.py"))
    assert same["sha256"] == out["sha256"]


def test_read_file_refuses_escape(tmp_path):
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8", newline="\n")
    inner = tmp_path / "repo"
    inner.mkdir()
    with pytest.raises(OpsError, match="outside"):
        mesh_ops.read_file(str(inner), "../secret.txt")
    with pytest.raises(OpsError, match="outside"):
        mesh_ops.read_file(str(inner), str(tmp_path / "secret.txt"))
    with pytest.raises(OpsError, match="required"):
        mesh_ops.read_file(str(inner), "")
    with pytest.raises(OpsError, match="no working directory"):
        mesh_ops.read_file("", "a")


def test_read_file_refuses_symlink_out(tmp_path):
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8", newline="\n")
    inner = tmp_path / "repo"
    inner.mkdir()
    try:
        os.symlink(tmp_path / "secret.txt", inner / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    with pytest.raises(OpsError, match="outside"):
        mesh_ops.read_file(str(inner), "link.txt")


def test_read_file_truncates_but_hashes_whole(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"a" * 100)
    out = mesh_ops.read_file(str(tmp_path), "big.txt", max_bytes=10)
    assert out["content"] == "a" * 10
    assert out["truncated"] is True
    assert out["size"] == 100
    whole = mesh_ops.read_file(str(tmp_path), "big.txt")
    assert whole["sha256"] == out["sha256"]
    with pytest.raises(OpsError, match="positive"):
        mesh_ops.read_file(str(tmp_path), "big.txt", max_bytes=-1)


def test_read_file_binary_is_base64(tmp_path):
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00abc")
    out = mesh_ops.read_file(str(tmp_path), "bin")
    assert out["encoding"] == "base64"
    import base64

    assert base64.b64decode(out["content"]) == b"\xff\xfe\x00abc"


def test_read_file_refuses_dir_and_missing(tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(OpsError, match="directory"):
        mesh_ops.read_file(str(tmp_path), "d")
    with pytest.raises(OpsError, match="does not exist"):
        mesh_ops.read_file(str(tmp_path), "nope.txt")


# --------------------------------------------------------------------------- #
# B. git
# --------------------------------------------------------------------------- #
def test_git_argv_whitelist(tmp_path):
    cwd = str(tmp_path)
    assert mesh_ops.git_argv(cwd, "status")[0] == "status"
    assert "--porcelain=v1" in mesh_ops.git_argv(cwd, "status")
    assert mesh_ops.git_argv(cwd, "branch")[0] == "branch"
    assert mesh_ops.git_argv(cwd, "diff", {"base": "master", "head": "HEAD", "stat": True}) == [
        "diff", "--no-color", "--no-ext-diff", "--stat", "master", "HEAD",
    ]
    argv = mesh_ops.git_argv(cwd, "log", {"n": 3, "range": "a..b"})
    assert argv[:3] == ["log", "--no-color", "--max-count=3"] and argv[-1] == "a..b"
    assert mesh_ops.git_argv(cwd, "show", {"ref": "HEAD~1"})[-1] == "HEAD~1"
    for bad in ("push", "commit", "checkout", "", "diff;rm"):
        with pytest.raises(OpsError, match="unknown git op"):
            mesh_ops.git_argv(cwd, bad)


def test_git_argv_refuses_unknown_and_option_like_args(tmp_path):
    cwd = str(tmp_path)
    with pytest.raises(OpsError, match="does not take"):
        mesh_ops.git_argv(cwd, "status", {"base": "x"})
    with pytest.raises(OpsError, match="does not take"):
        mesh_ops.git_argv(cwd, "branch", {"n": 1})
    with pytest.raises(OpsError, match="not a valid revision"):
        mesh_ops.git_argv(cwd, "diff", {"base": "--output=/tmp/x"})
    with pytest.raises(OpsError, match="looks like an option"):
        mesh_ops.git_argv(cwd, "status", {"paths": ["--foo"]})
    with pytest.raises(OpsError, match="outside"):
        mesh_ops.git_argv(cwd, "status", {"paths": ["../other"]})
    with pytest.raises(OpsError, match="needs 'base'"):
        mesh_ops.git_argv(cwd, "diff", {"head": "x"})
    with pytest.raises(OpsError, match="between 1 and 500"):
        mesh_ops.git_argv(cwd, "log", {"n": 0})
    with pytest.raises(OpsError, match="'ref' is required"):
        mesh_ops.git_argv(cwd, "show", {})


def _init_repo(path) -> None:
    def git(*a):
        subprocess.run(["git", *a], cwd=str(path), check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (path / "f.txt").write_text("one\n", encoding="utf-8", newline="\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "first")


@pytest.fixture
def repo(tmp_path):
    try:
        _init_repo(tmp_path)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    return tmp_path


def test_git_query_runs(repo):
    (repo / "f.txt").write_text("two\n", encoding="utf-8", newline="\n")
    status = mesh_ops.git_query(str(repo), "status")
    assert status["rc"] == 0
    assert "## main" in status["output"] and " M f.txt" in status["output"]
    diff = mesh_ops.git_query(str(repo), "diff", {"paths": ["f.txt"]})
    assert diff["rc"] == 0 and "-one" in diff["output"] and "+two" in diff["output"]
    log = mesh_ops.git_query(str(repo), "log", {"n": 1})
    assert log["rc"] == 0 and log["output"].rstrip().endswith("first")
    branch = mesh_ops.git_query(str(repo), "branch")
    assert branch["rc"] == 0 and "main" in branch["output"]


def test_git_query_bad_ref_is_rc_not_exception(repo):
    out = mesh_ops.git_query(str(repo), "show", {"ref": "no-such-ref"})
    assert out["rc"] != 0
    assert out["output"]  # git's own message
    assert out["truncated"] is False


# --------------------------------------------------------------------------- #
# C. leases
# --------------------------------------------------------------------------- #
def test_lease_acquire_conflict_and_renew():
    reg = LeaseRegistry()
    a = reg.acquire("path:x", "alice", ttl=60, note="editing", now=1000)
    assert a["holder"] == "alice" and a["expires_at"] == 1060 and a["note"] == "editing"
    with pytest.raises(LeaseHeld) as exc:
        reg.acquire("path:x", "bob", ttl=60, now=1010)
    assert exc.value.lease["holder"] == "alice"
    again = reg.acquire("path:x", "alice", ttl=60, now=1030)
    assert again["expires_at"] == 1090 and again["renewals"] == 1
    assert reg.get("path:x", now=1050)["holder"] == "alice"
    with pytest.raises(OpsError, match="key"):
        reg.acquire("", "alice")
    with pytest.raises(OpsError, match="holder"):
        reg.acquire("k", "")


def test_lease_expired_is_reclaimable():
    reg = LeaseRegistry()
    reg.acquire("k", "alice", ttl=10, now=0)
    assert reg.get("k", now=10) is None
    b = reg.acquire("k", "bob", ttl=10, now=11)
    assert b["holder"] == "bob" and b["renewals"] == 0
    assert [l["holder"] for l in reg.list(now=15)] == ["bob"]
    assert reg.list(now=15, holder="alice") == []


def test_lease_release_rules():
    reg = LeaseRegistry()
    reg.acquire("k", "alice", ttl=10, now=0)
    with pytest.raises(LeaseHeld):
        reg.release("k", "bob", now=1)
    assert reg.release("k", "alice", now=1) == {"key": "k", "released": True, "holder": "alice"}
    assert reg.release("k", "alice", now=2)["released"] is False
    with pytest.raises(OpsError, match="no live lease"):
        reg.renew("k", "alice", now=3)
    reg.acquire("k", "alice", ttl=10, now=3)
    with pytest.raises(LeaseHeld):
        reg.renew("k", "bob", now=4)


def test_lease_persistence_and_release_all():
    reg = LeaseRegistry()
    reg.acquire("a", "alice", ttl=100, now=0)
    reg.acquire("b", "alice", ttl=100, now=0)
    reg.acquire("c", "bob", ttl=100, now=0)
    doc = json.loads(json.dumps(reg.to_dict()))
    back = LeaseRegistry.from_dict(doc)
    assert sorted(l["key"] for l in back.list(now=1)) == ["a", "b", "c"]
    assert sorted(back.release_all("alice")) == ["a", "b"]
    assert [l["key"] for l in back.list(now=1)] == ["c"]
    assert LeaseRegistry.from_dict({"junk": "x", "k": {"nope": 1}}).list() == []
    reg2 = LeaseRegistry()
    reg2.acquire("x", "a", ttl=1, now=0)
    assert reg2.prune(now=5) == 1 and reg2.list(now=5) == []


def test_lease_ttl_clamped():
    reg = LeaseRegistry()
    lease = reg.acquire("k", "a", ttl=10 ** 9, now=0)
    assert lease["expires_at"] == mesh_ops.LEASE_MAX_TTL
    default = reg.acquire("d", "a", now=0)
    assert default["expires_at"] == mesh_ops.LEASE_DEFAULT_TTL
    with pytest.raises(OpsError):
        reg.acquire("e", "a", ttl="soon")
    with pytest.raises(OpsError):
        reg.acquire("e", "a", ttl=0)


# --------------------------------------------------------------------------- #
# D. through the mesh
# --------------------------------------------------------------------------- #
def _register_py_harness():
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def _dispatch_peer(mm: MeshManager, path: str, body: dict) -> dict:
    """The /peer/* HTTP layer, minus the HTTP (mirrors test_mesh_v2)."""
    if path == "/peer/mesh/join_request":
        return mm.peer_join_request_accept(
            body["mesh"], body["machine"], body.get("session") or "",
            body.get("handle") or "", body.get("role") or "",
            body.get("reply_token") or "", body.get("code") or "",
        )
    if path == "/peer/mesh/grant":
        return mm.peer_grant_accept(
            body["mesh"], body["machine"], body.get("request_id") or "",
            body.get("token") or "", bool(body.get("denied")), body.get("grant"),
        )
    if path == "/peer/mesh/join":
        return mm.peer_join_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("session") or "", body.get("handle") or "",
            body.get("role") or "", body.get("parent") or "",
        )
    if path == "/peer/mesh/leave":
        return mm.peer_leave_accept(
            body["mesh"], body["machine"], body["token"], body.get("handle") or ""
        )
    if path == "/peer/mesh/send":
        return mm.peer_send_accept(
            body["mesh"], body["machine"], body["token"], body.get("message") or {}
        )
    if path == "/peer/mesh/sync":
        return mm.peer_sync_accept(
            body["mesh"], body["machine"], body["token"],
            int(body.get("base") or 0), body.get("messages") or [],
            body.get("members") or [], body.get("policy"),
            body.get("nudges") or [],
            peers=body.get("peers"), epoch=body.get("epoch"),
            links=body.get("links"), edges=body.get("edges"),
            member_edges=body.get("member_edges"),
        )
    if path == "/peer/mesh/member-link":
        return mm.peer_member_link_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("a") or "", body.get("b") or "", bool(body.get("enabled")),
        )
    if path == "/peer/ops/file":
        return mm.peer_ops_file_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("session") or "", body.get("path") or "", body.get("max_bytes"),
        )
    if path == "/peer/ops/git":
        return mm.peer_ops_git_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("session") or "", body.get("op") or "", body.get("args") or {},
        )
    if path == "/peer/ops/lease":
        return mm.peer_lease_accept(
            body["mesh"], body["machine"], body["token"],
            body.get("op") or "", body.get("key") or "", body.get("holder") or "",
            body.get("ttl"), body.get("note") or "",
        )
    raise AssertionError(f"unexpected peer path {path!r}")


def _wire(machines: dict, calls: list) -> None:
    async def call(machine, path, body):
        calls.append((machine, path))
        return _dispatch_peer(machines[machine], path, body)

    for name, mm in machines.items():
        mm.machine = name
        mm.peer_transport = call
        mm.relay_connected = lambda: True


def _manager() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)


async def _linked_pair(mgr, tmp_path, calls):
    """Primary pcA (alice@sa, cwd cwdA) linked with guest pcB (bob@sb, cwdB)."""
    mm_a = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
    mm_b = MeshManager(mgr, settle=0.05, root=tmp_path / "meshB")
    _wire({"pcA": mm_a, "pcB": mm_b}, calls)
    for s in ("sa", "sb"):
        cwd = tmp_path / f"cwd_{s}"
        cwd.mkdir()
        (cwd / "README.md").write_text(f"hello from {s}\n", encoding="utf-8", newline="\n")
        mgr.create(SessionDef(name=s, harness="py", cwd=str(cwd), rows=80))
    # an outsider session on pcA that is in NO mesh
    (tmp_path / "cwd_sx").mkdir()
    (tmp_path / "cwd_sx" / "README.md").write_text("outsider\n", encoding="utf-8", newline="\n")
    mgr.create(SessionDef(name="sx", harness="py", cwd=str(tmp_path / "cwd_sx"), rows=80))
    mm_a.create("m")
    await mm_a.join("m", "sa", handle="alice")
    await mm_b.join("m@pcA", "sb", handle="bob", code=mm_a.invite("m")["code"])
    return mm_a, mm_b


def test_file_read_across_link_and_local(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        # D1: guest bob reads alice's README on the primary -> forwarded
        out = await mm_b.ops_file("m", "sb", "alice", "README.md")
        assert out["content"] == "hello from sa\n"
        assert out["member"] == "alice" and out["machine"] == "pcA"
        assert ("pcA", "/peer/ops/file") in calls
        # and the reverse: alice reads bob's tree over the link to pcB
        out = await mm_a.ops_file("m", "sa", "bob", "README.md", max_bytes=5)
        assert out["content"] == "hello" and out["truncated"] is True
        assert out["machine"] == "pcB"
        assert ("pcB", "/peer/ops/file") in calls
        # a member reading ITSELF is local, no peer call
        n = len(calls)
        me = await mm_a.ops_file("m", "sa", "alice", "README.md")
        assert me["content"] == "hello from sa\n" and len(calls) == n
        # sandbox holds across the link too
        with pytest.raises(MeshError, match="outside"):
            await mm_b.ops_file("m", "sb", "alice", "../cwd_sx/README.md")
        # a non-member session cannot ask
        with pytest.raises(MeshError, match="not a member"):
            await mm_a.ops_file("m", "sx", "alice", "README.md")
        with pytest.raises(MeshError, match="no member"):
            await mm_a.ops_file("m", "sa", "nobody", "README.md")
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_member_graph_is_the_acl(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        mesh_a = mm_a.get("m")
        # D2: cut alice|bob on the authority; the read is refused before any
        # peer call is made
        mesh_a.member_edges[mesh_a.member_key("alice", "bob")] = False
        n = len(calls)
        with pytest.raises(MeshError, match="not connected"):
            await mm_a.ops_file("m", "sa", "bob", "README.md")
        assert len(calls) == n
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_peer_accept_checks_token_and_membership(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        good = mm_a.get("m").links["pcB"]["token_in"]
        # D3: wrong token
        with pytest.raises(MeshError, match="bad mesh peer token"):
            mm_a.peer_ops_file_accept("m", "pcB", "nope", "sa", "README.md")
        # D4: a session on pcA that is NOT in mesh m is not readable through m
        with pytest.raises(MeshError, match="not a member"):
            mm_a.peer_ops_file_accept("m", "pcB", good, "sx", "README.md")
        # the real thing
        out = mm_a.peer_ops_file_accept("m", "pcB", good, "sa", "README.md")
        assert out["content"] == "hello from sa\n"
        # lease accept: only for a holder that is a member from that machine
        with pytest.raises(MeshError, match="not a member from daemon"):
            mm_a.peer_lease_accept("m", "pcB", good, "acquire", "k", "alice")
        # a mirror refuses to grant
        with pytest.raises(MeshError, match="mirror"):
            mm_b.peer_lease_accept("m", "pcA", "x", "acquire", "k", "alice")
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_leases_granted_by_authority(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        # D5: bob (guest) acquires -> forwarded to pcA
        got = await mm_b.lease("m", "sb", "acquire", "path:src/x.py", ttl=60, note="editing")
        assert got["ok"] is True and got["holder"] == "bob"
        assert got["authority"] == "pcA"
        assert ("pcA", "/peer/ops/lease") in calls
        # alice (authority) is refused the same key, locally, with the holder
        n = len(calls)
        refused = await mm_a.lease("m", "sa", "acquire", "path:src/x.py")
        assert refused["ok"] is False and refused["held_by"] == "bob"
        assert refused["lease"]["note"] == "editing"
        assert len(calls) == n
        # alice cannot release bob's key
        stolen = await mm_a.lease("m", "sa", "release", "path:src/x.py")
        assert stolen["ok"] is False and stolen["held_by"] == "bob"
        # list from either side agrees
        listed = await mm_b.lease("m", "sb", "list")
        assert [l["key"] for l in listed["leases"]] == ["path:src/x.py"]
        listed_a = await mm_a.lease("m", "sa", "list")
        assert listed_a["leases"] == listed["leases"]
        # renew then release by the holder
        renewed = await mm_b.lease("m", "sb", "renew", "path:src/x.py", ttl=120)
        assert renewed["ok"] and renewed["lease"]["renewals"] == 1
        released = await mm_b.lease("m", "sb", "release", "path:src/x.py")
        assert released["ok"] and released["released"] is True
        now_free = await mm_a.lease("m", "sa", "acquire", "path:src/x.py")
        assert now_free["ok"] is True and now_free["lease"]["holder"] == "alice"
        with pytest.raises(MeshError, match="unknown lease op"):
            await mm_a.lease("m", "sa", "steal", "k")
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_leases_dropped_on_leave_and_survive_reload(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        await mm_b.lease("m", "sb", "acquire", "issue:claunch-1", ttl=600)
        await mm_a.lease("m", "sa", "acquire", "issue:claunch-2", ttl=600)
        # D6a: persisted on the authority, and a fresh manager reads it back
        assert (tmp_path / "meshA" / "m" / "leases.json").is_file()
        mm_a2 = MeshManager(mgr, settle=0.05, root=tmp_path / "meshA")
        mm_a2.machine = "pcA"
        mm_a2.load_all()
        assert sorted(l["key"] for l in mm_a2.get("m").leases.list()) == [
            "issue:claunch-1", "issue:claunch-2",
        ]
        # D6b: bob leaves -> its lease goes, alice's stays
        await mm_b.leave("m", "bob")
        keys = [l["key"] for l in mm_a.get("m").leases.list()]
        assert keys == ["issue:claunch-2"]
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_git_status_across_link(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        try:
            _init_repo(tmp_path / "cwd_sa")
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("git not available")
        (tmp_path / "cwd_sa" / "f.txt").write_text("changed\n", encoding="utf-8", newline="\n")
        # D7: a tracked change and an untracked file both show
        out = await mm_b.ops_git("m", "sb", "alice", "status")
        assert out["rc"] == 0 and " M f.txt" in out["output"]
        assert "?? README.md" in out["output"]
        assert out["machine"] == "pcA" and ("pcA", "/peer/ops/git") in calls
        diff = await mm_b.ops_git("m", "sb", "alice", "diff", {"paths": ["f.txt"]})
        assert "+changed" in diff["output"]
        with pytest.raises(MeshError, match="unknown git op"):
            await mm_b.ops_git("m", "sb", "alice", "push")
        with pytest.raises(MeshError, match="does not take"):
            await mm_a.ops_git("m", "sa", "alice", "branch", {"n": 1})
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# E. the member protocol names the tools (the mesh skill is the mirror)
# --------------------------------------------------------------------------- #
def test_mesh_skill_teaches_peer_ops():
    from claude_launcher import mesh_install, mesh_mcp

    md = mesh_install.SKILL_MD
    tool_names = {t["name"] for t in mesh_mcp.TOOLS}
    for name in ("peer_file", "peer_git", "lease"):
        assert name in tool_names
        assert f"`{name}" in md, f"the mesh skill never mentions {name}"
    assert "claunch mesh lease" in md
    assert "claunch mesh ops" in md


# --------------------------------------------------------------------------- #
# F. addressing a peer: handle, session name, <machine>/<session>
# --------------------------------------------------------------------------- #
def test_session_name_addresses_a_peer(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        # F1: bob names alice by the SESSION it was told about, not the handle
        out = await mm_b.ops_file("m", "sb", "sa", "README.md")
        assert out["content"] == "hello from sa\n"
        assert out["member"] == "alice"
        assert out["session"] == "sa" and out["machine"] == "pcA"
        assert ("pcA", "/peer/ops/file") in calls
        # the same address stays local when the session is on this daemon
        n = len(calls)
        mine = await mm_a.ops_file("m", "sa", "sa", "README.md")
        assert mine["member"] == "alice" and len(calls) == n
        # git takes the same address
        out = await mm_b.ops_git("m", "sb", "sa", "branch")
        assert out["session"] == "sa" and out["machine"] == "pcA"
        # F4: neither a handle nor a session
        with pytest.raises(MeshError, match="no member"):
            await mm_a.ops_file("m", "sa", "nobody", "README.md")
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_handle_wins_over_a_colliding_session_name(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        # a third member on pcA whose HANDLE is the SESSION name of the member
        # on pcB: the two addresses now collide by construction
        cwd = tmp_path / "cwd_s2"
        cwd.mkdir()
        (cwd / "README.md").write_text(
            "hello from s2\n", encoding="utf-8", newline="\n"
        )
        mgr.create(SessionDef(name="s2", harness="py", cwd=str(cwd), rows=80))
        await mm_a.join("m", "s2", handle="sb")
        out = await mm_a.ops_file("m", "sa", "sb", "README.md")
        assert out["content"] == "hello from s2\n"
        assert out["member"] == "sb" and out["session"] == "s2"
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_same_session_on_two_daemons_must_be_qualified(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        mesh = mm_a.get("m")
        # one SessionManager cannot hold two sessions of a name, so the
        # collision is made on the roster — which is what resolve_peer reads
        mesh.members["bob"].session = "sa"
        with pytest.raises(MeshError, match="more than one daemon"):
            mm_a.resolve_peer(mesh, "sa")
        assert mm_a.resolve_peer(mesh, "pcA/sa").handle == "alice"
        assert mm_a.resolve_peer(mesh, "pcB/sa").handle == "bob"
        # the handle is untouched by the collision
        assert mm_a.resolve_peer(mesh, "bob").handle == "bob"
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# G. which daemon a sender speaks from
# --------------------------------------------------------------------------- #
def test_delivery_origins_split_local_from_remote(home, tmp_path):
    _register_py_harness()
    calls = []

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _linked_pair(mgr, tmp_path, calls)
        msgs = [{"from": "alice"}, {"from": "bob"}, {"from": "the-operator"}]
        origins = mm_a._delivery_origins(mm_a.get("m"), msgs)
        assert origins["alice"] == ""         # alice's session runs here
        assert origins["bob"] == "pcB"        # bob's does not
        assert "the-operator" not in origins  # external send: no member row
        # the same batch read on the OTHER daemon flips the answer
        flipped = mm_b._delivery_origins(mm_b.get("m"), msgs)
        assert flipped["bob"] == "" and flipped["alice"] == "pcA"
        await mm_a.shutdown()
        await mm_b.shutdown()
        await mgr.shutdown_all()

    asyncio.run(run())


def test_format_delivery_marks_the_sender_machine():
    msgs = [
        {"id": "m1", "from": "alice", "to": "bob", "type": "say", "body": "hi"},
        {"id": "m2", "from": "carl", "to": "bob", "type": "say", "body": "yo"},
        {"id": "m3", "from": "ops", "to": "bob", "type": "say", "body": "hey"},
    ]
    block = format_delivery(
        "m", "bob", msgs, origins={"alice": "", "carl": "pcB"}
    )
    assert "machine: local" in block
    assert "machine: pcB (remote)" in block
    # the external sender has no member row, so it gets no line of its own
    assert block.count("machine:") == 2
    # a caller that asks nothing gets the block it always got
    assert "machine:" not in format_delivery("m", "bob", msgs)
