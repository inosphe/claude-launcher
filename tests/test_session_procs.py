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
