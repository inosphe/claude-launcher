"""The fault pause clock (daemon/fault_pause.py, claunch-ijot0)."""

from datetime import datetime, timezone
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
        self.paused_at = None
        self.archived_at = None
        self.ended_by = None

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
        session = self.sessions[name]
        session.exited = True
        session.paused_at = "2026-10-30T04:00:00+00:00"
        session.ended_by = by


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


AFTER = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
BEFORE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _stated(**kw):
    return {**_limit(**kw), "until_stated": True, "harness": "claude",
            "label": "usage limit", "message": "You've hit your weekly limit"}


def test_a_reset_is_listed_once_its_stated_time_has_passed():
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    assert fault_pause.resets(manager, now=BEFORE) == []
    rows = fault_pause.resets(manager, now=AFTER)
    assert [r["session"] for r in rows] == ["s29"]
    assert rows[0]["fault"]["until"] == "2026-11-02T00:00:00+00:00"
    assert rows[0]["paused_at"] == "2026-10-30T04:00:00+00:00"


def test_the_record_outlives_the_live_fault_reading():
    """A restart turns the record into one without its screen: the reset
    is read from the file the pause wrote."""
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    manager.sessions["s29"]._fault = None
    assert [r["session"] for r in fault_pause.resets(manager, now=AFTER)] == ["s29"]


def test_a_guessed_reset_is_not_told():
    """A window the detector guessed says nothing about when the plan returns."""
    manager = FakeManager(FakeSession("s29", _limit()))
    fault_pause.sweep(manager, {})
    assert fault_pause.resets(manager, now=AFTER) == []


def test_only_a_session_still_paused_by_this_clock_is_listed():
    manager = FakeManager(FakeSession("a", _stated()), FakeSession("b", _stated()),
                          FakeSession("c", _stated()))
    fault_pause.sweep(manager, {})
    manager.sessions["a"].paused_at = None  # resumed or killed
    manager.sessions["b"].ended_by = actors.make(actors.USER)  # a person's pause
    manager.sessions["c"].archived_at = "2026-11-01T00:00:00+00:00"
    assert fault_pause.resets(manager, now=AFTER) == []


# --- the pause as a fault reading (claunch-9djny.1) ----------------------
#
# A pause kills the process, so the live screen reading is gone: the record
# the clock wrote is the only thing left that can say why the session is
# stopped, for the days between the pause and its reset.

def test_a_pending_reset_is_listed_when_asked_for():
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    assert fault_pause.resets(manager, now=BEFORE) == []
    pending = fault_pause.resets(manager, now=BEFORE, include_pending=True)
    assert [(r["session"], r["due"]) for r in pending] == [("s29", False)]
    due = fault_pause.resets(manager, now=AFTER, include_pending=True)
    assert [(r["session"], r["due"]) for r in due] == [("s29", True)]


def test_the_record_carries_the_pauses_own_stamp(home):
    """The stamp is the pause's, not a second reading of the clock: the
    reading that reads the record back matches it to the session's, and two
    ``now()`` calls can land either side of a second boundary."""
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    record = fault_pause.read_record("s29")
    assert record["paused_at"] == manager.sessions["s29"].paused_at


def test_a_record_without_a_readable_deadline_is_capped_by_its_kind(home):
    record = {
        "kind": "usage_limit", "until": "soon", "until_stated": True,
        "message": "You've hit your weekly limit",
    }
    fault_pause.write_record(
        "capped", record, paused_at="2026-10-30T04:00:00+00:00"
    )
    written = fault_pause.read_record("capped")
    # usage_limit's window in session_faults.yaml is 86400s.
    assert written["until"] == "2026-10-31T04:00:00+00:00"
    assert written["until_stated"] is False


def test_a_paused_session_answers_the_fault_it_was_paused_for(home):
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    view = fault_pause.paused_fault(manager.sessions["s29"])
    assert view is not None
    assert view["source"] == "pause-record"
    assert view["kind"] == "usage_limit" and view["label"] == "usage limit"
    assert view["until"] == "2026-11-02T00:00:00+00:00"
    assert view["message"] == "You've hit your weekly limit"


def test_a_session_no_longer_paused_answers_nothing(home):
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    session = manager.sessions["s29"]
    session.paused_at = None  # resumed, unpaused or killed
    assert fault_pause.paused_fault(session) is None
    session.paused_at = "2026-10-30T04:00:00+00:00"
    session.archived_at = "2026-11-01T00:00:00+00:00"
    assert fault_pause.paused_fault(session) is None
    session.archived_at = None
    session.ended_by = actors.make(actors.USER)  # a person's own pause
    assert fault_pause.paused_fault(session) is None


def test_a_record_from_another_pause_is_not_read(home):
    """A name reused by a later session has its own stamp: the older file
    on disk was written for a pause that ended."""
    manager = FakeManager(FakeSession("s29", _stated()))
    fault_pause.sweep(manager, {})
    manager.sessions["s29"].paused_at = "2026-10-31T09:00:00+00:00"
    assert fault_pause.paused_fault(manager.sessions["s29"]) is None


def test_the_pause_fault_is_over_when_its_reset_passes():
    record = {
        "kind": "usage_limit", "until": "2026-11-02T00:00:00+00:00",
        "until_stated": True, "paused_at": "2026-10-30T04:00:00+00:00",
    }
    assert fault_pause.pause_fault_view(record, now=BEFORE)["active"] is True
    assert fault_pause.pause_fault_view(record, now=AFTER)["active"] is False
