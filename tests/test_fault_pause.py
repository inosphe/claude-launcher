"""The fault pause clock (daemon/fault_pause.py, claunch-ijot0)."""

from types import SimpleNamespace

from claude_launcher.daemon import actors, fault_pause
from claude_launcher.daemon.session import STATUS_IDLE


class FakeSession:
    def __init__(self, name, fault=None, status=STATUS_IDLE):
        self.sdef = SimpleNamespace(name=name)
        self.exited = False
        self.kill_requested = False
        self._fault = fault
        self._status = status

    def fault(self):
        return self._fault

    def status(self, threshold=None):
        return self._status


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}
        self.paused = []

    def list(self):
        return list(self.sessions.values())

    def pause(self, name, *, force=False, by=None):
        self.paused.append((name, by))
        self.sessions[name].exited = True


def _limit(until="2026-11-02T00:00:00+00:00", active=True, kind="usage_limit"):
    return {"kind": kind, "active": active, "until": until}


def test_an_idle_session_under_a_usage_limit_is_paused_by_the_daemon():
    manager = FakeManager(FakeSession("s29", _limit()), FakeSession("s30"))
    assert fault_pause.sweep(manager, {}) == ["s29"]
    name, by = manager.paused[0]
    assert name == "s29"
    assert by == actors.make(actors.DAEMON, reason=fault_pause.REASON)


def test_other_fault_kinds_and_inactive_faults_do_not_pause():
    manager = FakeManager(
        FakeSession("net", _limit(kind="network")),
        FakeSession("old", _limit(active=False)),
        FakeSession("auth", _limit(kind="auth")),
    )
    assert fault_pause.sweep(manager, {}) == []
    assert manager.paused == []


def test_a_busy_session_is_not_paused_whatever_its_fault_reading():
    manager = FakeManager(FakeSession("s29", _limit(), status="busy"))
    assert fault_pause.sweep(manager, {}) == []


def test_an_exited_or_ending_session_is_left_alone():
    gone = FakeSession("gone", _limit())
    gone.exited = True
    ending = FakeSession("ending", _limit())
    ending.kill_requested = True
    manager = FakeManager(gone, ending)
    assert fault_pause.sweep(manager, {}) == []


def test_a_session_resumed_during_the_same_limit_is_not_paused_again():
    paused = {}
    first = FakeSession("s29", _limit())
    manager = FakeManager(first)
    assert fault_pause.sweep(manager, paused) == ["s29"]
    # A person resumes it before the reset: a fresh session, same limit.
    manager.sessions["s29"] = FakeSession("s29", _limit())
    assert fault_pause.sweep(manager, paused) == []
    # A later limit (a new reset deadline) pauses it again.
    manager.sessions["s29"] = FakeSession("s29", _limit(until="2026-11-09T00:00:00+00:00"))
    assert fault_pause.sweep(manager, paused) == ["s29"]


def test_the_clock_does_nothing_when_switched_off():
    manager = FakeManager(FakeSession("s29", _limit()))
    clock = fault_pause.FaultPauseClock(manager, config=lambda: {"fault_auto_pause": False})
    assert clock.pass_once() == []
    clock = fault_pause.FaultPauseClock(manager, config=lambda: {})
    assert clock.pass_once() == ["s29"]
