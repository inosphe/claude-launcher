"""Processes the daemon starts for a session and what they leave running
(claunch-ihwel): ``daemon/session_procs.py``, its hook in
``RestartClock._execute``, and the two routes the web UI calls."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from claude_launcher.daemon import cflow_clock, session_procs
from claude_launcher.daemon.session_procs import Proc, ProcTracker

SELF = 999


class Table:
    """A process table the test edits between snapshots."""

    def __init__(self, *procs: Proc) -> None:
        self.procs = {p.pid: p for p in procs}

    def add(self, pid, ppid, cmd="", ident=None, owner=None):
        self.procs[pid] = Proc(pid=pid, ppid=ppid, ident=ident or f"i{pid}", cmd=cmd or f"cmd{pid}",
                               owner=owner)

    def drop(self, pid):
        self.procs.pop(pid, None)

    def __call__(self):
        return dict(self.procs)


def _tracker(table, path=None, killed=None):
    return ProcTracker(path, snapshot_fn=table, self_pid=SELF,
                       kill_fn=(killed.append if killed is not None else lambda pids: None),
                       clock=lambda: 100.0)


def test_adopted_tree_is_tracked_and_survives_its_parent():
    t = Table()
    t.add(10, SELF, "sh -c restart")
    tr = _tracker(t)
    tr.adopt("s1", 10, label="restart:reflect")
    t.add(11, 10, "python restart_live.py")
    tr.refresh()
    t.add(12, 11, "npm run dev")
    tr.refresh()
    # The script and its shell exit; npm is reparented to init.
    t.drop(10)
    t.drop(11)
    t.procs[12] = Proc(pid=12, ppid=1, ident="i12", cmd="npm run dev")
    t.add(13, 12, "node vite")
    tr.refresh()
    rows = tr.listing("s1")
    assert [(r["pid"], r["cmd"], r["label"], r["children"]) for r in rows] == [
        (12, "npm run dev", "restart:reflect", 1)]


def test_a_recycled_pid_is_not_the_process_that_held_it():
    t = Table()
    t.add(10, SELF)
    tr = _tracker(t)
    tr.adopt("s1", 10, label="restart")
    t.procs[10] = Proc(pid=10, ppid=1, ident="other-start", cmd="unrelated")
    tr.refresh()
    assert tr.listing("s1") == []
    assert not tr.has_members()


def test_the_daemon_itself_and_what_it_hosts_are_never_members():
    """This repository's restart script runs `claunch daemon restart`; the
    next daemon (here: SELF) and its session terminals sit under the
    adopted shell and must not be listed as the session's leftovers."""
    t = Table()
    t.add(10, 1, "sh -c restart")
    tr = _tracker(t)
    tr.adopt("s1", 10, label="restart")
    t.procs[SELF] = Proc(pid=SELF, ppid=10, ident="d", cmd="python -m claude_launcher.daemon")
    t.add(500, SELF, "claude")
    tr.refresh()
    assert [r["pid"] for r in tr.listing("s1")] == [10]
    assert tr.listing("s1")[0]["children"] == 0


def test_a_detached_process_carrying_the_owner_tag_is_found():
    t = Table()
    t.add(10, SELF)
    tr = _tracker(t)
    tr.adopt("s1", 10, label="restart:reflect")
    t.add(40, 1, "node server.js", owner="s1")
    t.add(41, 1, "node other.js", owner="s2")
    tr.refresh()
    rows = tr.listing("s1")
    assert sorted(r["pid"] for r in rows) == [10, 40]
    assert {r["pid"]: r["label"] for r in rows}[40] == "restart:reflect"
    # The tag alone is enough: s2 has nothing adopted in this tracker.
    assert [(r["pid"], r["label"]) for r in tr.listing("s2")] == [(41, "")]


def test_a_tagged_leftover_outlives_the_command_that_started_it():
    """The shell starts a detached service and exits before any snapshot saw
    the two together: the service is found by its tag and keeps the label."""
    t = Table()
    t.add(10, SELF)
    tr = _tracker(t)
    tr.adopt("s1", 10, label="restart:reflect")
    t.drop(10)
    t.add(40, 1, "node server.js", owner="s1")
    tr.refresh()
    assert [(r["pid"], r["label"]) for r in tr.listing("s1")] == [(40, "restart:reflect")]
    t.drop(40)
    tr.refresh()
    assert tr.listing("s1") == [] and not tr.has_members()


def test_kill_ends_the_subtree_leaves_first_and_refuses_strangers():
    t = Table()
    t.add(10, SELF)
    killed = []
    tr = _tracker(t, killed=killed)
    tr.adopt("s1", 10, label="restart")
    t.add(11, 10)
    t.add(12, 11)
    t.add(77, 1)  # not a member
    tr.refresh()
    with pytest.raises(session_procs.NotAMember):
        tr.kill("s1", 77)
    with pytest.raises(session_procs.NotAMember):
        tr.kill("s2", 11)
    assert killed == []
    tr.kill("s1", 11)
    assert killed == [[12, 11]]


def test_members_persist_across_a_daemon_restart(tmp_path):
    t = Table()
    t.add(10, SELF, "npm run dev")
    path = tmp_path / "session_procs.json"
    tr = _tracker(t, path)
    tr.adopt("s1", 10, label="restart:reflect")
    again = _tracker(t, path)
    assert [(r["pid"], r["label"]) for r in again.listing("s1")] == [(10, "restart:reflect")]
    t.drop(10)
    again.refresh()
    assert _tracker(t, path).listing("s1") == []


def test_owner_tag_is_set_for_a_session_and_stripped_for_a_new_daemon():
    env = session_procs.owner_env({"A": "1"}, "s1")
    assert env[session_procs.OWNER_ENV] == "s1"
    assert session_procs.OWNER_ENV not in session_procs.owner_env(env, "")
    assert session_procs.OWNER_ENV not in session_procs.strip_owner(env)
    assert session_procs.strip_owner(env)["A"] == "1"


def test_spawn_daemon_does_not_hand_the_owner_tag_on(monkeypatch):
    from claude_launcher import daemon_client

    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw)
        raise OSError("not really")

    monkeypatch.setenv(session_procs.OWNER_ENV, "s1")
    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    with pytest.raises(OSError):
        daemon_client.spawn_daemon()
    assert session_procs.OWNER_ENV not in seen["env"]


# -- the /proc reader (claunch-b6mnl) ----------------------------------------


class FakeProc:
    """A /proc tree on disk: stat, cmdline, environ, task/<pid>/children."""

    def __init__(self, root):
        self.root = root
        self.opened = []

    def put(self, pid, ppid, *, comm="sh", start=1000, argv=("sh",), env=(), children=(),
            image=None):
        """``image`` is the stat line's arg_start, arg_end, env_start and
        env_end (fields 48-51, new with each exec); None writes the short
        line of a kernel before 3.5, without them."""
        base = self.root / str(pid)
        (base / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        rest = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5
        if image is not None:
            rest += ["0"] * 20 + [str(a) for a in image] + ["0"]
        (base / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest))
        (base / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (base / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
        (base / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))

    def drop(self, pid):
        import shutil
        shutil.rmtree(self.root / str(pid))

    def reader(self):
        r = session_procs._LinuxReader(str(self.root))
        r._children_ok = True
        return r


def _count_opens(monkeypatch):
    import builtins
    seen = []
    real = builtins.open

    def spy(path, *a, **kw):
        seen.append(os.path.basename(str(path)))
        return real(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", spy)
    return seen


def test_reader_reads_cmdline_and_environ_once_per_process_image(tmp_path, monkeypatch):
    fp = FakeProc(tmp_path)
    tag = f"{session_procs.OWNER_ENV}=s1"
    fp.put(10, 1, comm="node", argv=("node", "server.js"), env=("A=1", tag))
    fp.put(11, 1, comm="sh", argv=("sh",))
    r = fp.reader()
    first = r.snapshot()
    assert (first[10].cmd, first[10].owner, first[10].ident) == ("node server.js", "s1", "start:1000")
    assert (first[11].cmd, first[11].owner) == ("sh", None)
    opened = _count_opens(monkeypatch)
    again = r.snapshot()
    assert sorted(opened) == ["stat", "stat"]
    assert {pid: (p.cmd, p.owner, p.ident, p.ppid) for pid, p in again.items()} == {
        pid: (p.cmd, p.owner, p.ident, p.ppid) for pid, p in first.items()}


def test_reader_rereads_after_exec_and_for_a_recycled_pid(tmp_path):
    """exec keeps pid and start time; it replaces comm, cmdline and environ.
    A changed comm or start time makes the reader read the process again."""
    fp = FakeProc(tmp_path)
    fp.put(10, 1, comm="python3", argv=("python3", "-m", "x"))
    r = fp.reader()
    assert r.snapshot()[10].owner is None
    # exec into another program that carries the owner tag
    fp.put(10, 1, comm="node", argv=("node", "dev"), env=(f"{session_procs.OWNER_ENV}=s1",))
    after = r.snapshot()[10]
    assert (after.cmd, after.owner) == ("node dev", "s1")
    # the pid is recycled: same comm, another start time
    fp.put(10, 1, comm="node", start=2000, argv=("node", "other"))
    recycled = r.snapshot()[10]
    assert (recycled.cmd, recycled.owner, recycled.ident) == ("node other", None, "start:2000")


def test_reader_rereads_after_an_exec_that_keeps_comm(tmp_path):
    """sh -> sh keeps pid, start time and comm; the new image has its
    arguments and environment at new addresses, and the reader reads it
    again -- the owner tag that comes in with that exec too (claunch-d2pgg)."""
    fp = FakeProc(tmp_path)
    fp.put(10, 1, comm="sh", argv=("sh", "-c", "a"), image=(100, 110, 110, 200))
    r = fp.reader()
    assert r.snapshot()[10].cmd == "sh -c a"
    fp.put(10, 1, comm="sh", argv=("sh", "-c", "b"), image=(300, 310, 310, 400),
           env=(f"{session_procs.OWNER_ENV}=s1",))
    after = r.snapshot()[10]
    assert (after.cmd, after.owner) == ("sh -c b", "s1")


def test_reader_keeps_what_it_read_where_the_image_is_hidden(tmp_path, monkeypatch):
    """The kernel shows the addresses as 0 to a reader without ptrace access
    (another user's process, a non-dumpable one) and not at all before 3.5.
    The key is then pid, start time and comm, and an exec that keeps comm
    goes unseen. Such a process's environ is closed to the reader as well, so
    it never carries an owner tag the reader could see; what stays is the
    first command line. It stays cached: reading those processes again on
    every snapshot is the cost claunch-b6mnl took away."""
    fp = FakeProc(tmp_path)
    fp.put(10, 1, comm="sh", argv=("sh", "-c", "a"), image=(0, 0, 0, 0))
    fp.put(11, 1, comm="sh", argv=("sh", "-c", "a"))
    r = fp.reader()
    r.snapshot()
    fp.put(10, 1, comm="sh", argv=("sh", "-c", "b"), image=(0, 0, 0, 0))
    fp.put(11, 1, comm="sh", argv=("sh", "-c", "b"))
    opened = _count_opens(monkeypatch)
    again = r.snapshot()
    assert (again[10].cmd, again[11].cmd) == ("sh -c a", "sh -c a")
    assert sorted(opened) == ["stat", "stat"]
    # a new reader sees the new image
    assert fp.reader().snapshot()[10].cmd == "sh -c b"


def test_reader_does_not_keep_an_empty_command_line(tmp_path, monkeypatch):
    """An empty cmdline is a process in the middle of an exec (or a zombie):
    the answer '[comm]' is not remembered, and the next snapshot reads the
    process again. A kernel thread's cmdline is always empty; it is
    remembered."""
    fp = FakeProc(tmp_path)
    fp.put(10, 1, comm="python3", argv=(), image=(100, 110, 110, 200))
    fp.put(2, 0, comm="kthreadd", argv=())
    fp.put(30, 2, comm="kworker/0:1", argv=())
    r = fp.reader()
    first = r.snapshot()
    assert (first[10].cmd, first[2].cmd, first[30].cmd) == (
        "[python3]", "[kthreadd]", "[kworker/0:1]")
    fp.put(10, 1, comm="python3", argv=("python3", "-m", "x"), image=(100, 110, 110, 200),
           env=(f"{session_procs.OWNER_ENV}=s1",))
    opened = _count_opens(monkeypatch)
    after = r.snapshot()
    assert (after[10].cmd, after[10].owner) == ("python3 -m x", "s1")
    assert sorted(opened) == ["cmdline", "environ", "stat", "stat", "stat"]


def test_reader_sees_the_tag_the_daemon_puts_in_through_exec(tmp_path):
    fp = FakeProc(tmp_path)
    fp.put(10, 1, comm="python3", argv=("python3", "-m", "claude_launcher.daemon"))
    r = fp.reader()
    assert r.snapshot()[10].owner is None  # forked, not yet exec'd
    fp.put(10, 1, comm="sh", argv=("/bin/sh", "-c", "tools/restart_live.sh"),
           env=(f"{session_procs.OWNER_ENV}=s1",))
    assert r.snapshot()[10].owner == "s1"


def test_reader_forgets_processes_that_are_gone(tmp_path):
    fp = FakeProc(tmp_path)
    fp.put(10, 1)
    fp.put(11, 1)
    r = fp.reader()
    r.snapshot()
    fp.drop(11)
    assert sorted(r.snapshot()) == [10]
    assert sorted(r._known) == [10]


def test_reader_tree_reads_the_roots_and_what_is_under_them(tmp_path):
    fp = FakeProc(tmp_path)
    fp.put(10, 1, children=(11,))
    fp.put(11, 10, children=(12,))
    fp.put(12, 11)
    fp.put(20, 1)  # not under the roots
    r = fp.reader()
    assert sorted(r.tree([10, 99])) == [10, 11, 12]
    r._children_ok = False
    assert r.tree([10]) is None


def _tree_of(table):
    """A subtree reader over a test Table, as the /proc one answers."""
    def tree(roots):
        out, frontier = {}, list(roots)
        while frontier:
            pid = frontier.pop()
            proc = table.procs.get(pid)
            if proc is None or pid in out:
                continue
            out[pid] = proc
            frontier.extend(p.pid for p in table.procs.values() if p.ppid == pid)
        return out
    return tree


def test_refresh_tree_grows_and_drops_like_refresh():
    t = Table()
    t.add(10, SELF, "sh -c restart")
    tr = ProcTracker(None, snapshot_fn=t, tree_fn=_tree_of(t), self_pid=SELF,
                     kill_fn=lambda pids: None, clock=lambda: 100.0)
    tr.adopt("s1", 10, label="restart:reflect")
    t.add(11, 10, "python restart_live.py")
    tr.refresh_tree("s1")
    t.add(12, 11, "npm run dev")
    tr.refresh_tree("s1")
    t.drop(11)
    t.procs[12] = Proc(pid=12, ppid=1, ident="i12", cmd="npm run dev")
    t.add(40, 1, "detached", owner="s1")  # only a full refresh finds it by the tag
    tr.refresh_tree("s1")
    assert sorted(r["pid"] for r in tr.listing("s1")) == [10, 12]
    tr.refresh()
    assert sorted(r["pid"] for r in tr.listing("s1")) == [10, 12, 40]
    t.drop(10)
    t.drop(12)
    t.drop(40)
    tr.refresh_tree("s1")
    assert not tr.has_members()


def test_refresh_tree_reads_the_whole_table_where_it_cannot_read_a_subtree():
    t = Table()
    t.add(10, SELF)
    tr = _tracker(t)  # a test table and no subtree reader
    tr.adopt("s1", 10, label="restart")
    t.add(40, 1, owner="s1")
    tr.refresh_tree("s1")
    assert sorted(r["pid"] for r in tr.listing("s1")) == [10, 40]
    tr.refresh_tree("nobody")
    assert "nobody" not in tr.summary()


# -- real processes (Linux /proc) -------------------------------------------

linux_only = pytest.mark.skipif(not os.path.isdir("/proc/self") or sys.platform == "win32",
                                reason="reads /proc")


def _detached_sleeper_command() -> str:
    # A restart script that starts a service in a new session and exits,
    # as restart_live.py does with `npm run dev`.
    code = ("import subprocess, sys; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); print('service', p.pid, flush=True)")
    return f'"{sys.executable}" -c "{code}"'


@linux_only
def test_restart_command_leftover_is_listed_and_killed(tmp_path):
    # A session name of its own: what other tests' restart commands left
    # running carries their session's owner tag, and is found by it.
    action = {"command": _detached_sleeper_command(), "timeout": 20, "step": "reflect"}
    result = cflow_clock.RestartClock._execute(str(tmp_path), action, "s-leftover")
    service = int(result["output"].split("service", 1)[1].split()[0])
    try:
        assert result["exit_code"] == 0
        tr = session_procs.tracker()
        tr.refresh()
        rows = tr.listing("s-leftover")
        assert service in [r["pid"] for r in rows]
        row = next(r for r in rows if r["pid"] == service)
        assert row["label"] == "restart:reflect"
        assert "time.sleep(60)" in row["cmd"]
        tr.kill("s-leftover", service)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and session_procs._alive(service):
            time.sleep(0.05)
        assert not session_procs._alive(service)
        assert tr.listing("s-leftover") == []
    finally:
        try:
            os.kill(service, signal.SIGKILL)
        except OSError:
            pass


@linux_only
def test_a_nameless_run_is_not_tracked(tmp_path):
    action = {"command": f'"{sys.executable}" -c "print(1)"', "timeout": 10}
    cflow_clock.RestartClock._execute(str(tmp_path), action, "")
    assert not session_procs.tracker().has_members()


@linux_only
def test_reader_sees_a_real_exec():
    proc = subprocess.Popen([sys.executable, "-c",
                             "import os, sys; print('ready', flush=True); sys.stdin.readline(); "
                             "os.execv('/bin/sleep', ['sleep', '30'])"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        # Read only once the child runs its own code: right after Popen it
        # may still be in the exec of the interpreter (claunch-d2pgg).
        assert proc.stdout.readline() == b"ready\n"
        r = session_procs._LinuxReader()
        before = r.tree([proc.pid])[proc.pid]
        assert "execv" in before.cmd
        proc.stdin.write(b"go\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 5
        after = before
        while time.monotonic() < deadline and after.cmd == before.cmd:
            time.sleep(0.05)
            after = r.snapshot()[proc.pid]
        assert after.cmd == "sleep 30"
        assert after.ident == before.ident
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_reader_sees_an_exec_into_the_same_program():
    """A real python -> python exec: pid, start time and comm stay, and the
    reader still answers the new command line and environment."""
    tag = f"{session_procs.OWNER_ENV}=s-after"
    code = ("import os, sys; print('ready', flush=True); sys.stdin.readline(); "
            "env = dict(os.environ); env[%r] = %r; "
            "os.execve(sys.executable, [sys.executable, '-c', "
            "\"import sys; print('exec', flush=True); sys.stdin.readline()\", 'after-exec'], env)"
            % tuple(tag.split("=", 1)))
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    try:
        assert proc.stdout.readline() == b"ready\n"
        r = session_procs._LinuxReader()
        before = r.tree([proc.pid])[proc.pid]
        assert "readline" in before.cmd and before.owner is None
        proc.stdin.write(b"go\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == b"exec\n"
        after = r.tree([proc.pid])[proc.pid]
        assert after.ident == before.ident
        assert after.cmd.endswith("after-exec")
        assert after.owner == "s-after"
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_tracking_is_the_same_with_the_cached_reader(tmp_path):
    """Over a real process tree, the cached whole-table read, the subtree
    read and a fresh (uncached) reader give the tracker the same members."""
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "subprocess.Popen(['sh', '-c', 'exec sleep 30']); time.sleep(30)")
    env = session_procs.owner_env(dict(os.environ), "s-same")
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kids = [p for p in session_procs.snapshot().values() if p.ppid == proc.pid]
            if len(kids) == 2 and all("sleep" in p.cmd for p in kids):
                break
            time.sleep(0.05)
        views = []
        for snap_fn, how in ((session_procs.snapshot, "tree"),
                             (session_procs.snapshot, "full"),
                             (lambda: session_procs._LinuxReader().snapshot(), "fresh")):
            tr = ProcTracker(None, snapshot_fn=snap_fn, kill_fn=lambda pids: None,
                             clock=lambda: 1.0)
            # adopt from a table holding only the command, so the members
            # under it come from the read being compared
            root = session_procs.snapshot()[proc.pid]
            tr.adopt("s-same", proc.pid, label="restart:x", snap={proc.pid: root})
            assert list(tr._members["s-same"]) == [proc.pid]
            if how == "tree":
                tr.refresh_tree("s-same")
            else:
                tr.refresh()
            members = tr._members["s-same"]
            views.append(sorted((pid, m["ppid"], m["ident"], m["cmd"], m["label"])
                                for pid, m in members.items()))
        assert len(views[0]) == 3
        assert views[0] == views[1] == views[2]
    finally:
        for p in session_procs.snapshot().values():
            if p.owner == "s-same" and p.pid != proc.pid:
                try:
                    os.kill(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        proc.kill()
        proc.wait()


# -- the routes --------------------------------------------------------------

@linux_only
def test_procs_routes(home, tmp_path):
    import asyncio
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdin=subprocess.DEVNULL)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                stdin=subprocess.DEVNULL)
    session_procs.tracker().adopt("a", sleeper.pid, label="restart:reflect", command="sleep")

    async def run():
        manager = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=False)
        known = {"a": SimpleNamespace(sdef=SimpleNamespace(name="a", cwd=str(tmp_path)))}
        real_get = manager.get
        manager.get = lambda name: known[name] if name in known else real_get(name)
        app = build_app(manager, "secret", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        headers = {"Authorization": "Bearer secret"}
        try:
            assert (await client.get("/api/sessions/a/procs")).status == 401
            body = await (await client.get("/api/sessions/a/procs", headers=headers)).json()
            assert [r["pid"] for r in body["procs"]] == [sleeper.pid]
            refused = await client.post(f"/api/sessions/a/procs/{stranger.pid}/kill", headers=headers)
            assert refused.status == 404
            assert stranger.poll() is None
            bad = await client.post("/api/sessions/a/procs/x/kill", headers=headers)
            assert bad.status == 400
            resp = await client.post(f"/api/sessions/a/procs/{sleeper.pid}/kill", headers=headers)
            assert resp.status == 200
            body = await resp.json()
            assert body["killed"] == [sleeper.pid]
        finally:
            await client.close()
            await manager.shutdown_all()

    try:
        asyncio.run(run())
        assert sleeper.wait(timeout=5) is not None
    finally:
        for p in (sleeper, stranger):
            p.kill()
            p.wait()
