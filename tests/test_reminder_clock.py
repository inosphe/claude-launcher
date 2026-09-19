"""Session reminder delivery, its cflow source, and the API doors.

The clock's contract: a run sitting on the same agent-actionable position for
its interval gets that position's instructions typed into its session, and a
run that moves hears nothing. The first reminder at a position restates the
step; a repeat at that same position is delivered only while the session's
meaningful screen activity shows it is still working, and a terminal that has
not moved since the last reminder is re-armed instead. Configuration is
layered — machine defaults in the config file (read live), one run's override
in its own state — and both layers are exercised here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from claude_launcher import profile, store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow.engine import CflowError
from claude_launcher.daemon import cflow_clock, mesh_roles, rebrief, session_reminder
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

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
    """An isolated project with the linear workflow declared."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    return d


class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        self.status_value = "busy"  # an agent mid-work, the reminder's audience

    def status(self, threshold=None):
        return self.status_value

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True

    def reminders_paused(self) -> bool:
        return self.sdef.reminder_paused

    def set_reminder_pause(self, paused: bool) -> bool:
        self.sdef = dataclasses.replace(self.sdef, reminder_paused=bool(paused))
        return self.sdef.reminder_paused


class _ActivitySession(_FakeSession):
    def __init__(self, name: str, cwd: str) -> None:
        super().__init__(name, cwd)
        self.activity = "first"

    def last_activity_at(self):
        return self.activity


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]

    def list(self):
        return list(self._sessions.values())

    def persist(self):
        return None


# --------------------------------------------------------------------------- #
# the per-run override (engine)
# --------------------------------------------------------------------------- #
def test_role_source_has_its_own_machine_policy():
    cfg = store.daemon_config()
    assert session_reminder.role_reminder_policy(cfg) == (True, 600.0)
    assert session_reminder.role_reminder_policy({
        "role_reminder": False, "role_reminder_interval": 60,
    }) == (False, 60.0)


def test_set_reminder_merges_clears_and_validates(proj):
    cwd = str(proj)
    with pytest.raises(CflowError, match="no active cflow run"):
        cflow_engine.set_reminder(True, None, cwd=cwd, scope="w1")
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    out = cflow_engine.set_reminder(None, 60, cwd=cwd, scope="w1")
    assert out["reminder"] == {"interval": 60.0}
    # a later partial set merges over the earlier one
    out = cflow_engine.set_reminder(False, None, cwd=cwd, scope="w1")
    assert out["reminder"] == {"interval": 60.0, "enabled": False}
    # the override rides on status, so the clock and the dashboard see it
    assert cflow_engine.status(cwd, scope="w1")["reminder"] == {
        "interval": 60.0, "enabled": False,
    }
    with pytest.raises(CflowError, match="at least"):
        cflow_engine.set_reminder(None, 5, cwd=cwd, scope="w1")
    # both None clears it: back on the machine defaults
    out = cflow_engine.set_reminder(None, None, cwd=cwd, scope="w1")
    assert out["reminder"] is None
    assert "reminder" not in cflow_engine.status(cwd, scope="w1")


# --------------------------------------------------------------------------- #
# the clock's scan
# --------------------------------------------------------------------------- #
def test_no_screen_progress_suppresses_the_repeat(proj):
    """First sight arms; the interval fires; a still screen stays quiet.

    The run is stalled at the same position, so the first reminder fires —
    but with the session's meaningful screen marker unchanged, the next
    interval re-arms instead of repeating. Only screen progress or a
    position move lets the timer speak again.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _ActivitySession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    assert clock.scan(t) == []                      # armed, not fired
    assert clock.scan(t + 599) == []                # default 600 not yet up
    due = clock.scan(t + 601)
    assert [(c, s) for c, s, _, _ in due] == [(cwd, "w1")]
    assert "do one" in due[0][2]                    # the step's instructions
    assert "step 'one'" in due[0][2]
    asyncio.run(clock._deliver(*due[0]))            # the first reminder lands
    assert len(sess.delivered) == 1

    # neither the run nor its screen has moved: nothing repeats
    assert clock.scan(t + 1202) == []

    # meaningful screen progress re-activates the timer
    sess.activity = "second"
    due = clock.scan(t + 1803)
    assert [(c, s) for c, s, _, _ in due] == [(cwd, "w1")]

    # and the run moving re-arms the next position fresh
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert clock.scan(t + 1900) == []
    due = clock.scan(t + 1900 + 601)
    assert [(c, s) for c, s, _, _ in due] == [(cwd, "w1")]
    assert "do two" in due[0][2]


def test_defaults_and_override_layering(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    # machine default off silences every unconfigured run
    store.set_daemon_field("cflow_reminder", False)
    clock.scan(1000.0)
    assert clock.scan(5000.0) == []
    # a run's own override wins over the default, in both directions
    cflow_engine.set_reminder(True, 60, cwd=cwd, scope="w1")
    clock.scan(1000.0)                              # arm under the override
    assert clock.scan(1000.0 + 59) == []
    assert len(clock.scan(1000.0 + 61)) == 1
    cflow_engine.set_reminder(False, None, cwd=cwd, scope="w1")
    assert clock.scan(9000.0) == []


def test_only_agent_actionable_positions_remind(proj):
    """A run parked on a human's gate must not have step instructions typed
    at the agent — it is not allowed to enter the step."""
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "gated.yaml").write_text(
        "name: gated\n"
        "steps:\n"
        "  ship:\n"
        "    gate: human review\n"
        "    instructions: ship it\n",
        encoding="utf-8",
    )
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "waiting_approval"
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    clock.scan(1000.0)
    assert clock.scan(99999.0) == []


def test_reminder_block_restates_done_when():
    payload = {
        "status": "step", "workflow": "linear", "step_id": "impl",
        "visit": 1, "instructions": "implement it",
    }
    without = cflow_clock.reminder_block(payload, 180)
    assert "done when:" not in without
    assert "line is the test" not in without
    block = cflow_clock.reminder_block(
        {**payload, "done_when": "the diff is committed"}, 180
    )
    assert "done when: the diff is committed" in block
    assert "(the 'done when' line is the test)" in block


def test_reminder_block_for_a_branch_choice():
    block = cflow_clock.reminder_block(
        {
            "status": "select", "workflow": "linear", "step_id": "triage",
            "visit": 1, "prompt": "pick a path",
            "options": [{"name": "a", "description": "path a"}],
        },
        180,
    )
    assert "branch choice at step 'triage'" in block
    assert "pick a path" in block
    assert "- a: path a" in block
    assert "'select' tool" in block


def test_delivery_goes_to_the_scope_session_in_the_same_cwd(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    right = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": right}))
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 601)
    assert due
    asyncio.run(clock._deliver(*due[0]))
    assert len(right.delivered) == 1
    # a successful delivery re-arms (at the real clock, which _deliver reads):
    # well within one interval of the delivery, nothing is due again
    assert clock.scan(time.monotonic() + 100) == []

    # the same name in another directory is somebody else's session
    elsewhere = _FakeSession("w1", str(proj.parent))
    clock2 = cflow_clock.ReminderClock(_FakeManager({"w1": elsewhere}))
    assert clock2._session_for(cwd, "w1") is None
    elsewhere.exited = True
    assert clock2._session_for(str(proj.parent), "w1") is None


def test_only_a_working_session_is_reminded(proj):
    """A reminder held during an idle turn starts a fresh interval on resume."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    sess.status_value = "idle"                   # nobody is working here
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 601)
    assert due                                   # the debt is due...
    asyncio.run(clock._deliver(*due[0]))
    assert sess.delivered == []                  # ...but nothing is typed

    due = clock.scan(time.monotonic() + 700)
    assert due                                   # held, so still due next poll
    sess.status_value = "busy"                   # the agent starts working
    asyncio.run(clock._deliver(*due[0]))
    assert sess.delivered == []                  # resume does not replay it

    # The next reminder is measured from the resumed turn, not from the old
    # due time while the session was idle.
    assert clock.scan(time.monotonic() + 100) == []
    resumed_due = clock.scan(time.monotonic() + 601)
    assert resumed_due
    asyncio.run(clock._deliver(*resumed_due[0]))
    assert len(sess.delivered) == 1


def test_skip_lets_one_reminder_go_by_and_keeps_the_clock(proj):
    """The narrow verb beside the switch: re-arm once, change nothing.

    Its whole claim is that it is NOT a pause — so the two halves worth
    pinning are that the reminder about to be typed is not typed, and that a
    full interval later the next one is, with no override written anywhere.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)                                # arrival arms the timer
    assert clock.scan(t + 601)                   # ...and an interval later it is due

    assert clock.skip(cwd, "w1") is True         # let that one go by
    assert clock.scan(time.monotonic() + 100) == []   # nothing is due now

    # The clock is not off, only re-armed: a full interval from the skip the
    # next reminder is due exactly as it would have been.
    assert clock.scan(time.monotonic() + 601)

    # And nothing was written. A pause stores an override on the run (and is
    # archived with it); this stores nothing, which is the difference the
    # button in the header is offering.
    assert "reminder" not in cflow_engine.status(cwd, scope="w1")
    assert store.daemon_config().get("cflow_reminder") is not False


def test_skip_drops_a_reminder_held_for_a_stopped_session(proj):
    """The state a skip is worth the most in, and the one it is easiest to
    get wrong: a held reminder is due and retried EVERY poll, so re-arming
    without clearing the hold would land the very reminder just skipped, the
    moment the agent starts its next turn."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    sess.status_value = "idle"                   # nobody working: it will hold
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 601)
    assert due
    asyncio.run(clock._deliver(*due[0]))
    assert sess.delivered == []
    key = (cwd, "w1")
    assert clock.timers()[key]["held_ago"] is not None   # the debt is stamped

    assert clock.skip(cwd, "w1") is True
    assert clock.timers()[key]["held_ago"] is None       # ...and dropped

    sess.status_value = "busy"                   # the agent starts working
    assert clock.scan(time.monotonic() + 100) == []
    assert sess.delivered == []                  # the held reminder never lands


def test_skip_reports_when_there_was_nothing_to_skip(proj):
    """`False` is the answer whenever this clock keeps no timer for the run —
    and the two ways that happens are worth separating from a failure, because
    in both of them no reminder was coming for a skip to have stopped."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    # Never scanned: the table is empty, so there is nothing armed.
    assert clock.skip(cwd, "w1") is False

    # A position the clock stays out of (a human gate) drops the entry on the
    # pass that sees it — same answer, and for the better reason.
    (proj / ".claunch" / "workflows" / "gated.yaml").write_text(
        "name: gated\nsteps:\n  one:\n    gate: human review\n"
        "    instructions: ship it\n",
        encoding="utf-8",
    )
    cflow_engine.start("gated", cwd=cwd, scope="w2")
    clock.scan(time.monotonic())
    # One pass, two runs in the same directory: the working one keeps a timer
    # a skip can re-arm, the gated one is dropped from the table entirely.
    assert clock.skip(cwd, "w1") is True
    assert clock.skip(cwd, "w2") is False


# --------------------------------------------------------------------------- #
# the API doors
# --------------------------------------------------------------------------- #
def test_the_api_edits_defaults_and_per_run_overrides(proj):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        mm = MeshManager(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/cflow/reminder", headers=BEARER)
            defs = (await resp.json())["defaults"]
            assert defs == {"enabled": True, "interval": 600.0}

            resp = await client.put(
                "/api/cflow/reminder", headers=BEARER,
                json={"enabled": False, "interval": 240},
            )
            assert (await resp.json())["defaults"] == {
                "enabled": False, "interval": 240.0,
            }
            # persisted where the clock (and the CLI) read it, live
            assert store.daemon_config()["cflow_reminder"] is False

            resp = await client.put(
                "/api/cflow/reminder", headers=BEARER, json={"interval": 5},
            )
            assert resp.status == 400

            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "interval": 60},
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["reminder"] == {"interval": 60.0}
            assert body["defaults"]["interval"] == 240.0

            resp = await client.get(
                f"/api/cflow/run?cwd={cwd}&scope=w1", headers=BEARER,
            )
            detail = await resp.json()
            assert detail["run"]["reminder"] == {"interval": 60.0}
            assert detail["reminder_defaults"]["interval"] == 240.0

            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "clear": True},
            )
            assert (await resp.json())["reminder"] is None

            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1"},
            )
            assert resp.status == 400  # nothing to set
        finally:
            await client.close()

    asyncio.run(scenario())


def test_the_api_takes_enabled_alone_and_keeps_the_runs_interval(proj):
    """`enabled` with no interval: the shape the terminal header's chip sends.

    It is its own case because the chip has nothing to put in `interval` and
    must not invent one. Sending the effective value back would look harmless
    and would quietly convert a run that was FOLLOWING the machine default
    into one that has pinned today's value — the next change to the default
    would then skip that run, for a reason nobody could see. So the chip
    sends the one field it is actually changing, and this pins the two
    properties it relies on: the door accepts that shape, and the merge keeps
    the interval the run already had.
    """
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        mm = MeshManager(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # A run that has tuned its own interval, as the run page allows.
            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "interval": 90},
            )
            assert (await resp.json())["reminder"] == {"interval": 90.0}

            # Pause, the chip's way: one field, no interval.
            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "enabled": False},
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["reminder"] == {"interval": 90.0, "enabled": False}

            # And the stored override reads as "off, still every 90s" through
            # the very function the clock and the dashboard both consult —
            # reminder_policy takes the run's `reminder` and nothing else, so
            # feeding it what was just persisted is what the clock will do on
            # its next pass. A pause that silently reset the interval would
            # pass every assertion above and fail here.
            enabled, interval = cflow_clock.reminder_policy(
                {"reminder": body["reminder"]}, store.daemon_config()
            )
            assert (enabled, interval) == (False, 90.0)

            # Resume puts it back without having had to remember the 90.
            resp = await client.post(
                "/api/cflow/reminder", headers=BEARER,
                json={"cwd": cwd, "scope": "w1", "enabled": True},
            )
            assert (await resp.json())["reminder"] == {
                "interval": 90.0, "enabled": True,
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_the_skip_door_re_arms_the_clock_and_writes_nothing(proj):
    """`POST /api/cflow/reminder/skip` — the header chip's other press.

    Three properties, and the third is the one that makes it worth a door of
    its own: it re-arms the live clock, it answers honestly when there was
    nothing armed to re-arm, and it leaves the run's override alone. A skip
    that quietly wrote `enabled: false` would pass any test that only looked
    at whether the next reminder fired.
    """
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        mm = MeshManager(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # No clock on this daemon at all: reported, not answered
            # `skipped: false` — "nothing is coming from here, ever" and
            # "nothing was due just now" are different facts.
            resp = await client.post(
                "/api/cflow/reminder/skip", headers=BEARER,
                json={"cwd": cwd, "scope": "w1"},
            )
            assert resp.status == 503, await resp.json()

            app["cflow_clocks"] = {"reminder": clock, "ping": None}

            # Armed but never scanned into the table yet: nothing to skip.
            resp = await client.post(
                "/api/cflow/reminder/skip", headers=BEARER,
                json={"cwd": cwd, "scope": "w1"},
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["skipped"] is False

            t = time.monotonic()
            clock.scan(t)
            assert clock.scan(t + 601)           # the reminder is due

            resp = await client.post(
                "/api/cflow/reminder/skip", headers=BEARER,
                json={"cwd": cwd, "scope": "w1"},
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["skipped"] is True
            assert clock.scan(time.monotonic() + 100) == []

            # The switch is untouched: the run still carries no override, and
            # the policy the clock consults still says on, at the default.
            detail = await (await client.get(
                f"/api/cflow/run?cwd={cwd}&scope=w1", headers=BEARER,
            )).json()
            assert "reminder" not in detail["run"]
            assert cflow_clock.reminder_policy(
                detail["run"], store.daemon_config()
            ) == (True, 600.0)

            # A path that is not there is a bad request, refused before
            # anything takes the slot's lock -- as it is for every other
            # action door, the ten POSTs that share `_cflow_action_cwd`.
            #
            # Not to be read as "no run here". That case is a directory
            # that exists, so it passes `is_dir()`, reaches the handler,
            # and answers 200 with `skipped: false` -- the assertion
            # above under "Armed but never scanned into the table yet".
            # The 400 below is about the path, not about the run.
            resp = await client.post(
                "/api/cflow/reminder/skip", headers=BEARER,
                json={"cwd": str(proj / "nope"), "scope": "w1"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(scenario())


def test_session_reminder_header_doors_pause_resume_and_skip_both_sources(proj):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    profile.create("work")

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        session = mgr.stage(SessionDef(name="w1", profile="work", cwd=cwd))
        mm = MeshManager(mgr)
        mm.create("m")
        await mm.join("m", "w1", handle="w1", role="worker")
        cflow_engine.start("linear", cwd=cwd, scope="w1")
        service = session_reminder.SessionReminderService(mgr, mm)
        service.start()
        service.scan(1000.0)
        service.scan_roles(
            1000.0, {"role_reminder": True, "role_reminder_interval": 600.0}
        )

        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        app["session_reminder"] = service
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/sessions/w1/reminder", headers=BEARER, json={"paused": True}
            )
            body = await resp.json()
            assert resp.status == 200, body
            assert body["paused"] is True
            assert body["role"]["state"] == "paused"
            assert session.sdef.reminder_paused is True

            listed = await (await client.get("/api/sessions", headers=BEARER)).json()
            row = next(s for s in listed["sessions"] if s["name"] == "w1")
            assert row["session_reminder"]["paused"] is True

            resp = await client.post(
                "/api/sessions/w1/reminder/skip", headers=BEARER, json={}
            )
            assert (await resp.json())["skipped"] is False

            resp = await client.post(
                "/api/sessions/w1/reminder", headers=BEARER, json={"paused": False}
            )
            assert (await resp.json())["paused"] is False
            resp = await client.post(
                "/api/sessions/w1/reminder/skip", headers=BEARER, json={}
            )
            body = await resp.json()
            assert body["skipped"] is True
            assert body["sources"] == ["cflow", "role"]
        finally:
            await client.close()
            await service.shutdown()
            mgr.discard("w1")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# the two forms: full once, short after
# --------------------------------------------------------------------------- #
#: A step whose instructions are the length real workflows write — the case
#: the short form exists for. The toy ``linear`` steps above are a line each,
#: and against those the pointer is the bigger block (see the inversion test).
WORDY = """
name: wordy
steps:
  one:
    instructions: >
      {body}
    done_when: the diff is committed
    next: two
  two:
    instructions: do two
""".format(body="implement the thing carefully and completely. " * 20)


def test_the_step_is_restated_once_then_pointed_at(proj):
    """The load-bearing half of the push/pull split.

    The first reminder at a position pastes the step; every repeat there says
    the short thing and names the two pulls instead. Progress puts the full
    form back, because a new step has never been restated.
    """
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "wordy.yaml").write_text(
        WORDY, encoding="utf-8"
    )
    cflow_engine.start("wordy", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)

    first = clock.scan(t + 601)[0][2]
    assert "implement the thing carefully" in first   # the step itself
    assert "rebrief" not in first
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))

    second = clock.scan(time.monotonic() + 601)[0][2]
    assert "implement the thing carefully" not in second   # not said twice
    assert "short form" in second
    assert "'recall' tool with id" in second         # the pull is offered here
    assert "done when: the diff is committed" in second    # the test survives
    assert len(second) < len(first)

    # progress re-arms AND re-earns the full restatement
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    t2 = time.monotonic()
    clock.scan(t2)
    assert "do two" in clock.scan(t2 + 601)[0][2]


def test_a_step_too_small_to_shrink_keeps_its_restatement(proj):
    """The short form is taken only when it is actually shorter.

    Its protocol paragraph is a fixed cost, so against a one-line step the
    pointer is the bigger block — and paying more to be told less is the
    opposite of the point. The clock compares and keeps the full one.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")   # 'do one', one line
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    repeat = clock.scan(time.monotonic() + 601)[0][2]
    assert "do one" in repeat                       # still the restatement
    assert "short form" not in repeat


def test_a_reminder_that_was_never_typed_does_not_spend_the_restatement(proj):
    """The flag is stamped on delivery, not on composition.

    A reminder held for a stopped session (:func:`ReminderClock._deliver`) is
    one the agent never saw. Counting it would hand that agent the short form
    first, pointing it at a restatement it was never given.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    sess.status_value = "idle"
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    assert sess.delivered == []                     # held, never typed

    sess.status_value = "busy"
    due = clock.scan(time.monotonic() + 700)
    asyncio.run(clock._deliver(*due[0]))
    assert sess.delivered == []                     # resume re-arms, not replays

    # The re-armed interval elapses while the session keeps working. The
    # first reminder actually typed is still the full form: the held one was
    # never delivered, so it spent nothing.
    later = clock.scan(time.monotonic() + 700 + 601)
    asyncio.run(clock._deliver(*later[0]))
    assert "do one" in sess.delivered[0]            # still the full form


#: An ask on a later step, so a run has somewhere to be forced FROM.
ORPHAN = """
name: orphan
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
    ask:
      prompt: may it land?
      from: [{role: leader}]
"""


def test_a_decision_that_reached_nobody_never_shrinks(proj):
    """The one position whose block is news, not a restatement.

    A run forced onto a delegated ask with ``goto`` reports waiting_answer
    while nobody holds the question. The block's whole content is that fact —
    nothing the agent already has, so nothing a pointer can stand in for.
    """
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "orphan.yaml").write_text(
        ORPHAN, encoding="utf-8"
    )
    cflow_engine.start("orphan", cwd=cwd, scope="w1")
    cflow_engine.goto("two", cwd=cwd, scope="w1")
    assert cflow_clock._ask_reached_nobody(
        cflow_engine.status(cwd, scope="w1")
    )

    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    repeat = clock.scan(time.monotonic() + 601)[0][2]
    assert "never actually put to anyone" in repeat  # the diagnosis, again
    assert "short form" not in repeat


def test_repeat_block_keeps_the_completion_test_and_names_the_pull():
    payload = {
        "status": "step", "workflow": "linear", "step_id": "impl", "visit": 2,
        "instructions": "implement it", "done_when": "the diff is committed",
        "digest": "abc123abc123",
    }
    block = cflow_clock.repeat_block(payload, 300, 1500.0)
    assert "implement it" not in block              # the point of the form
    assert "done when: the diff is committed" in block
    assert "step 'impl' (visit 2), unmoved for ~25 min" in block
    assert "step text id: abc123abc123" in block
    assert "Look for abc123abc123 in this conversation" in block
    assert "'recall' tool with id abc123abc123" in block
    assert "'report' then 'next'" in block


def test_repeat_block_without_an_id_falls_back_to_status():
    """A position with no instructional content to name (a bare ask) has no
    id, and then there is no predicate to offer -- so the block says the one
    thing that is still true instead of quoting an id it does not have."""
    block = cflow_clock.repeat_block(
        {"status": "step", "workflow": "linear", "step_id": "impl", "visit": 1},
        300, 900.0,
    )
    assert "step text id" not in block
    assert "recall" not in block
    assert "'status' tool restates this step in full" in block


def test_repeat_block_for_a_branch_choice_points_at_select():
    block = cflow_clock.repeat_block(
        {
            "status": "select", "workflow": "linear", "step_id": "triage",
            "visit": 1, "prompt": "pick a path",
            "options": [{"name": "a", "description": "path a"}],
        },
        300, 900.0,
    )
    assert "branch choice at step 'triage', unmoved for ~15 min" in block
    assert "pick a path" not in block               # 'status' serves it
    assert "restates this choice and its options" in block
    assert "'select' is what moves it" in block


def test_timers_report_which_form_comes_next(proj):
    """The readout says which block the next fire is, not only when.

    A reader watching this countdown is deciding whether to let the clock
    speak, and "due in 40s" means a different thing at 1.5k characters than
    at 0.6k.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    assert clock.timers()[(cwd, "w1")]["restated"] is False
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    assert clock.timers()[(cwd, "w1")]["restated"] is True


# --------------------------------------------------------------------------- #
# the content id: quoted instead of pasted, and pulled back by 'recall'
# --------------------------------------------------------------------------- #
def test_the_digest_names_content_and_ignores_framing():
    """It must survive the things that move on every fire, and only those."""
    base = {"status": "step", "instructions": "do it", "done_when": "it is done",
            "verify": "make test"}
    d = cflow_engine.step_digest(base)
    assert d and len(d) == cflow_engine.DIGEST_CHARS
    # framing moves, the id does not
    assert cflow_engine.step_digest(
        {**base, "visit": 9, "workflow": "other", "step_id": "elsewhere"}
    ) == d
    # content moves, the id does
    for key in ("instructions", "done_when", "verify"):
        assert cflow_engine.step_digest({**base, key: base[key] + "!"}) != d
    # a chooser is named by what the chooser reads
    sel = {"status": "select", "prompt": "pick", "options": [{"name": "a",
                                                             "description": "A"}]}
    assert cflow_engine.step_digest(sel)
    assert cflow_engine.step_digest(
        {**sel, "options": [{"name": "a", "description": "B"}]}
    ) != cflow_engine.step_digest(sel)
    # nothing instructional to name
    assert cflow_engine.step_digest({"status": "waiting_approval"}) == ""


def test_every_door_that_hands_over_a_position_carries_its_id(proj):
    """``next`` and ``status`` must agree, or the agent cannot match the id it
    was reminded of against the text it was originally given."""
    cwd = str(proj)
    first = cflow_engine.start("linear", cwd=cwd, scope="w1")
    assert first["digest"]
    assert cflow_engine.status(cwd, scope="w1")["digest"] == first["digest"]
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    second = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert second["digest"] and second["digest"] != first["digest"]


def test_the_first_fire_carries_the_id_with_the_text_and_repeats_quote_it(proj):
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "wordy.yaml").write_text(WORDY, encoding="utf-8")
    cflow_engine.start("wordy", cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    d = payload["digest"]
    full = cflow_clock.reminder_block(payload, 300.0)
    short = cflow_clock.repeat_block(payload, 300.0, 1500.0)
    # the id arrives ATTACHED to what it names -- otherwise there is nothing
    # for the agent to have matched it against later
    assert f"step text id: {d}" in full
    assert "implement the thing carefully" in full
    # ...and the repeat quotes the id in place of the body
    assert f"step text id: {d}" in short
    assert "implement the thing carefully" not in short
    assert f"'recall' tool with id {d}" in short
    assert f"Look for {d} in this conversation" in short


def test_recall_hands_the_text_back_and_refuses_a_stale_id(proj):
    """The middle answer is the one that makes this more than 'status'."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    d = cflow_engine.status(cwd, scope="w1")["digest"]

    got = cflow_engine.recall(d, cwd=cwd, scope="w1")
    assert got["status"] == "recalled"
    assert got["id"] == d and got["step_id"] == "one"
    assert got["instructions"] == "do one"

    stale = cflow_engine.recall("0" * 12, cwd=cwd, scope="w1")
    assert stale["status"] == "stale_id"
    assert stale["current_id"] == d
    assert "instructions" not in stale          # never serve the wrong step

    # the run moves: the id the agent still holds is now the wrong one, and
    # answering it with text would put the agent back on a step it has left
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    after = cflow_engine.recall(d, cwd=cwd, scope="w1")
    assert after["status"] == "stale_id"
    assert after["step_id"] == "two"
    assert "instructions" not in after


def test_recall_is_read_only_about_an_idle_slot_and_needs_an_id(proj):
    cwd = str(proj)
    assert cflow_engine.recall("abc", cwd=cwd, scope="w1")["status"] == "idle"
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    with pytest.raises(CflowError):
        cflow_engine.recall("", cwd=cwd, scope="w1")
    # and it does not deliver the step or open anything: the position is
    # untouched by having been recalled
    before = cflow_engine.status(cwd, scope="w1")
    cflow_engine.recall(before["digest"], cwd=cwd, scope="w1")
    after = cflow_engine.status(cwd, scope="w1")
    assert (before["status"], before["visit"]) == (after["status"], after["visit"])


# --------------------------------------------------------------------------- #
# the volatile half: pushed in full, because no id could stay true for it
# --------------------------------------------------------------------------- #
class _KinManager(_FakeManager):
    """A manager that also answers the kin questions the situation asks."""

    def __init__(self, sessions: dict, *, children=(), parent_exited=None) -> None:
        super().__init__(sessions)
        self._children = list(children)
        self._parent_exited = parent_exited

    def live_children(self, name: str):
        return list(self._children)

    def get(self, name: str):
        if self._parent_exited is not None and name == self._parent_exited:
            gone = _FakeSession(name, "")
            gone.exited = True
            return gone
        return super().get(name)


class _FakeMesh:
    def __init__(self, name, owed, roleset=None):
        self.name = name
        self._owed = owed
        # The vocabulary in force on this mesh. Defaults to the packaged one
        # so a test that only cares about `owed` needs to say nothing.
        self.roleset = roleset if roleset is not None else mesh_roles.resolve()

    def owed(self, handle):
        return self._owed


class _FakeMeshMgr:
    def __init__(self, mesh, handle="w1", role="worker"):
        self._mesh = mesh
        self._handle = handle
        self._role = role

    def meshes_for_session(self, name):
        return [{"mesh": self._mesh.name}]

    def get(self, name):
        return self._mesh

    def member_for_session(self, mesh, name):
        return type("M", (), {"handle": self._handle, "role": self._role})()


def test_a_quiet_session_adds_nothing(proj):
    """The ordinary case, and the one the measured size depends on."""
    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    assert session_reminder.situation_lines("w1", mgr, None, 0) == []


def test_the_situation_states_owed_asks_children_and_a_dead_parent(proj):
    sess = _FakeSession("w1", str(proj))
    sess.sdef = SessionDef(name="w1", cwd=str(proj), parent="lead")
    mgr = _KinManager({"w1": sess}, children=["c1", "c2"], parent_exited="lead")
    mm = _FakeMeshMgr(_FakeMesh("m0", [{"id": "a"}, {"id": "b"}]))
    lines = session_reminder.situation_lines("w1", mgr, mm, 3)
    joined = "\n".join(lines)
    assert "there is no id that could stay true for it" in lines[0]
    assert "asks: 3 delegated decision(s)" in joined
    assert "owed: 2 delivered message(s) on mesh m0" in joined
    assert "children: c1, c2 still running" in joined
    assert "parent: lead has exited" in joined


def test_a_broken_roster_never_sinks_the_reminder(proj):
    """Decoration must not cost a delivery."""
    class _Exploding:
        def meshes_for_session(self, name):
            raise RuntimeError("mesh registry mid-write")

    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess}, children=["c1"])
    lines = session_reminder.situation_lines("w1", mgr, _Exploding(), 0)
    assert any("children: c1" in ln for ln in lines)   # the rest still stands


def test_the_reminder_carries_the_situation_when_delivered(proj):
    """It rides the block that actually lands, composed on the loop."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    mgr = _KinManager({"w1": sess}, children=["c1"])
    clock = cflow_clock.ReminderClock(mgr, _FakeMeshMgr(_FakeMesh("m0", [{"id": "a"}])))
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 601)
    assert "children:" not in due[0][2]          # not composed in the thread
    asyncio.run(clock._deliver(*due[0]))
    landed = sess.delivered[0]
    assert "children: c1 still running" in landed
    assert "owed: 1 delivered message(s) on mesh m0" in landed
    assert landed.splitlines()[-1] == "---"      # and it is still one block


def test_the_session_ids_ride_the_full_form_and_not_the_repeat(proj):
    """Where the session-level ids are named, and why only there.

    The full form is the fire that hands over text; naming the other blocks
    the agent was handed belongs beside it. The repeat is 777 characters
    against the full block's 1545, and that gap is the product -- a
    reference line on every repeat would spend a third of it on a question
    the hook has usually just answered by re-delivering those blocks.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    sess.sdef = SessionDef(name="w1", cwd=cwd, task="count the beans")
    clock = cflow_clock.ReminderClock(_KinManager({"w1": sess}))
    ident = rebrief.block_digest("count the beans")

    t = time.monotonic()
    clock.scan(t)
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    first = sess.delivered[0]
    assert f"session text ids: {ident} (task)" in first
    assert "## Context" in first
    assert "attached text" in first          # the bare mention does not count
    assert first.splitlines()[-1] == "---"   # one session-level fence

    asyncio.run(clock._deliver(*clock.scan(time.monotonic() + 601)[0]))
    repeat = sess.delivered[1]
    assert "session text ids:" not in repeat
    assert ident not in repeat


def test_a_session_with_nothing_addressable_says_nothing(proj):
    """A task-less, mesh-less session has no ids, so the line is absent
    rather than empty -- the reminder's size is the reason."""
    sess = _FakeSession("w1", str(proj))
    assert session_reminder.context_id_lines(
        "w1", _KinManager({"w1": sess}), None
    ) == []


def test_a_broken_roster_never_costs_the_ids_a_delivery(proj):
    """Same trade as the situation lines: decoration must not sink a send."""
    class _Exploding:
        def meshes_for_session(self, name):
            raise RuntimeError("mesh registry mid-write")

    sess = _FakeSession("w1", str(proj))
    sess.sdef = SessionDef(name="w1", cwd=str(proj), task="count the beans")
    mgr = _KinManager({"w1": sess})
    lines = session_reminder.context_id_lines("w1", mgr, _Exploding())
    assert lines and "(task)" in lines[0]     # the task id still stands


def test_role_and_cflow_are_peer_sections_in_every_step_reminder(proj):
    """The role identity remains visible while its long correction stays
    priced at one fire per cflow position.

    The measurement this is priced on: the full block cuts the step's own
    instructions at _INSTRUCTIONS_LIMIT, so it is already over budget. A
    worker stance pasted whole would add 36% to it and a leader stance 156%,
    which is why what rides here is the role's cflow_reminder line and never
    the stance.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    sess.sdef = SessionDef(name="w1", cwd=cwd, task="count the beans")
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    clock = cflow_clock.ReminderClock(_KinManager({"w1": sess}), mesh_mgr)
    worker = mesh_roles.resolve().get("worker")

    t = time.monotonic()
    clock.scan(t)
    asyncio.run(clock._deliver(*clock.scan(t + 601)[0]))
    first = sess.delivered[0]
    assert "## Role" in first and "## Cflow" in first
    assert first.index("## Role") < first.index("## Cflow")
    assert "role: worker on m" in first
    assert "stance text id:" in first
    assert worker.cflow_reminder in first
    # The STANCE is what this deliberately does not send.
    assert worker.stance.split("\n")[0].strip() not in first
    assert first.splitlines()[-1] == "---"      # spliced inside the fence

    asyncio.run(clock._deliver(*clock.scan(time.monotonic() + 601)[0]))
    repeat = sess.delivered[1]
    assert "## Role" in repeat and "## Cflow" in repeat
    assert "role: worker on m" in repeat
    assert "stance text id:" in repeat
    assert worker.cflow_reminder not in repeat


def test_role_source_reminds_a_session_with_no_cflow_run(proj):
    """Role recovery has its own key and does not require a run to exist."""
    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)

    asyncio.run(service.tick(1000.0))       # first sight arms the role source
    assert sess.delivered == []
    asyncio.run(service.tick(1601.0))

    assert len(sess.delivered) == 1
    block = sess.delivered[0]
    assert "# claunch session: reminder" in block
    assert "## Role" in block
    assert "role: worker on m" in block
    assert "## Cflow" not in block


def test_role_source_skips_repeats_when_session_has_not_moved(proj):
    sess = _ActivitySession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0}

    base = time.monotonic()
    asyncio.run(service.tick(base))
    asyncio.run(service.tick(base + 601.0))
    assert len(sess.delivered) == 1

    # The timer reaches its next interval, but the terminal's meaningful
    # screen marker is unchanged, so no second pending reminder is produced.
    asyncio.run(service.tick(base + 1202.0))
    assert len(sess.delivered) == 1

    sess.activity = "second"
    asyncio.run(service.tick(base + 1803.0))
    assert len(sess.delivered) == 2


def test_cflow_source_skips_repeats_when_session_has_not_moved(proj):
    """The cflow reminder applies the same no-progress rule as Role.

    A session stalled at the same position hears the first reminder, but a
    static screen is re-armed rather than typed into every interval; the
    timer speaks again only once the screen moves.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _ActivitySession("w1", cwd)
    service = session_reminder.SessionReminderService(_KinManager({"w1": sess}))

    base = time.monotonic()
    asyncio.run(service.tick(base))
    asyncio.run(service.tick(base + 601.0))
    assert len(sess.delivered) == 1

    # The interval elapses again, but the meaningful screen marker is
    # unchanged, so no second pending reminder is produced.
    asyncio.run(service.tick(base + 1202.0))
    assert len(sess.delivered) == 1

    sess.activity = "second"
    asyncio.run(service.tick(base + 1803.0))
    assert len(sess.delivered) == 2


def test_due_role_and_cflow_sources_share_one_delivery(proj):
    """Independent timers are batched at the terminal boundary."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)

    asyncio.run(service.tick(1000.0))
    asyncio.run(service.tick(1601.0))

    assert len(sess.delivered) == 1
    assert sess.delivered[0].count("# claunch session: reminder") == 1
    assert "## Role" in sess.delivered[0]
    assert "## Cflow" in sess.delivered[0]


def test_session_pause_holds_both_sources_and_resume_starts_fresh(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)

    asyncio.run(service.tick(1000.0))
    assert service.set_paused("w1", True, now=1001.0) is True
    asyncio.run(service.tick(1602.0))
    assert sess.delivered == []

    assert service.set_paused("w1", False, now=1602.0) is False
    asyncio.run(service.tick(2201.0))
    assert sess.delivered == []
    asyncio.run(service.tick(2203.0))
    assert len(sess.delivered) == 1
    assert "## Role" in sess.delivered[0]
    assert "## Cflow" in sess.delivered[0]


def test_session_skip_rearms_active_role_and_cflow_sources(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)

    asyncio.run(service.tick(1000.0))
    assert service.skip_session("w1", now=1300.0) == ["cflow", "role"]
    asyncio.run(service.tick(1601.0))
    assert sess.delivered == []
    asyncio.run(service.tick(1901.0))
    assert len(sess.delivered) == 1


def test_role_timer_status_reports_session_pause_without_a_cflow_run(proj):
    class _Running:
        @staticmethod
        def done():
            return False

    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    service = session_reminder.SessionReminderService(
        mgr, _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    )
    service._task = _Running()
    service.scan_roles(
        1000.0, {"role_reminder": True, "role_reminder_interval": 600.0}
    )

    status = service.status(
        "w1",
        now=1120.0,
        cfg={"role_reminder": True, "role_reminder_interval": 600.0},
    )
    assert status["paused"] is False
    assert status["role"]["state"] == "counting"
    assert status["role"]["due_in"] == 480.0

    service.set_paused("w1", True, now=1120.0)
    status = service.status(
        "w1",
        now=1120.0,
        cfg={"role_reminder": True, "role_reminder_interval": 600.0},
    )
    assert status["paused"] is True
    assert status["role"]["state"] == "paused"


def test_cflow_progress_does_not_rearm_the_role_source(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0}

    service.scan(1000.0)
    service.scan_roles(1000.0, cfg)
    cflow_engine.report("one done", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert service.scan(1100.0) == []  # cflow source reaches a new position

    due = service.scan_roles(1601.0, cfg)
    assert [name for name, _entries in due] == ["w1"]


def test_role_change_is_due_without_waiting_for_the_old_interval(proj):
    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0}
    assert service.scan_roles(1000.0, cfg) == []

    mesh_mgr._role = "reviewer"
    due = service.scan_roles(1001.0, cfg)
    assert due[0][1][0]["name"] == "reviewer"


def test_role_source_advances_only_after_successful_delivery(proj):
    sess = _FakeSession("w1", str(proj))
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    service = session_reminder.SessionReminderService(mgr, mesh_mgr)
    cfg = {"role_reminder": True, "role_reminder_interval": 600.0}
    assert service.scan_roles(1000.0, cfg) == []
    due = service.scan_roles(1601.0, cfg)
    assert due
    armed = service._roles["w1"]["at"]

    async def refuse(_text):
        return False

    sess.deliver = refuse
    asyncio.run(service._deliver_session("w1", roles=due[0][1], role_due=True))
    assert service._roles["w1"]["at"] == armed

    async def accept(text):
        sess.delivered.append(text)
        return True

    sess.deliver = accept
    asyncio.run(service._deliver_session("w1", roles=due[0][1], role_due=True))
    assert service._roles["w1"]["at"] > armed


def test_role_source_reads_mesh_then_legacy_compatibility_data(proj):
    sess = _FakeSession("w1", str(proj))
    sess.sdef = SessionDef(name="w1", cwd=str(proj), role="leader")
    mgr = _KinManager({"w1": sess})
    entries = session_reminder.role_entries("w1", mgr, None)
    assert [(e["mesh"], e["name"]) for e in entries] == [("", "leader")]
    assert mesh_roles.resolve().get("leader").cflow_reminder in "\n".join(
        session_reminder.role_section(entries, cflow_guidance=True)
    )

    # Membership data is authoritative when it exists.
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0), role="reviewer")
    entries = session_reminder.role_entries("w1", mgr, mesh_mgr)
    assert [(e["mesh"], e["name"]) for e in entries] == [("m", "reviewer")]


def test_role_identity_survives_an_empty_guidance_line_and_broken_roster(proj):
    quiet = mesh_roles.resolve(
        mesh_roles.parse("roles: {worker: {stance: build it}}")
    )
    sess = _FakeSession("w1", str(proj))
    sess.sdef = SessionDef(name="w1", cwd=str(proj))
    mgr = _KinManager({"w1": sess})
    mesh_mgr = _FakeMeshMgr(_FakeMesh("m", 0, roleset=quiet), role="worker")
    entries = session_reminder.role_entries("w1", mgr, mesh_mgr)
    lines = session_reminder.role_section(entries, cflow_guidance=True)
    assert lines[0] == "role: worker on m"
    assert "at this cflow position:" not in "\n".join(lines)
    # No role at all, no mesh: nothing to say.
    assert session_reminder.role_entries("w1", mgr, None) == []

    class _Exploding:
        def meshes_for_session(self, name):
            raise RuntimeError("mesh registry mid-write")

    sess.sdef = SessionDef(name="w1", cwd=str(proj), role="worker")
    entries = session_reminder.role_entries("w1", mgr, _Exploding())
    assert [(e["mesh"], e["name"]) for e in entries] == [("", "worker")]


def test_recall_is_journalled_so_the_pull_rate_can_be_measured(proj):
    """The design is priced on how often this is called; until it is recorded
    that number is a guess."""
    import json
    from claude_launcher.cflow import state as cflow_state

    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    d = cflow_engine.status(cwd, scope="w1")["digest"]
    cflow_engine.recall(d, cwd=cwd, scope="w1")
    cflow_engine.recall("0" * 12, cwd=cwd, scope="w1")
    entries = [
        json.loads(ln)
        for ln in cflow_state.journal_path(cwd, "w1").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    recalls = [e for e in entries if e["event"] == "recall"]
    assert [e["hit"] for e in recalls] == [True, False]
    assert recalls[0]["id"] == d and recalls[0]["step"] == "one"


# --------------------------------------------------------------------------- #
# the idle-session loop: a reminder must not keep its own timer alive
# --------------------------------------------------------------------------- #
class _AnsweringSession(_ActivitySession):
    """A terminal that answers a delivery and then does nothing else.

    Models the two things the real :class:`Session` does and the plain fake
    does not.  The screen moves *because of* the delivery — the submit
    repaint and the turn both land after ``deliver()`` has returned, so the
    marker read at the next scan is always later than the one ``_mark_role``
    recorded.  And the screen then stays still, which ``idle_since`` reports
    as a duration that keeps growing.

    Reconstructed from session s602 (2026-09-19): 97 reminders, 0.4–7.2 s of
    turn time each, and no other output between them.
    """

    def __init__(self, name, cwd, *, work: float = 5.0) -> None:
        super().__init__(name, cwd)
        self.work = work          # seconds the terminal moves per delivery
        self.delivered_at: list = []
        self.now = 0.0            # the test's clock, in the same units
        self._pending_paint = False

    async def deliver(self, text):
        ok = await super().deliver(text)
        self.delivered_at.append(self.now)
        # The repaint the delivery causes is observed by the sampler after
        # the write returns, so the value _mark_role reads is still the old
        # one.  The next reader sees the new screen.
        self._pending_paint = True
        return ok

    def last_activity_at(self):
        if self._pending_paint:
            self._pending_paint = False
            return self.activity
        if self.delivered_at:
            self.activity = f"paint-{len(self.delivered_at)}"
        return self.activity

    def idle_since(self):
        if not self.delivered_at:
            return None
        return max(0.0, self.now - self.delivered_at[-1] - self.work)


def test_role_reminder_stops_once_the_session_only_answers_it(proj):
    """The marker moves on every delivery; the idle duration does not lie."""
    sess = _AnsweringSession("w1", str(proj))
    service = session_reminder.SessionReminderService(
        _KinManager({"w1": sess}), _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    )
    base = time.monotonic()
    for i in range(12):
        sess.now = base + 601.0 * i
        asyncio.run(service.tick(sess.now))
    # One reminder reaches a terminal that then does nothing but take it in;
    # the timer is re-armed from there rather than typed into every interval.
    assert len(sess.delivered) == 1


def test_role_reminder_repeats_while_the_session_is_actually_working(proj):
    """Real work between two reminders is what the repeat exists for."""
    sess = _AnsweringSession("w1", str(proj), work=400.0)
    service = session_reminder.SessionReminderService(
        _KinManager({"w1": sess}), _FakeMeshMgr(_FakeMesh("m", 0), role="worker")
    )
    base = time.monotonic()
    for i in range(4):
        sess.now = base + 601.0 * i
        asyncio.run(service.tick(sess.now))
    assert len(sess.delivered) == 3


def test_cflow_reminder_stops_once_the_session_only_answers_it(proj):
    """The cflow source carries the same rule as Role."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _AnsweringSession("w1", cwd)
    service = session_reminder.SessionReminderService(_KinManager({"w1": sess}))
    base = time.monotonic()
    for i in range(12):
        sess.now = base + 601.0 * i
        asyncio.run(service.tick(sess.now))
    assert len(sess.delivered) == 1
