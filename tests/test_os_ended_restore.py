"""A session Windows ends at a logoff or shutdown comes back at the next boot.

2026-09-23 11:09 KST (claunch-wnwa5): a Windows restart killed the session
processes with DBG_TERMINATE_PROCESS (0x40010004) a moment before it killed
the daemon. The daemon was still alive to see those exits, recorded them as
final (``was_running: false``) and swept their board issues back to open, so
the next boot's ``restore_all`` retired 23 sessions it should have resumed;
only the ones whose exit the daemon did not live to see came back.

These tests hold the fix: such an exit is persisted as still running, skips
the exit hooks the way a daemon shutdown does, and is relaunched by the next
``restore_all`` — while a record retired earlier with the same code, or one
archived since, stays where it is.
"""

from __future__ import annotations

from claude_launcher.daemon import db
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager, ended_by_os
from claude_launcher.daemon.session import DeadSession

OS_ENDED = 0x40010004  # 1073807364, as the registry stores it


class _Exited:
    """A live session at the moment its process has gone, as the manager's
    exit funnel and ``persist`` read one."""

    def __init__(self, name: str, cwd: str, exit_code) -> None:
        self.sdef = SessionDef(name=name, cwd=cwd, restore=True)
        self.exited = True
        self.exit_code = exit_code
        self.ended_by_os = False
        self.pid = 4321
        self.created_at = None
        self.last_output_at = None
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = "2026-09-23T02:09:35+00:00"
        self.archived_at = None

    def status(self, threshold=None) -> str:
        return "exited"

    def delivery_held(self) -> bool:
        return False


class _NoSpawnManager(SessionManager):
    """``restore_all`` decides before it spawns; register instead of launching."""

    def create(self, sdef, *, restoring=False, opening="", created_at="",
               last_visited_at="", last_input_at="", delivery_hold=False):
        session = _Exited(sdef.name, sdef.cwd, None)
        session.exited = False
        self._sessions[sdef.name] = session
        return session


def _manager(cls=SessionManager):
    return cls(idle_threshold=0.5, scrollback=100, restore_default=True)


def _saved():
    return {e["def"]["name"]: e for e in db.open_default().load_all()}


def test_only_the_windows_termination_code_counts_as_the_os():
    assert ended_by_os(1073807364)
    for code in (None, 0, 1, 2, 0xC000013A, 137):
        assert not ended_by_os(code)


def test_an_os_ended_exit_is_kept_running_and_skips_the_exit_hooks(home, tmp_path):
    mgr = _manager()
    swept = []
    mgr.exit_hooks.append(lambda s: swept.append(s.sdef.name))
    session = _Exited("rebooted", str(tmp_path), OS_ENDED)
    mgr._sessions["rebooted"] = session

    mgr._session_exited(session)

    assert session.ended_by_os is True
    assert _saved()["rebooted"]["was_running"] is True
    # the board sweep is an exit hook: an issue must not go back to open
    assert swept == []


def test_an_ordinary_exit_is_still_final(home, tmp_path):
    mgr = _manager()
    swept = []
    mgr.exit_hooks.append(lambda s: swept.append(s.sdef.name))
    session = _Exited("quit", str(tmp_path), 2)
    mgr._sessions["quit"] = session

    mgr._session_exited(session)

    assert _saved()["quit"]["was_running"] is False
    assert swept == ["quit"]


def test_the_next_boot_restores_the_os_ended_session(home, tmp_path):
    before = _manager()
    session = _Exited("rebooted", str(tmp_path), OS_ENDED)
    before._sessions["rebooted"] = session
    before._session_exited(session)

    after = _manager(_NoSpawnManager)
    assert after.restore_all() == []
    assert not after.get("rebooted").exited


def test_a_record_retired_earlier_with_the_code_stays_retired(home, tmp_path):
    # s684 was ended by an earlier reboot, before the fix, and retired at
    # the boot after it. Carrying its exit code must not revive it later.
    mgr = _manager()
    mgr._sessions["s684"] = DeadSession(
        SessionDef(name="s684", cwd=str(tmp_path), restore=True),
        exit_code=OS_ENDED,
    )
    mgr.persist()
    assert _saved()["s684"]["was_running"] is False


def test_archiving_an_os_ended_session_stops_its_restore(home, tmp_path):
    mgr = _manager()
    session = _Exited("rebooted", str(tmp_path), OS_ENDED)
    mgr._sessions["rebooted"] = session
    mgr._session_exited(session)

    session.archived_at = "2026-09-23T03:00:00+00:00"
    mgr.persist()
    assert _saved()["rebooted"]["was_running"] is False
