"""A failed or downgraded session creation reaches the person, once.

The two acceptance readings of claunch-is4o4:

- one failed creation makes one ``warning`` event, and
- the same target failing for the same reason is reported once.

The surface is the session event history the daemon already keeps
(``daemon/session_events.py``) plus the ``warn`` notice the person at that
session sees; ``daemon/spawn_events.py`` says why those two and no new one.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from claude_launcher import spawn as spawn_mod
from claude_launcher.daemon import cflow2_clock, cflow2_exec, onboard, session_events, spawn_events
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import MeshManager
from test_spawn_api import BEARER, _manager, _register_py_harness, _serve


@pytest.fixture(autouse=True)
def isolated_reporter():
    """The reporter is process-wide (``spawn_events.install``); a test that
    leaves its own installed would answer for the next one."""
    previous = spawn_events.installed()
    yield
    spawn_events.install(previous)


def _events(tmp_path):
    return session_events.Events(tmp_path)


def reporting(tmp_path, *names):
    """A real ``Failures`` installed for the process, and a reader for what it
    recorded under ``names``.

    A call site's own report is the thing under test there: the site is the
    line that can be deleted without another test noticing (s511-rev), so the
    tests that drive a call site install this and read the events back
    instead of asserting on a stub. The autouse fixture above puts the
    previous reporter back.
    """
    events = session_events.Events(tmp_path)
    live = {
        name: SimpleNamespace(sdef=SimpleNamespace(name=name), created_at="first",
                              notify=lambda *a, **kw: 0)
        for name in names
    }
    spawn_events.install(spawn_events.Failures(events, live.get, clock=lambda: 100.0))

    def rows(name):
        return [e for e in events.rows(live[name]) if e["kind"] in ("fail", "fallback")]

    return events, rows


def _session(name="s1"):
    return SimpleNamespace(sdef=SimpleNamespace(name=name), created_at="first",
                           notify=lambda *a, **kw: 0)


def test_one_failed_creation_is_one_warning_event(tmp_path):
    session = _session()
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    event = failures.report("s1", reason="session 's1' is in 2 meshes", site="api.spawn")

    assert [e["id"] for e in events.rows(session)] == [event["id"]]
    assert event["kind"] == "fail"
    assert event["details"]["level"] == "warning"
    assert event["details"]["reason"] == "session 's1' is in 2 meshes"
    assert event["details"]["site"] == "api.spawn"
    assert event["details"]["target"] == "s1"
    assert event["at"], "the event carries its own time"
    assert "session 's1' is in 2 meshes" in event["text"]


def test_repeated_failure_of_the_same_reason_is_reported_once(tmp_path):
    session = _session()
    events = _events(tmp_path)
    now = [100.0]
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: now[0])

    first = failures.report("s1", reason="refused: no such workflow", site="api.spawn")
    for _ in range(4):
        now[0] += 60.0
        assert failures.report("s1", reason="refused: no such workflow",
                               site="api.spawn") is None
    assert [e["id"] for e in events.rows(session)] == [first["id"]]

    # Past the gap the same failure is worth one line again, and it says how
    # many were folded into the report before it.
    now[0] += failures.gap + 1
    later = failures.report("s1", reason="refused: no such workflow", site="api.spawn")
    assert later is not None and later["details"]["repeats"] == 4
    assert "4회 더" in later["text"]

    # A different reason on the same target is its own report.
    other = failures.report("s1", reason="no such harness", site="api.spawn")
    assert other is not None and other["id"] != later["id"]


def test_the_route_and_its_http_caller_name_the_same_failure(tmp_path):
    """The route answers an error body; ``daemon_client`` shows its caller
    the same sentence behind ``POST <path>: ``. Both must key as one."""
    session = _session()
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    refused = failures.report("s1", reason="session 's1' is in 2 meshes (a, b)", site="api.spawn")
    assert refused is not None
    assert failures.report(
        "s1", reason="POST /api/sessions/s1/children: session 's1' is in 2 meshes (a, b)",
        site="cflow2.spawn") is None
    assert len(events.rows(session)) == 1


def test_notice_goes_to_the_session_a_person_is_looking_at(tmp_path):
    seen = []

    def viewer(name):
        return SimpleNamespace(
            sdef=SimpleNamespace(name=name), created_at="now",
            notify=lambda text, **kw: seen.append((name, text, kw)),
        )

    child, parent = viewer("child"), viewer("parent")
    failures = spawn_events.Failures(
        _events(tmp_path), lambda name: {"child": child, "parent": parent}[name],
        clock=lambda: 100.0)

    failures.report("child", kind="fallback", reason="a mesh was opened for it",
                    site="onboard.inherit_mesh", notify_on="parent")

    assert [name for name, _, _ in seen] == ["parent"], "the child has no viewer yet"
    _, text, kwargs = seen[0]
    assert "fallback" in text and kwargs["level"] == "warn"


def test_a_refused_child_spawn_lands_a_warning_on_the_parent(home, tmp_path, monkeypatch):
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        notices = []
        monkeypatch.setattr(parent, "notify", lambda text, **kw: notices.append(text))
        # The child's name is the parent's: stage refuses it, so the spawn
        # answers 409 -- a refused creation and nothing else.
        body = {"name": "parent", "task": "child work", "beads": False}
        try:
            first = await client.post("/api/sessions/parent/children", headers=BEARER, json=body)
            assert first.status == 409, await first.text()
            again = await client.post("/api/sessions/parent/children", headers=BEARER, json=body)
            assert again.status == 409, await again.text()
        finally:
            await client.close()
        return mgr, parent, notices

    mgr, parent, notices = asyncio.run(run())
    rows = [e for e in mgr.events.rows(parent) if e["kind"] == "fail"]
    assert len(rows) == 1, "one failure is one event; the repeat adds none"
    assert rows[0]["details"]["level"] == "warning"
    assert rows[0]["details"]["site"] == "api.spawn"
    assert rows[0]["details"]["status"] == 409
    assert "already exists" in rows[0]["details"]["reason"]
    assert len(notices) == 1 and "세션 생성 실패" in notices[0]


def test_opening_a_mesh_for_a_meshless_parent_is_reported(home, tmp_path, monkeypatch):
    """The fallback branch of ``inherit_mesh`` (claunch-is4o4 addendum): a
    parent in no mesh gets one opened for it and its children, and the user
    hears about the reshaped topology."""
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        notices = []
        monkeypatch.setattr(parent, "notify", lambda text, **kw: notices.append(text))
        body = {}
        await onboard.inherit_mesh(body, parent="parent", mesh_mgr=mm, child="child")
        return mgr, parent, notices, body

    mgr, parent, notices, body = asyncio.run(run())
    assert body["mesh"] == "parent"
    rows = [e for e in mgr.events.rows(parent) if e["kind"] == "fallback"]
    assert len(rows) == 1
    assert rows[0]["details"]["mesh"] == "parent"
    assert rows[0]["details"]["child"] == "child"
    assert rows[0]["details"]["site"] == "onboard.inherit_mesh"
    assert len(notices) == 1 and "fallback" in notices[0]


# -- the call sites themselves --------------------------------------------------
#
# Each of these drives one reporting site with the code that runs in the
# daemon and reads the event back. They exist because a call site can be
# deleted without another test noticing (s511-rev, s597-rev: with the report
# removed at each of these points the suite stayed green), and because
# ``spawn_events.report`` swallows its own errors by design -- so the absence
# of a report is silent everywhere except here.


def test_a_session_delegation_the_engine_cannot_spawn_is_reported(tmp_path, monkeypatch):
    """``cflow2_exec.spawn_session``: the delegation behind claunch-ull6m. The
    engine reroutes it to a sub-agent, and this event is the only thing the
    person hears."""
    from test_cflow2_clock import SESSION, SESSION_CAPS, FakeManager, FakeSession, _clock, _start

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _, rows = reporting(tmp_path, "own")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def spawn(parent_name, body):
        raise cflow2_exec.SpawnRefused("spawn policy: max_children reached")

    clock = _clock(FakeManager(FakeSession("own", str(run_dir))), spawn=spawn)
    clock.scan()
    _start(run_dir, SESSION, caps=SESSION_CAPS)
    clock.scan()

    recorded = rows("own")
    assert len(recorded) == 1
    assert recorded[0]["details"]["site"] == "cflow2.spawn"
    assert recorded[0]["details"]["level"] == "warning"
    assert recorded[0]["details"]["target"] == "own"
    assert "max_children reached" in recorded[0]["details"]["reason"]
    assert recorded[0]["details"]["executor"] == "session"


def test_a_fork_delegation_the_engine_cannot_fork_is_reported(tmp_path, monkeypatch):
    """``cflow2_exec._spawn_fork``: same invisibility one route over."""
    from test_cflow2_fork_executor import FakeManager, FakeSession, _fork_members, _start

    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _, rows = reporting(tmp_path, "own")

    def refuse(parent, body):
        raise cflow2_exec.SpawnRefused("spawn policy: max_children reached")

    clock = cflow2_clock.Cflow2Clock(
        FakeManager(FakeSession("own", str(tmp_path))), poll=5, fork=refuse,
        context_left=lambda s: (500_000, "stub"), fork_min_context=100_000,
        probe_runner=None, threaded=False)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    clock.scan()

    recorded = rows("own")
    assert len(recorded) == 1
    assert recorded[0]["details"]["site"] == "cflow2.fork"
    assert recorded[0]["details"]["level"] == "warning"
    assert recorded[0]["details"]["executor"] == "fork"
    assert "max_children reached" in recorded[0]["details"]["reason"]


def test_a_watcher_spawn_that_fails_is_reported_to_the_job_owner(tmp_path, monkeypatch):
    """``delayed_sweep.Clock.attend``: the retry loop claunch-k3acp measured at
    34 failures a minute. One event on the owner, and the retry after
    ``SPAWN_BACKOFF`` folds into it."""
    from claude_launcher.daemon import paths as daemon_paths
    from test_delayed_sweep import Clock, git as run_git, live_manager, merges, queue, spawner

    monkeypatch.setenv(daemon_paths.INSTANCE_BASE_ENV, str(tmp_path / ".home"))
    monkeypatch.setenv(daemon_paths.INSTANCE_ENV, "")
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "test@example.com"),
                 ("config", "user.name", "test")):
        run_git(repo, *args)
    (repo / "base").write_text("base")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "base")
    run_git(repo, "branch", "-M", "master")

    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))
    calls, spawn = spawner([], fail=True)

    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    _, rows = reporting(tmp_path, "leader")
    clock = Clock(live_manager(), q, batch=True, spawn=spawn, policy=(5, 3600))
    asyncio.run(clock.tick())
    asyncio.run(clock.tick())  # backed off: no second attempt, no second report

    recorded = rows("leader")
    assert len(recorded) == 1
    assert recorded[0]["details"]["site"] == "delayed_sweep.attend"
    assert recorded[0]["details"]["permanent"] is False
    assert recorded[0]["details"]["level"] == "warning"
    assert recorded[0]["details"]["target"] == "leader"
    assert recorded[0]["details"]["job"] == jobs[-1]["id"]
    assert recorded[0]["details"]["sha"] == jobs[-1]["sha"]
    assert "daemon refused" in recorded[0]["details"]["reason"]


def test_a_refused_quick_fork_lands_a_warning_on_the_origin(home, tmp_path, monkeypatch):
    """``api.h_session_quick_fork``: the route a cflow ``fork`` delegation and
    ``deep_think`` reach. Refused twice, reported once."""
    _register_py_harness()
    monkeypatch.setattr(spawn_mod, "can_fork", lambda parent: False)

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            origin = mgr.stage(SessionDef(name="a", harness="py", cwd=str(tmp_path)))
            for _ in range(2):
                resp = await client.post("/api/sessions/a/quick-fork", headers=BEARER)
                assert resp.status == 400, await resp.text()
        finally:
            await client.close()
        return mgr, origin

    mgr, origin = asyncio.run(run())
    recorded = [e for e in mgr.events.rows(origin) if e["kind"] == "fail"]
    assert len(recorded) == 1
    assert recorded[0]["details"]["site"] == "api.quick_fork"
    assert recorded[0]["details"]["level"] == "warning"
    assert recorded[0]["details"]["status"] == 400
    assert "no conversation to copy" in recorded[0]["details"]["reason"]


def test_a_permanent_failure_reports_once_per_gap_not_once_per_attempt(tmp_path):
    """The documented policy (s597-rev, s511-rev asked whether it is meant):
    a refusal that never stops is one event, then one more per
    ``SAME_REASON_GAP``, each carrying how many attempts it folded. Silence
    after the first would read as a failure that had stopped."""
    session = _session()
    events = _events(tmp_path)
    now = [100.0]
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: now[0])

    for _ in range(12):  # an hour of attempts, one every five minutes
        failures.report("leader", reason="the daemon refused", site="delayed_sweep.attend")
        now[0] += 300.0

    rows = events.rows(session)
    assert len(rows) == 4, "four rows an hour, not twelve"
    assert [r["details"]["repeats"] for r in rows] == [0, 2, 2, 2]


def test_the_routes_refusal_and_the_runs_report_are_one_event(home, tmp_path, monkeypatch):
    """The fold across the two real paths, not a unit of ``normalize`` alone.

    The route reports what it answered; the run engine reaches the same
    refusal over HTTP and ``daemon_client`` hands it back as ``POST <path>:
    <error body>`` (``daemon_client.py:134``), which ``cflow2_exec`` reports
    again. One refusal, one event -- the reason ``normalize`` exists.
    """
    _register_py_harness()

    async def run():
        mgr = _manager()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        body = {"name": "parent", "task": "child work", "beads": False}
        try:
            resp = await client.post("/api/sessions/parent/children", headers=BEARER, json=body)
            assert resp.status == 409, await resp.text()
            error = (await resp.json())["error"]
        finally:
            await client.close()
        return mgr, parent, error

    mgr, parent, error = asyncio.run(run())
    # What cflow2_exec reports after default_spawn turns the client's error
    # into a SpawnRefused (cflow2_exec.default_spawn -> DaemonClientError).
    spawn_events.report(
        "parent", kind="fail",
        reason=f"POST /api/sessions/parent/children: {error}", site="cflow2.spawn",
        delegation="d1", scope="parent", executor="session")

    recorded = [e for e in mgr.events.rows(parent) if e["kind"] == "fail"]
    assert len(recorded) == 1, "the route's report and the engine's are one failure"
    assert recorded[0]["details"]["site"] == "api.spawn", "the first report names it"


# -- the two guards the review found unguarded ---------------------------------


def test_a_store_that_cannot_write_does_not_become_a_second_failure(tmp_path):
    """``events.record`` is guarded like ``_notice`` is (s1389-rev, s1262-rev).

    A report runs on the way out of a failure: the route's 4xx response, the
    sweep's pass, the run engine's reroute. If the history store raised here,
    the first would answer 500, the second would end the pass, and the third
    would take the store's error as the spawn's.
    """
    notices = []
    session = SimpleNamespace(
        sdef=SimpleNamespace(name="s1"), created_at="first",
        notify=lambda text, **kw: notices.append((text, kw)))

    class Broken:
        def record(self, *args, **kwargs):
            raise OSError("no space left on device")

    failures = spawn_events.Failures(Broken(), lambda name: session, clock=lambda: 100.0)

    assert failures.report("s1", reason="refused", site="api.spawn") is None
    assert len(notices) == 1 and "세션 생성 실패: refused" in notices[0][0]
    assert notices[0][1]["level"] == "warn"


def test_a_report_from_a_worker_thread_shows_the_notice_on_the_loop(tmp_path):
    """The cflow2 clock runs the executor on ``pools.background``, so its
    report -- and ``Session.notify``'s fan-out to asyncio queues -- happens
    off the loop (s490-rev). The notice is handed to the session's loop."""
    _, _rows = reporting(tmp_path, "own")  # the autouse fixture restores ours

    async def run():
        loop = asyncio.get_running_loop()
        seen = []
        session = SimpleNamespace(
            sdef=SimpleNamespace(name="own"), created_at="t", _loop=loop,
            notify=lambda text, **kw: seen.append((threading.current_thread(), text, kw)))
        failures = spawn_events.Failures(_events(tmp_path / "events"), lambda n: session,
                                         clock=lambda: 100.0)
        worker, _ = await asyncio.to_thread(
            lambda: (threading.current_thread(),
                     failures.report("own", reason="refused", site="cflow2.spawn")))
        for _ in range(5):
            await asyncio.sleep(0)
        return worker, seen

    worker, seen = asyncio.run(run())
    assert worker is not threading.main_thread(), "the report ran off the loop"
    assert len(seen) == 1, "the notice still arrives"
    assert seen[0][0] is threading.main_thread(), "it is drawn by the loop, not the worker"
    assert seen[0][2]["level"] == "warn"


def test_a_refusal_that_names_no_reason_is_still_one_report(tmp_path):
    """When a route answers an error with no ``error`` field the route reports
    an empty reason and the caller reads ``HTTP <code>``
    (``daemon_client._error_detail``). Both mean the same refusal (s490-rev)."""
    session = _session()
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    assert failures.report("s1", reason="", site="api.spawn") is not None
    assert failures.report("s1", reason="POST /api/sessions/s1/children: HTTP 500",
                           site="cflow2.spawn") is None
    assert failures.report("s1", reason="HTTP 502", site="cflow2.spawn") is None
    assert len(events.rows(session)) == 1
    assert events.rows(session)[0]["details"]["reason"] == spawn_events.NO_REASON


def test_a_failure_already_told_another_way_keeps_the_event_but_not_the_line(tmp_path):
    """Two reporters meet on one permanent refusal (claunch-k3acp +
    claunch-is4o4): the sweep types a block into the owner's terminal itself,
    so the transient line is suppressed while the durable event stays.

    The history is the half with no substitute -- a typed block scrolls away,
    and a retry loop that never becomes permanent has no block at all, which
    is why the default is to show the line."""
    seen = []
    session = SimpleNamespace(
        sdef=SimpleNamespace(name="owner"), created_at="t",
        notify=lambda text, **kw: seen.append(text))
    events = _events(tmp_path)
    failures = spawn_events.Failures(events, lambda name: session, clock=lambda: 100.0)

    event = failures.report("owner", reason="denied", site="delayed_sweep.attend",
                            notice=False)
    assert event is not None, "the event is the part that has no substitute"
    assert event["details"]["level"] == "warning"
    assert seen == [], "the caller already put the refusal in front of the person"

    failures.report("owner", reason="a different refusal", site="delayed_sweep.attend")
    assert len(seen) == 1, "every other failure still shows its line"


def _sweep_repo(tmp_path, run_git):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "test@example.com"),
                 ("config", "user.name", "test")):
        run_git(repo, *args)
    (repo / "base").write_text("base")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "base")
    run_git(repo, "branch", "-M", "master")
    return repo


def test_a_permanent_refusal_is_one_event_per_generation(tmp_path, monkeypatch):
    """The typed block and the durable event ride ONE condition.

    ``Clock._report_spawn_failure`` tells the owner once per
    ``(job id, generation)``; the event is emitted from inside that guard so
    the two cannot drift: a repeat inside one generation adds neither.

    A ``retry-watcher`` bump opens that guard and the report goes out again,
    but the row still folds -- a generation bump inside the window is the
    same situation, and the issue asks for one row per refusal, not one per
    attempt. The fold is the only thing keeping the route's row and the
    sweep's row to one, so it cannot be opted out of (s5808-rev measured what
    an opt-out costs); the count surfaces when the window turns over."""
    from claude_launcher.daemon import cflow2_exec
    from claude_launcher.daemon import paths as daemon_paths
    from test_delayed_sweep import Clock, git as run_git, live_manager, merges, queue, spawner

    monkeypatch.setenv(daemon_paths.INSTANCE_BASE_ENV, str(tmp_path / ".home"))
    monkeypatch.setenv(daemon_paths.INSTANCE_ENV, "")
    repo = _sweep_repo(tmp_path, run_git)
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))

    def refuse(parent, body):
        raise cflow2_exec.SpawnRefused("spawn policy: max_children reached",
                                       permanent=True)

    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    _, rows = reporting(tmp_path, "leader")
    clock = Clock(live_manager(), q, batch=True, spawn=refuse, policy=(5, 3600))
    asyncio.run(clock.tick())

    recorded = rows("leader")
    assert len(recorded) == 1
    assert recorded[0]["details"]["permanent"] is True
    assert recorded[0]["details"]["site"] == "delayed_sweep.attend"
    assert recorded[0]["details"]["job"] == jobs[-1]["id"]
    assert "max_children" in recorded[0]["details"]["reason"]

    # A second pass makes no second attempt (the job is marked permanent), so
    # neither the block nor the event repeats.
    asyncio.run(clock.tick())
    assert len(rows("leader")) == 1

    # ``retry-watcher`` is a person asking again: it bumps the generation, so
    # the guard opens and the block is typed again. The event folds into the
    # row above -- inside the window one refusal is still one situation, and
    # a second row here would be the duplicate the issue is about.
    q.retry_watcher(jobs[-1]["id"])
    async def launch_again(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch_again)
    clock.spawn_tried.clear()
    asyncio.run(clock.tick())
    assert len(rows("leader")) == 1, "the bump folds; the row count does not double"


def test_a_refusal_seen_by_the_route_and_the_sweep_is_one_event(tmp_path, monkeypatch):
    """One permanent refusal, reached twice, is one row (s5808-rev, claunch-is4o4).

    The sweep's default spawn is ``cflow2_exec.default_spawn``, which POSTs to
    this daemon's own ``/api/sessions/{parent}/children``. A refusal therefore
    passes the route's reporter first (``api.spawn``) and ``Clock._report_spawn_failure``
    second (``delayed_sweep.attend``). Those are two call sites for one
    failure, and what keeps it one row is the 900-second fold -- so the
    permanent path must not turn the fold off. Turning it off also clears the
    key from ``seen``, which makes the outer report believe it is the first
    and write a second row next to the route's.

    The other permanent-refusal test injects ``spawn=`` and so never crosses
    the route; this one is the shape that actually happens in the daemon."""
    from claude_launcher.daemon import paths as daemon_paths
    from test_delayed_sweep import Clock, git as run_git, live_manager, merges, queue

    monkeypatch.setenv(daemon_paths.INSTANCE_BASE_ENV, str(tmp_path / ".home"))
    monkeypatch.setenv(daemon_paths.INSTANCE_ENV, "")
    repo = _sweep_repo(tmp_path, run_git)
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))

    reason = "session 'leader' already exists"
    _, rows = reporting(tmp_path, "leader")

    # 1. The route reports the refusal, as ``default_spawn`` would see it.
    spawn_events.report("leader", reason=reason, site="api.spawn")

    # 2. The sweep reports the same refusal for the same job.
    def refuse(parent, body):
        raise cflow2_exec.SpawnRefused(reason, permanent=True)

    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    clock = Clock(live_manager(), q, batch=True, spawn=refuse, policy=(5, 3600))
    asyncio.run(clock.tick())

    recorded = rows("leader")
    assert len(recorded) == 1, (
        "one refusal reached the route and the sweep; the fold is what makes "
        f"it one row, got {[e['details']['site'] for e in recorded]}")
    assert recorded[0]["details"]["site"] == "api.spawn", (
        "the route reported first; the sweep's copy folds into it")


def test_a_long_reason_still_folds_the_route_and_the_sweep_into_one_row(tmp_path, monkeypatch):
    """Truncation must not split the fold key (s5808-rev, claunch-is4o4).

    ``_one_line`` cuts at 300 characters *before* ``normalize`` strips the
    ``POST <path>: `` prefix, so a refusal longer than the limit keys 300
    characters on the route side and 264 on the sweep side -- two rows for one
    refusal. The sweep therefore reports the untruncated sentence and lets
    ``normalize`` truncate once, on the same text the route saw."""
    from claude_launcher.daemon import paths as daemon_paths
    from test_delayed_sweep import Clock, git as run_git, live_manager, merges, queue

    monkeypatch.setenv(daemon_paths.INSTANCE_BASE_ENV, str(tmp_path / ".home"))
    monkeypatch.setenv(daemon_paths.INSTANCE_ENV, "")
    repo = _sweep_repo(tmp_path, run_git)
    q = queue(tmp_path)
    jobs = merges(q, repo, list("abcde"))

    long_reason = "session 'leader' already exists: " + ("x" * 600)
    _, rows = reporting(tmp_path, "leader")

    # The route reports what the body said, untruncated.
    spawn_events.report("leader", reason=long_reason, site="api.spawn")

    def refuse(parent, body):
        raise cflow2_exec.SpawnRefused(long_reason, permanent=True)

    async def launch(*args, **kwargs):
        return SimpleNamespace(pid=os.getpid(), returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    clock = Clock(live_manager(), q, batch=True, spawn=refuse, policy=(5, 3600))
    asyncio.run(clock.tick())

    recorded = rows("leader")
    assert len(recorded) == 1, (
        "a long reason keys the same on both sides; truncation happens once, "
        f"inside normalize, got {[e['details']['site'] for e in recorded]}")


def test_a_report_that_raises_does_not_turn_the_refusal_into_500(
        home, tmp_path, monkeypatch, caplog):
    """``_report_spawn_failure``'s own guard, which nothing pinned (claunch-3t1wd).

    The report runs *after* the route has decided its answer (``api.py``
    ``_spawn_child_reported``): the 4xx is already computed, and only then is
    the refusal recorded. A report that raised there would replace a 409 the
    client can act on -- "a session by that name exists" -- with a 500 that
    says nothing and reads as the daemon's fault.

    The store's own guard is a different one and already tested:
    ``test_a_store_that_cannot_write_does_not_become_a_second_failure`` covers
    ``Failures.report``'s internals (``events.record``, ``_notice``). This
    measures the caller's ``except`` around ``failures.report`` itself, so the
    stub raises from ``report`` rather than from anything it calls -- the one
    way to reach that ``except``.

    Mutation: deleting the ``try``/``except`` at ``api.py``
    ``_spawn_child_reported``'s callee (``_report_spawn_failure``) makes this
    red with a 500, because aiohttp turns the escaped exception into one.
    """
    _register_py_harness()

    def refuse(*args, **kwargs):
        raise OSError("the board went away")

    async def run():
        mgr = _manager()
        # The route reaches the manager's reporter; give it one that raises.
        monkeypatch.setattr(mgr, "spawn_failures",
                            SimpleNamespace(report=refuse), raising=False)
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        parent = mgr.stage(SessionDef(name="parent", harness="py", cwd=str(tmp_path)))
        try:
            resp = await client.post(
                "/api/sessions/parent/children", headers=BEARER,
                json={"name": "parent", "task": "child work", "beads": False})
            status = resp.status
            # aiohttp answers an escaped exception with text/plain, so reading
            # the body first would fail on the JSON decode instead of on the
            # status that says what went wrong.
            error = (await resp.json()).get("error") if status == 409 else await resp.text()
        finally:
            await client.close()
        return mgr, parent, status, error

    mgr, parent, status, error = asyncio.run(run())

    assert status == 409, (
        "the route's own answer must survive its report failing, "
        f"got {status}")
    assert "already exists" in error
    assert "could not report the refused spawn" in caplog.text, (
        "the failure was reported to the log rather than swallowed silently")
    # And the route reported what it answered, from its own site.
    assert [e for e in mgr.events.rows(parent) if e["kind"] == "fail"] == [], (
        "the stub replaced the reporter, so this route added no row of its own")
