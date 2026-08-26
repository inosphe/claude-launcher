"""The resume nudge: who a daemon restart owes a "carry on", and who it does not.

A restart leaves every restored session alive and idle — the terminal is back,
the conversation is back, and the turn that was running is gone with nothing
to start the next one. These tests hold the three parts of the fix to their
contracts: ``persist`` records *which sessions were working* in the moment
before shutdown, ``restore_all`` carries those names forward, and
:class:`ResumeNudge` types the block into exactly those sessions — once, only
when the TUI can take it, and never into one whose cflow run is parked behind
a guardrail the agent cannot open.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import paths, resume
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""

GATED = """
name: gated
steps:
  work:
    instructions: do work
    next: ship
  ship:
    gate: approve shipping
    instructions: ship it
"""

USER_BRANCH = """
name: userbranch
steps:
  landing:
    select:
      prompt: request or hold?
      chooser: user
      options:
        request: {description: ask now, next: after}
        hold:    {description: freeze, next: after}
  after:
    instructions: continue
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project with the three workflow shapes declared."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    for name, body in (
        ("linear", LINEAR), ("gated", GATED), ("userbranch", USER_BRANCH)
    ):
        (d / ".claunch" / "workflows" / f"{name}.yaml").write_text(body, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _Recorded:
    """A session as ``persist`` reads one: a definition and a status."""

    def __init__(self, name: str, status: str = "idle", *, cwd: str = "") -> None:
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.exited = status == "exited"
        self._status = status
        self.exit_code = None
        self.pid = 4321
        self.created_at = None
        self.last_output_at = None
        # persist() also records when a person last looked in and last typed
        # here; a double that leaves them off makes it raise instead of
        # writing the file this test then reads.
        self.last_visited_at = None
        self.last_input_at = None
        self.exited_at = None

    def status(self, threshold=None) -> str:
        return self._status


class _NoSpawnManager(SessionManager):
    """A manager whose ``create`` registers instead of launching a PTY.

    ``restore_all``'s decisions — who is relaunched, who is retired, who lands
    in ``resumed_busy`` — are all made before anything is spawned, so the
    spawn is the one part these tests do not need.
    """

    def create(
        self,
        sdef,
        *,
        restoring: bool = False,
        opening: str = "",
        created_at: str = "",
        last_visited_at: str = "",
        last_input_at: str = "",
    ):
        session = _Recorded(sdef.name, "starting", cwd=sdef.cwd)
        # Carried like the real one does: a restore relaunches into a new
        # object, and the session's creation time has to survive it (the
        # listings are ordered by it -- see manager.list). The same holds for
        # when a person last looked in and last typed here -- and this
        # override has to keep accepting them, or restore_all's call raises
        # TypeError and every restore is counted as a failed relaunch.
        session.created_at = created_at or None
        session.last_visited_at = last_visited_at or None
        session.last_input_at = last_input_at or None
        self._sessions[sdef.name] = session
        return session


class _Session:
    """A live session as the nudge reads one."""

    def __init__(self, name: str, cwd: str, *, harness: str = "claude") -> None:
        self.sdef = SessionDef(name=name, cwd=cwd, harness=harness)
        self.exited = False
        self.screen = types.SimpleNamespace(bracketed_paste=True)
        self.delivered: list = []
        self.deliver_ok = True
        #: One status per poll, last value repeating — so a test writes the
        #: sequence it wants instead of racing a clock.
        self.statuses = ["idle"]
        self._seen = 0

    def status(self, threshold=None) -> str:
        i = min(self._seen, len(self.statuses) - 1)
        self._seen += 1
        return self.statuses[i]

    async def deliver(self, text: str) -> bool:
        if not self.deliver_ok:
            return False
        self.delivered.append(text)
        return True


class _Manager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


async def _drain(nudge, *, timeout: float = 5.0) -> None:
    """Start the one-shot and wait for it to finish on its own."""
    nudge.start()
    if nudge._task is not None:
        await asyncio.wait_for(nudge._task, timeout)


@pytest.fixture
def instant(monkeypatch):
    """No startup settle: these tests own the status sequence, not the clock."""
    monkeypatch.setattr(resume, "INPUT_SETTLE", 0.0)


# --------------------------------------------------------------------------- #
# who was working: persist -> sessions.json -> restore_all
# --------------------------------------------------------------------------- #
def test_persist_records_which_sessions_were_working(home, tmp_path):
    mgr = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)
    mgr._sessions["mid-turn"] = _Recorded("mid-turn", "busy", cwd=str(tmp_path))
    mgr._sessions["finished"] = _Recorded("finished", "idle", cwd=str(tmp_path))
    mgr._sessions["dead"] = _Recorded("dead", "exited", cwd=str(tmp_path))
    mgr.persist()

    entries = {
        e["def"]["name"]: e
        for e in json.loads(paths.sessions_json().read_text(encoding="utf-8"))
    }
    assert entries["mid-turn"]["was_busy"] is True
    assert entries["finished"]["was_busy"] is False
    assert entries["dead"]["was_busy"] is False
    # the pre-existing field keeps its own meaning: alive, not working
    assert entries["finished"]["was_running"] is True
    assert entries["dead"]["was_running"] is False


def _write_sessions_json(entries: list) -> None:
    path = paths.sessions_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _entry(name: str, *, cwd: str, running: bool = True, busy: bool = False,
           restore: bool = True) -> dict:
    return {
        "def": SessionDef(name=name, cwd=cwd, restore=restore).to_dict(),
        "was_running": running,
        "was_busy": busy,
    }


def test_restore_collects_only_the_sessions_that_were_working(home, tmp_path):
    cwd = str(tmp_path)
    _write_sessions_json([
        _entry("worker", cwd=cwd, busy=True),
        _entry("resting", cwd=cwd, busy=False),
        _entry("gone", cwd=cwd, running=False, busy=True),   # already exited
        _entry("opted-out", cwd=cwd, busy=True, restore=False),
    ])
    mgr = _NoSpawnManager(idle_threshold=0.5, scrollback=100, restore_default=True)
    assert mgr.restore_all() == []
    # relaunched but not working, exited, and opt-out records all stay silent
    assert mgr.resumed_busy == ["worker"]
    assert mgr.get("gone").exited and mgr.get("opted-out").exited


def test_a_file_from_an_older_daemon_nudges_nobody(home, tmp_path):
    """No ``was_busy`` key at all (the field did not exist): read as False."""
    entry = _entry("legacy", cwd=str(tmp_path))
    entry.pop("was_busy")
    _write_sessions_json([entry])
    mgr = _NoSpawnManager(idle_threshold=0.5, scrollback=100, restore_default=True)
    mgr.restore_all()
    assert mgr.resumed_busy == []
    assert not mgr.get("legacy").exited  # still restored, just not nudged


def test_a_fresh_manager_owes_nobody_a_nudge(home):
    mgr = SessionManager(idle_threshold=0.5, scrollback=100, restore_default=True)
    assert mgr.resumed_busy == []


# --------------------------------------------------------------------------- #
# the guardrail: which cflow positions allow a nudge
# --------------------------------------------------------------------------- #
def test_no_run_at_all_is_not_a_guardrail(proj):
    assert resume.gate(str(proj), "w1") is True


def test_a_step_is_the_agents_to_move(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "step"
    assert resume.gate(cwd, "w1") is True


def test_a_human_gate_holds_the_nudge(proj):
    cwd = str(proj)
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    cflow_engine.report("done", cwd=cwd, scope="w1")
    payload = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert payload["status"] == "waiting_approval"
    assert resume.gate(cwd, "w1") is False
    # and the moment the human opens it, the session is nudgeable again
    cflow_engine.approve(by="user", cwd=cwd, scope="w1")
    assert resume.gate(cwd, "w1") is True


def test_a_users_selection_holds_the_nudge(proj):
    cwd = str(proj)
    cflow_engine.start("userbranch", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "waiting_selection"
    assert resume.gate(cwd, "w1") is False


def test_an_unreadable_run_is_not_an_answer(proj, monkeypatch):
    """Neither go nor stand-down: the caller looks again next poll."""
    def _boom(*a, **kw):
        raise RuntimeError("state locked mid-write")

    monkeypatch.setattr(cflow_engine, "status", _boom)
    assert resume.gate(str(proj), "w1") is None


# --------------------------------------------------------------------------- #
# the nudge itself
# --------------------------------------------------------------------------- #
def test_it_delivers_once_and_stops(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path))
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge.delivered == ["w1"]
    assert len(session.delivered) == 1
    block = session.delivered[0]
    assert "session resume" in block and "machine-generated" in block
    assert "session: w1" in block
    assert nudge.pending == []


def test_a_parked_run_hears_nothing(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: False)
    session = _Session("w1", str(tmp_path))
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert session.delivered == []
    assert nudge.delivered == []
    assert nudge.pending == []  # settled, not left spinning


def test_an_unreadable_run_is_retried_not_dropped(home, tmp_path, monkeypatch, instant):
    answers = [None, None, True]

    monkeypatch.setattr(resume, "gate", lambda cwd, scope: answers.pop(0))
    session = _Session("w1", str(tmp_path))
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge.delivered == ["w1"]
    assert answers == []


def test_it_waits_for_the_tui_to_take_the_keyboard(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path))
    session.statuses = ["starting", "starting", "idle", "idle"]
    session.screen.bracketed_paste = False

    async def run():
        nudge = resume.ResumeNudge(
            _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
        )
        nudge.start()
        await asyncio.sleep(0.05)
        assert session.delivered == []  # still starting: nothing typed into it
        session.screen.bracketed_paste = True  # the TUI takes the keyboard
        await asyncio.wait_for(nudge._task, 5.0)
        return nudge

    nudge = asyncio.run(run())
    assert nudge.delivered == ["w1"]


def test_a_session_working_again_is_left_alone(home, tmp_path, monkeypatch, instant):
    """Somebody else got there first — a human, a mesh delivery, a reminder."""
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path))
    session.statuses = ["idle", "busy"]  # armed, then driven by somebody
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert session.delivered == []
    assert nudge.delivered == []


def test_a_refused_delivery_is_tried_again(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path))
    session.deliver_ok = False

    async def run():
        nudge = resume.ResumeNudge(
            _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
        )
        nudge.start()
        await asyncio.sleep(0.05)
        assert nudge.delivered == []  # held, and still owed
        assert nudge.pending == ["w1"]
        session.deliver_ok = True
        await asyncio.wait_for(nudge._task, 5.0)
        return nudge

    nudge = asyncio.run(run())
    assert nudge.delivered == ["w1"]


def test_a_session_that_went_away_settles_quietly(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    dead = _Session("dead", str(tmp_path))
    dead.exited = True
    nudge = resume.ResumeNudge(
        _Manager({"dead": dead}), ["dead", "never-existed"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge.delivered == []
    assert nudge.pending == []


def test_a_session_that_never_becomes_ready_is_given_up_on(home, tmp_path,
                                                           monkeypatch, instant):
    """The window bounds it: a paste into a TUI still starting is never sent."""
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path))
    session.statuses = ["starting"]
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=0.05
    )
    asyncio.run(_drain(nudge))

    assert session.delivered == []
    assert nudge.pending == ["w1"]  # owed and unpaid, said so in the log


def test_the_switch_turns_the_whole_thing_off(home, tmp_path, monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    store.set_daemon_field("resume_nudge", False)
    assert store.daemon_config()["resume_nudge"] is False
    session = _Session("w1", str(tmp_path))
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge._task is None  # never started
    assert session.delivered == []


def test_nobody_to_nudge_starts_nothing(home, tmp_path):
    nudge = resume.ResumeNudge(_Manager({}), [], poll=0.01, window=5.0)
    asyncio.run(_drain(nudge))
    assert nudge._task is None


def test_a_non_tui_harness_is_not_waited_on_for_bracketed_paste(home, tmp_path,
                                                                monkeypatch, instant):
    monkeypatch.setattr(resume, "gate", lambda cwd, scope: True)
    session = _Session("w1", str(tmp_path), harness="py")
    session.screen.bracketed_paste = False  # a plain program never sets it
    nudge = resume.ResumeNudge(
        _Manager({"w1": session}), ["w1"], poll=0.01, window=5.0
    )
    asyncio.run(_drain(nudge))

    assert nudge.delivered == ["w1"]


# --------------------------------------------------------------------------- #
# the whole chain, on a real PTY
# --------------------------------------------------------------------------- #
CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


def test_a_restarted_daemon_nudges_the_session_that_was_working(home, tmp_path):
    """persist -> restore_all -> a real child reading the block off its stdin."""
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        mgr.create(SessionDef(name="mid", harness="py", cwd=str(tmp_path)))
        # The old daemon's last act: record what each session was doing. The
        # status is forced rather than raced — what is under test is the
        # nudge, not the idle tracker (tests/test_idle.py owns that).
        entries = [
            {
                "def": mgr.get("mid").sdef.to_dict(),
                "was_running": True,
                "was_busy": True,
            }
        ]
        await mgr.shutdown_all()
        paths.sessions_json().write_text(json.dumps(entries), encoding="utf-8")

        mgr2 = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        assert mgr2.restore_all() == []
        assert mgr2.resumed_busy == ["mid"]
        session = mgr2.get("mid")
        deadline = time.monotonic() + 15.0
        while "READY" not in "\n".join(session.capture()):
            assert time.monotonic() < deadline, "child never started"
            await asyncio.sleep(0.1)

        nudge = resume.ResumeNudge(mgr2, mgr2.resumed_busy, poll=0.05, window=20.0)
        nudge.start()
        try:
            await asyncio.wait_for(nudge._task, 20.0)
            assert nudge.delivered == ["mid"]
            deadline = time.monotonic() + 10.0
            while "session resume" not in "\n".join(session.capture()):
                assert time.monotonic() < deadline, (
                    "the block never reached the child:\n"
                    + "\n".join(session.capture())
                )
                await asyncio.sleep(0.1)
        finally:
            await nudge.shutdown()
            await mgr2.shutdown_all()

    asyncio.run(run())
