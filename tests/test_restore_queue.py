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


def test_rank_puts_leaders_then_parents_then_working_then_recent():
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
    assert order == ["lead", "parent", "busy", "idle-new", "idle-old", "child"]


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
