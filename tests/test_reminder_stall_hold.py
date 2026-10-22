"""The session reminder's stall hold (claunch-pumne).

A terminal that has drawn no new line for ``reminder_stall_hold`` seconds keeps
its repeating reminder held, whatever its busy/idle status says: a turn parked
on a long command, or a TUI animating a spinner, reads as *working* while
producing nothing new to read, so the busy gate alone kept typing reminders
into it.

The hold is the same one a stopped session already gets, which is the point of
putting it here rather than in each source: every source reaches the terminal
through one delivery boundary, and a held reminder is re-armed for a fresh
interval when the screen moves again instead of being replayed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock, mesh_roles, session_reminder
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.idle import IdleTracker
from claude_launcher.daemon.session import Session

CFG = {"role_reminder": True, "role_reminder_interval": 600.0}


class _Session:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        self.status_value = "busy"
        #: Seconds the screen has been still. ``None`` = the session does not
        #: expose the reading at all (older/fake implementations).
        self.screen_idle = 0.0

    def status(self, threshold=None):
        return self.status_value

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True

    def reminders_paused(self) -> bool:
        return False

    def screen_idle_for(self):
        return None if self.screen_idle is None else float(self.screen_idle)


class _Manager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())

    def live_children(self, name: str):
        return []

    def persist(self):
        return None


class _Mesh:
    name = "m"
    #: The packaged role vocabulary, so ``role_entries`` resolves a stance.
    roleset = mesh_roles.resolve()

    def owed(self, handle):
        return []


class _MeshMgr:
    def meshes_for_session(self, name):
        return [{"mesh": "m"}]

    def get(self, name):
        return _Mesh()

    def member_for_session(self, mesh, name):
        return type("M", (), {"handle": name, "role": "worker"})()


def _service(session):
    return session_reminder.SessionReminderService(
        _Manager({session.sdef.name: session}), _MeshMgr()
    )


def _due(service, sess, now, cfg=CFG):
    """Scan, then deliver what the scan found due."""
    due = service.scan_roles(now, cfg)
    if not due:
        return []
    asyncio.run(
        service._deliver_session(
            sess.sdef.name, roles=due[0][1], role_due=True, now=now, cfg=cfg
        )
    )
    return due


# --------------------------------------------------------------------------- #
# the gate itself
# --------------------------------------------------------------------------- #
def test_a_still_screen_holds_the_reminder():
    """Due, working, and still: nothing is typed, and the debt is stamped."""
    sess = _Session("w1", "proj")
    service = _service(sess)
    service.scan_roles(1000.0, CFG)          # first sight arms the timer
    sess.screen_idle = 20.0                  # no new line for 20 s
    assert _due(service, sess, 1601.0)       # the timer says it is time
    assert sess.delivered == []
    assert service._roles["w1"]["held_at"] == 1601.0


def test_a_moving_screen_is_still_delivered():
    sess = _Session("w1", "proj")
    service = _service(sess)
    service.scan_roles(1000.0, CFG)
    sess.screen_idle = 5.0                   # a new line three seconds ago
    _due(service, sess, 1601.0)
    assert len(sess.delivered) == 1


def test_resuming_the_screen_rearms_instead_of_replaying():
    """The rule claunch-1x96 set for stopped sessions, reused unchanged."""
    sess = _Session("w1", "proj")
    service = _service(sess)
    service.scan_roles(1000.0, CFG)
    sess.screen_idle = 20.0
    _due(service, sess, 1601.0)
    assert sess.delivered == []
    held = service._roles["w1"]

    # The screen moves: the hold is released by re-arming, not by typing the
    # stale reminder into the turn that just started.
    sess.screen_idle = 1.0
    _due(service, sess, 1602.0)
    assert sess.delivered == []
    assert held["held_at"] is None
    assert held["at"] == 1602.0

    # A full interval from the resume, and the screen is still moving.
    _due(service, sess, 1602.0 + 601.0)
    assert len(sess.delivered) == 1


def test_the_threshold_is_configurable_and_zero_turns_the_hold_off():
    assert cflow_clock.stall_hold_seconds({}) == 15.0
    assert cflow_clock.stall_hold_seconds({"reminder_stall_hold": 30}) == 30.0
    assert cflow_clock.stall_hold_seconds({"reminder_stall_hold": 0}) == 0.0
    assert cflow_clock.stall_hold_seconds({"reminder_stall_hold": -5}) == 0.0
    # An unreadable value falls back to the default, never to "off": a broken
    # config must not be the thing that floods the terminals.
    assert cflow_clock.stall_hold_seconds({"reminder_stall_hold": "x"}) == 15.0

    # Below a raised threshold the reminder goes out; with the gate off, a
    # screen that has been still for an hour does not hold anything.
    below = _Session("w1", "proj")
    service = _service(below)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0,
           "reminder_stall_hold": 30}
    service.scan_roles(1000.0, cfg)
    below.screen_idle = 20.0
    _due(service, below, 1601.0, cfg)
    assert len(below.delivered) == 1

    off = _Session("w1", "proj")
    service = _service(off)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0,
           "reminder_stall_hold": 0}
    service.scan_roles(1000.0, cfg)
    off.screen_idle = 3600.0
    _due(service, off, 1601.0, cfg)
    assert len(off.delivered) == 1


LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project with a workflow to drive the cflow source."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    directory = tmp_path / "proj"
    (directory / ".claunch" / "workflows").mkdir(parents=True)
    (directory / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    return directory


def test_the_cflow_source_is_held_the_same_way(proj, monkeypatch):
    """The gate sits on the one delivery boundary every source crosses.

    The cflow reminder is the one the complaint was about, so it is worth
    pinning that it reaches the same hold as the role source and not only
    the role test above.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _Session("w1", cwd)
    service = _service(sess)
    cfg = {"cflow_reminder": True, "cflow_reminder_interval": 600.0,
           "role_reminder": False, "prompt_pack_reminder": False}
    monkeypatch.setattr(
        session_reminder.SessionReminderService, "_config", staticmethod(lambda: cfg)
    )

    base = time.monotonic()
    asyncio.run(service.tick(base))          # first sight arms the timer
    sess.screen_idle = 20.0                  # no new line for 20 s
    asyncio.run(service.tick(base + 601.0))
    assert sess.delivered == []
    assert service.timers()[(cwd, "w1")]["held_ago"] is not None

    # The screen moves: re-armed from here, not replayed.
    sess.screen_idle = 1.0
    asyncio.run(service.tick(base + 602.0))
    assert sess.delivered == []
    asyncio.run(service.tick(base + 602.0 + 601.0))
    assert len(sess.delivered) == 1


def test_a_session_without_the_reading_is_not_held():
    """No measurement is not evidence of stillness (older/fake sessions)."""
    sess = _Session("w1", "proj")
    sess.screen_idle = None
    service = _service(sess)
    service.scan_roles(1000.0, CFG)
    _due(service, sess, 1601.0)
    assert len(sess.delivered) == 1


# --------------------------------------------------------------------------- #
# what the header says
# --------------------------------------------------------------------------- #
class _Running:
    @staticmethod
    def done():
        return False


def test_the_status_view_names_the_stall():
    sess = _Session("w1", "proj")
    service = _service(sess)
    service._task = _Running()
    service.scan_roles(1000.0, CFG)

    sess.screen_idle = 20.0
    stalled = service.status("w1", now=1601.0, cfg=CFG)
    assert stalled["role"]["state"] == "stalled"

    sess.screen_idle = 2.0
    assert service.status("w1", now=1601.0, cfg=CFG)["role"]["state"] == "due"

    # The stopped-session reading keeps its own word: both are silence, but
    # only one of them is a session that is working.
    sess.status_value = "idle"
    assert service.status("w1", now=1601.0, cfg=CFG)["role"]["state"] == "held"


# --------------------------------------------------------------------------- #
# the session-level reading
# --------------------------------------------------------------------------- #
def _bare_session() -> Session:
    """A Session with the handful of attributes the reading touches."""
    session = object.__new__(Session)
    session.exited = False
    session._saw_output = True
    session.background_render = True
    session._last_output_mono = 0.0
    session.tracker = IdleTracker()
    return session


def test_screen_idle_for_answers_while_the_session_is_busy():
    session = _bare_session()
    t0 = 1000.0
    session.tracker.sample((1, 2, 3), t0)
    session.tracker.sample((1, 2, 3), t0 + 5.0)   # an unchanged sample is not a change
    assert session.screen_idle_for(t0 + 20.0) == 20.0
    # A new line is what moves it, not the clock.
    session.tracker.sample((9, 9, 9), t0 + 20.0)
    assert session.screen_idle_for(t0 + 20.0) == 0.0


def test_screen_idle_for_ignores_a_spinner():
    """Animated rows are excluded from "meaningful", which is the whole point."""
    session = _bare_session()
    t0 = 1000.0
    # Row 1 changes on every sample: animated, so it is not a new line. The
    # window needs a few samples to learn that, hence the warm-up.
    for i in range(10):
        session.tracker.sample((1, i, 3), t0 + i)
    assert session.screen_idle_for(t0 + 9.0) >= 5.0


def test_screen_idle_for_declines_to_measure_a_parked_grid():
    session = _bare_session()
    session.background_render = False
    session.is_focused = lambda: False
    session._last_output_mono = 1000.0
    # A parked grid's line hashes say nothing; output timing is the reading.
    assert session.screen_idle_for(1012.0) == 12.0
    # Before the first output there is nothing to measure at all.
    session._saw_output = False
    assert session.screen_idle_for(1012.0) is None
