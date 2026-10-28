"""Failed and delayed sessions (claunch-0j6gb.1, claunch-0j6gb.2).

On 2026-10-13 a daemon restart relaunched 51 sessions at once and seven codex
sessions died inside the burst (``account/read failed during TUI bootstrap``),
filed as killed with their issues swept back to open. Two changes answer it:

* an exit nobody asked for, with a nonzero code, is filed *failed* — its own
  partition. Its exit hooks run, except the board sweep: the issue stays
  in_progress until a person settles the record (kill, archive, remove);
* the boot's restore files each session that would come back with its
  conversation as *delayed* — a hold the restore queue owns — and the queue
  relaunches them in priority order a few seconds apart, while any use wakes
  one at once through the hold's own wake paths.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from claude_launcher import store
from claude_launcher.daemon import restore_queue
from claude_launcher.daemon import resume as resume_mod
from claude_launcher.daemon import session as session_mod
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import RESTORE_QUEUE, SessionManager
from claude_launcher.daemon.mesh import CURRENT_CATEGORIES, member_in_state
from claude_launcher.daemon.session import DeadSession
from test_daemon_e2e import _manager, _wait_screen

STAMP = "2026-10-13T08:59:28+00:00"

#: Prints READY, then exits 3 on the first line it reads: a harness whose
#: program ends by itself with an error.
FAILING = (
    "import sys\n"
    "print('READY')\n"
    "sys.stdin.readline()\n"
    "sys.exit(3)\n"
)


def _register_failing_harness() -> None:
    store.update(lambda doc: doc.setdefault("harnesses", {}).update(
        {"pyfail": {"command": [sys.executable, "-u", "-c", FAILING]}}))


def _staggered() -> SessionManager:
    return SessionManager(idle_threshold=0.5, scrollback=200,
                          restore_default=True, restore_stagger=0.01)


def _owed(mgr: SessionManager, name: str, *, parent=None, output=None,
          harness="codex", conversation_id="rollout-1") -> DeadSession:
    """A record the next boot owes a relaunch (still delayed at shutdown)."""
    record = DeadSession(
        SessionDef(name=name, harness=harness, cwd="/work", parent=parent,
                   conversation_id=conversation_id),
        exit_code=None, held_at=STAMP, delayed_at=STAMP, last_output_at=output,
    )
    mgr._sessions[name] = record
    return record


# --------------------------------------------------------------------------- #
# the partitions
# --------------------------------------------------------------------------- #


def test_failed_and_delayed_are_their_own_partitions():
    def rec(**kw):
        base = dict(exited=True, archived_at=None, paused_at=None, held_at=None,
                    delayed_at=None, failed_at=None)
        base.update(kw)
        return SimpleNamespace(**base)

    category = session_mod.session_category
    assert category(rec(failed_at=STAMP)) == "failed"
    assert category(rec(held_at=STAMP, delayed_at=STAMP)) == "delayed"
    assert category(rec(held_at=STAMP)) == "hold"
    assert category(rec()) == "killed"
    assert category(rec(failed_at=STAMP, archived_at=STAMP)) == "archived"
    # A delayed record is asleep like a hold: the roster keeps it current.
    assert member_in_state("delayed", "current")
    assert not member_in_state("failed", "current")
    assert "delayed" in CURRENT_CATEGORIES


# --------------------------------------------------------------------------- #
# failed (claunch-0j6gb.1)
# --------------------------------------------------------------------------- #


def test_an_unasked_nonzero_exit_is_failed_and_keeps_its_hooks_for_the_kill(
        home, tmp_path):
    _register_failing_harness()

    async def run():
        mgr = _manager()
        exits, settled = [], []
        mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))
        mgr.settle_hooks.append(lambda s: settled.append(s.sdef.name))
        crashed = mgr.create(SessionDef(name="crashed", harness="pyfail",
                                        cwd=str(tmp_path)))
        killed = mgr.create(SessionDef(name="killed", harness="pyfail",
                                       cwd=str(tmp_path)))
        await _wait_screen(crashed, "READY")
        await _wait_screen(killed, "READY")

        await crashed.send_keys(["go", "Enter"])
        await crashed.wait_for("exited", timeout=10.0, threshold=0.5)
        assert crashed.exit_code == 3
        assert crashed.failed_at
        assert session_mod.session_category(crashed) == "failed"
        assert crashed.info()["failed_at"] == crashed.failed_at
        # The ending is reported (a parent hears of a crash) ...
        assert exits == ["crashed"]
        assert settled == []
        assert [e["kind"] for e in mgr.events.rows(crashed)][-1] == "fail"

        # An ending somebody asked for stays a kill, whatever the code.
        mgr.kill("killed")
        await killed.wait_for("exited", timeout=10.0, threshold=0.5)
        assert killed.failed_at is None
        assert session_mod.session_category(killed) == "killed"
        assert exits == ["crashed", "killed"]

        # ... and the person's kill of the failed record settles what it put
        # off -- the settle hooks (the board sweep), not the exit hooks again.
        assert mgr.unhold("crashed") is True
        assert crashed.failed_at is None
        assert session_mod.session_category(crashed) == "killed"
        assert exits == ["crashed", "killed"]
        assert settled == ["crashed"]

    asyncio.run(run())


def test_failed_marker_survives_a_restart_and_owes_no_sweep(home):
    mgr = _manager()
    mgr._sessions["f"] = DeadSession(SessionDef(name="f", harness="claude", cwd="/w"),
                                     exit_code=1, failed_at=STAMP)
    mgr._sessions["k"] = DeadSession(SessionDef(name="k", harness="claude", cwd="/w"),
                                     exit_code=0)
    mgr.persist()

    restarted = _manager()
    assert restarted.restore_all() == []
    assert restarted.get("f").failed_at == STAMP
    assert session_mod.session_category(restarted.get("f")) == "failed"
    assert [s.sdef.name for s in restarted.take_retired_for_sweep()] == ["k"]


# --------------------------------------------------------------------------- #
# delayed (claunch-0j6gb.2)
# --------------------------------------------------------------------------- #


def test_restore_files_resumable_sessions_as_delayed_instead_of_launching(home):
    mgr = _manager()
    _owed(mgr, "a")
    _owed(mgr, "b")
    mgr.persist()

    restarted = _staggered()
    assert restarted.restore_all() == []
    for name in ("a", "b"):
        record = restarted.get(name)
        assert record.exited and record.held_at and record.delayed_at
        assert session_mod.session_category(record) == "delayed"
    assert restarted.restore_queue == ["a", "b"]
    # Owed the resume nudge once up, as their state at shutdown says.
    assert set(restarted.resumed_idle) == {"a", "b"}
    # Not an ending: no board sweep at boot.
    assert restarted.take_retired_for_sweep() == []
    # Persisted as delayed, so a second restart owes them the same.
    restarted.persist()
    again = _staggered()
    again.restore_all()
    assert again.restore_queue == ["a", "b"]


def test_a_definition_that_would_come_back_blank_is_not_delayed(home, monkeypatch):
    mgr = _manager()
    _owed(mgr, "blank", conversation_id=None)
    mgr.persist()

    restarted = _staggered()
    launched = []
    monkeypatch.setattr(SessionManager, "create",
                        lambda self, sdef, **kw: launched.append(sdef.name))
    restarted.restore_all()
    assert restarted.restore_queue == []
    assert launched == ["blank"]


def test_rank_puts_leaders_then_working_then_parents_then_recent():
    mgr = _manager()
    _owed(mgr, "idle-old", output="2026-10-13T01:00:00+00:00")
    _owed(mgr, "idle-new", output="2026-10-13T08:00:00+00:00")
    _owed(mgr, "busy")
    _owed(mgr, "parent")
    _owed(mgr, "child", parent="parent")
    _owed(mgr, "lead")
    names = ["idle-old", "idle-new", "busy", "parent", "child", "lead"]
    order = restore_queue.rank(
        mgr, names,
        roles_of=lambda n: ["leader"] if n == "lead" else ["worker"],
        busy=["busy"],
    )
    assert order == ["lead", "busy", "parent", "idle-new", "idle-old", "child"]


def test_rank_starts_a_busy_worker_before_every_idle_parent():
    # claunch-z6c4b: s469 was mid-landing at the restart and came back 41st,
    # behind 40 idle parents (each with a reviewer child), 4m48s later.
    mgr = _manager()
    names = []
    for i in range(3):
        _owed(mgr, f"p{i}", output="2026-10-13T08:00:00+00:00")
        _owed(mgr, f"p{i}-rev", parent=f"p{i}")
        names += [f"p{i}", f"p{i}-rev"]
    _owed(mgr, "busy-parent")
    _owed(mgr, "busy-parent-rev", parent="busy-parent")
    _owed(mgr, "busy-worker", output="2026-10-13T01:00:00+00:00")
    names += ["busy-worker", "busy-parent", "busy-parent-rev"]
    order = restore_queue.rank(mgr, names, busy=["busy-worker", "busy-parent"])
    # Working first, a parent ahead among them; then the idle parents.
    assert order[:2] == ["busy-parent", "busy-worker"]
    assert order[2:5] == ["p0", "p1", "p2"]


def test_the_queue_wakes_in_order_skips_the_used_and_fails_a_broken_relaunch(home):
    mgr = _staggered()
    for name in ("first", "used", "broken", "last"):
        _owed(mgr, name)
        mgr.restore_queue.append(name)
    woken = []

    async def fake_wake(name, *, by=""):
        assert by == RESTORE_QUEUE
        if name == "broken":
            raise RuntimeError("spawn refused")
        record = mgr.get(name)
        record.held_at = record.delayed_at = None
        woken.append(name)
        return record

    mgr.wake_async = fake_wake

    async def run():
        queue = restore_queue.RestoreQueue(mgr, interval=0.0)
        # Used before the queue reached it: a viewer woke it.
        mgr.get("used").held_at = mgr.get("used").delayed_at = None
        queue.start()
        await queue._task
        return queue

    exits = []
    mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))
    queue = asyncio.run(run())
    assert woken == ["first", "last"]
    # The broken relaunch ended as a failure: its exit hooks ran.
    assert exits == ["broken"]
    assert queue.started == ["first", "last"]
    broken = mgr.get("broken")
    assert broken.delayed_at is None and broken.held_at is None
    assert broken.failed_at
    assert session_mod.session_category(broken) == "failed"


def test_resume_nudge_waits_for_a_delayed_session_instead_of_dropping_it(home):
    mgr = _manager()
    _owed(mgr, "later")
    nudge = resume_mod.ResumeNudge(mgr, ["later"], enabled=True)

    async def run():
        return await nudge._attempt("later")

    assert asyncio.run(run()) is False
    assert nudge._waiting["later"] == resume_mod.WAIT_DELAYED
    # Killed while waiting: settled, no longer owed.
    mgr.get("later").held_at = mgr.get("later").delayed_at = None
    assert asyncio.run(run()) is True


def test_the_board_hook_leaves_a_failed_session_for_the_settle(home):
    from claude_launcher.daemon import beads as beads_mod

    swept = []

    async def sweep(session):
        swept.append(session.sdef.name)

    board = SimpleNamespace(sweep=sweep, _tasks=set())
    failed = SimpleNamespace(failed_at=STAMP, sdef=SimpleNamespace(name="f"))
    killed = SimpleNamespace(failed_at=None, sdef=SimpleNamespace(name="k"))

    async def run():
        beads_mod.Board.session_exited(board, failed)
        beads_mod.Board.session_exited(board, killed)
        await asyncio.gather(*board._tasks)

    asyncio.run(run())
    assert swept == ["k"]


def test_killing_a_delayed_record_settles_it(home):
    mgr = _manager()
    exits = []
    mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))
    _owed(mgr, "d")
    assert mgr.unhold("d") is True
    record = mgr.get("d")
    assert record.delayed_at is None and record.held_at is None
    assert session_mod.session_category(record) == "killed"
    assert exits == ["d"]


def test_a_used_delayed_record_whose_relaunch_fails_is_filed_failed(home, monkeypatch):
    """A viewer, a mesh message or a cflow notice wakes a delayed record ahead
    of the queue. When that relaunch raises, the record is filed failed --
    the same as the queue's own failure -- instead of staying delayed with
    nobody left to try it (claunch-f9ene)."""
    mgr = _staggered()
    _owed(mgr, "d")
    mgr.restore_queue.append("d")

    async def refuse(self, **kw):
        raise RuntimeError("spawn refused")

    monkeypatch.setattr(SessionManager, "create_async", refuse)
    exits = []
    mgr.exit_hooks.append(lambda s: exits.append(s.sdef.name))

    async def run():
        try:
            await mgr.wake_async("d", by="attach")
        except RuntimeError:
            return True
        return False

    assert asyncio.run(run()) is True
    record = mgr.get("d")
    assert record.delayed_at is None and record.held_at is None
    assert record.failed_at
    assert session_mod.session_category(record) == "failed"
    assert exits == ["d"]


def test_the_queue_does_not_strand_a_record_whose_use_wake_fails_mid_turn(
        home, monkeypatch):
    """The queue's turn comes while a use-wake of the same record is still
    relaunching it: the record is out of the registry for that moment, so the
    queue passes it. The relaunch then fails. The record must not come back
    delayed -- the queue has gone past it and nothing else would retry it
    (claunch-f9ene)."""
    mgr = _staggered()
    _owed(mgr, "d")
    mgr.restore_queue.append("d")

    async def run():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_refuse(self, **kw):
            entered.set()
            await release.wait()
            raise RuntimeError("spawn refused")

        monkeypatch.setattr(SessionManager, "create_async", slow_refuse)
        use = asyncio.get_running_loop().create_task(
            mgr.wake_async("d", by="mesh"))
        await entered.wait()
        queue = restore_queue.RestoreQueue(mgr, interval=0.0)
        queue.start()
        await queue._task
        release.set()
        try:
            await use
        except RuntimeError:
            pass
        return queue

    queue = asyncio.run(run())
    assert queue.started == []
    record = mgr.get("d")
    assert record.delayed_at is None and record.held_at is None
    assert session_mod.session_category(record) == "failed"


# --------------------------------------------------------------------------- #
# progress for the dashboard (claunch-v4uyt)
# --------------------------------------------------------------------------- #


def test_queue_reports_each_place_and_its_progress(home):
    mgr = _staggered()
    for name in ("a", "b", "c", "d"):
        _owed(mgr, name)
    queue = restore_queue.RestoreQueue(mgr, interval=2.0)
    queue.order = ["a", "b", "c", "d"]
    queue._began = 100.0

    places = queue.positions(now=100.0)
    assert places["a"] == {"rank": 1, "total": 4, "ahead": 0, "eta_seconds": 2}
    assert places["d"]["ahead"] == 3 and places["d"]["eta_seconds"] == 8
    status = queue.status(now=100.0)
    assert status["total"] == 4 and status["done"] == 0 and status["remaining"] == 4
    assert status["next"] == "a"

    # "a" started by the queue after 10s, "c" woken by use: both leave it, and
    # the estimate follows the pace measured (10s), not the configured 2s.
    for name in ("a", "c"):
        mgr.get(name).held_at = mgr.get(name).delayed_at = None
    queue.started.append("a")
    queue._last_start = 110.0
    places = queue.positions(now=110.0)
    assert set(places) == {"b", "d"}
    assert places["b"] == {"rank": 2, "total": 4, "ahead": 0, "eta_seconds": 10}
    assert places["d"] == {"rank": 4, "total": 4, "ahead": 1, "eta_seconds": 20}
    status = queue.status(now=110.0)
    assert (status["done"], status["remaining"], status["next"]) == (2, 2, "b")
    assert status["pace_seconds"] == 10.0

    for name in ("b", "d"):
        mgr.get(name).held_at = mgr.get(name).delayed_at = None
    assert queue.status() is None
    assert queue.positions() == {}


def test_rail_rows_carry_the_delayed_marker_and_the_queue_place(home):
    """The rail view keeps only listed fields: delayed_at and failed_at were
    not among them, so every delayed row reached the page as a plain hold
    (claunch-v4uyt)."""
    from aiohttp.test_utils import TestClient, TestServer
    from claude_launcher.daemon.api import build_app

    async def run():
        mgr = _staggered()
        _owed(mgr, "waiting")
        mgr._sessions["crashed"] = DeadSession(
            SessionDef(name="crashed", harness="claude", cwd="/w"),
            exit_code=1, failed_at=STAMP)
        app = build_app(mgr, "sekrit", started_at=0.0)
        queue = restore_queue.RestoreQueue(mgr, interval=3.0)
        queue.order = ["waiting"]
        queue._began = __import__("time").monotonic()
        app["restore_queue"] = queue
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get(
                "/api/sessions?view=rail&state=current",
                headers={"Authorization": "Bearer sekrit"})
            assert response.status == 200
            body = await response.json()
        finally:
            await client.close()
        return body

    body = asyncio.run(run())
    rows = {row["name"]: row for row in body["sessions"]}
    assert rows["waiting"]["delayed_at"] == STAMP
    assert rows["waiting"]["restore_queue"]["rank"] == 1
    assert rows["waiting"]["restore_queue"]["total"] == 1
    assert rows["crashed"]["failed_at"] == STAMP
    assert "restore_queue" not in rows["crashed"]
    assert body["restore_queue"]["remaining"] == 1
    assert body["restore_queue"]["next"] == "waiting"


class _FakeQueue:
    def __init__(self, status, positions):
        self._status, self._positions = status, positions

    def waiting(self):
        return list(self._positions)

    def status(self, now=None, without=()):
        return self._status

    def positions(self, now=None, without=()):
        return self._positions


def test_a_host_relaunch_queue_does_not_hide_the_boot_queue():
    """The dashboard reads one restore queue; a host relaunch's queue used to
    replace the boot's there while the boot's was still walking its list
    (claunch-hor3b.2). Queues answers for both."""
    boot = _FakeQueue(
        {"total": 4, "done": 1, "started_by_queue": 1, "remaining": 3, "next": "b2",
         "interval": 3.0, "pace_seconds": 5.0, "eta_seconds": 15},
        {"b2": {"rank": 2, "total": 4, "ahead": 0, "eta_seconds": 5},
         "b3": {"rank": 3, "total": 4, "ahead": 1, "eta_seconds": 10}},
    )
    host = _FakeQueue(
        {"total": 3, "done": 1, "started_by_queue": 1, "remaining": 2, "next": "h1",
         "interval": 3.0, "pace_seconds": 3.0, "eta_seconds": 30},
        {"h1": {"rank": 2, "total": 3, "ahead": 0, "eta_seconds": 3}},
    )
    view = restore_queue.Queues(boot)
    view.add(host)
    status = view.status()
    assert (status["total"], status["done"], status["remaining"]) == (7, 2, 5)
    assert status["started_by_queue"] == 2
    assert status["next"] == "b2" and status["eta_seconds"] == 30
    assert set(view.positions()) == {"b2", "b3", "h1"}


def test_a_finished_queue_leaves_the_view_to_the_live_one():
    done = _FakeQueue(None, {})
    live = _FakeQueue(
        {"total": 1, "done": 0, "started_by_queue": 0, "remaining": 1, "next": "h1",
         "interval": 3.0, "pace_seconds": 3.0, "eta_seconds": 3},
        {"h1": {"rank": 1, "total": 1, "ahead": 0, "eta_seconds": 3}},
    )
    assert restore_queue.Queues(done).status() is None
    view = restore_queue.Queues(done, live)
    assert view.status() == live.status() and list(view.positions()) == ["h1"]


class _QueueManager:
    """Just what :class:`RestoreQueue` reads: delayed records it wakes."""

    restore_stagger = 0.0
    restore_queue: list = []
    resumed_busy: list = []

    def __init__(self, *names):
        self.records = {n: SimpleNamespace(exited=True, delayed_at=STAMP,
                                           last_output_at=None) for n in names}

    def get(self, name):
        from claude_launcher.daemon.manager import ManagerError
        if name not in self.records:
            raise ManagerError(name)
        return self.records[name]

    def descendants(self, name):
        return []

    async def wake_async(self, name, by=None):
        self.records[name].exited = False
        self.records[name].delayed_at = None

    def delay_again(self, name):
        self.records[name].exited = True
        self.records[name].delayed_at = STAMP


def test_a_name_the_boot_queue_has_passed_is_counted_once_when_delayed_again():
    """The boot's queue started A; a host loss then files A delayed again for
    the relaunch's own queue. The boot's queue never comes back to A, so A
    waits in the relaunch's queue alone: three waiting, not four, and A's
    place is the relaunch queue's (claunch-hor3b.2, s511-rev)."""
    mgr = _QueueManager("A", "B", "C")

    async def go():
        boot = restore_queue.RestoreQueue(mgr, names=["A", "B", "C"], interval=60)
        boot.start()
        for _ in range(100):
            if boot.started:
                break
            await asyncio.sleep(0.01)
        assert boot.started == ["A"]  # and now sleeps the interval before B
        mgr.delay_again("A")
        host = restore_queue.RestoreQueue(mgr, names=["A"], interval=60)
        host.order = ["A"]  # ranked, not started: its run would wake A at once
        view = restore_queue.Queues(boot, host)
        try:
            assert boot.waiting() == ["B", "C"]
            status = view.status()
            assert status["remaining"] == 3 and status["total"] == 4
            assert view.positions()["A"]["total"] == 1
        finally:
            await boot.stop()

    asyncio.run(go())


def test_a_name_woken_before_its_turn_and_delayed_again_is_counted_once():
    """The boot's queue [B, C, A] started B; A was woken by use before its
    turn. A host loss files A and B delayed again for the relaunch's queue
    [A, B]. A now waits in both queues' lists; it is counted once, in the
    boot's: three waiting, not four (claunch-hor3b.2, s511-rev)."""
    mgr = _QueueManager("A", "B", "C")

    async def go():
        boot = restore_queue.RestoreQueue(mgr, names=["B", "C", "A"], interval=60)
        boot.start()
        for _ in range(100):
            if boot.started:
                break
            await asyncio.sleep(0.01)
        assert boot.order == ["B", "C", "A"] and boot.started == ["B"]
        mgr.delay_again("B")
        mgr.delay_again("A")
        host = restore_queue.RestoreQueue(mgr, names=["A", "B"], interval=60)
        host.order = ["A", "B"]  # ranked, not started: its run would wake A at once
        view = restore_queue.Queues(boot, host)
        try:
            assert boot.waiting() == ["C", "A"] and host.waiting() == ["A", "B"]
            status = view.status()
            assert (status["total"], status["done"], status["remaining"]) == (4, 1, 3)
            places = view.positions()
            assert set(places) == {"A", "B", "C"}
            assert places["A"] == {**places["A"], "rank": 3, "total": 3, "ahead": 1}
            assert places["B"] == {**places["B"], "rank": 1, "total": 1, "ahead": 0}
        finally:
            await boot.stop()

    asyncio.run(go())


def test_a_queue_that_walked_its_list_leaves_the_view_when_another_joins():
    mgr = _QueueManager("A")

    async def go():
        first = restore_queue.RestoreQueue(mgr, names=["A"], interval=0)
        first.start()
        for _ in range(100):
            if first.finished:
                break
            await asyncio.sleep(0.01)
        assert first.finished
        view = restore_queue.Queues(first)
        later = restore_queue.RestoreQueue(mgr, names=[], interval=0)
        view.add(later)
        assert view.queues == [later]

    asyncio.run(go())
