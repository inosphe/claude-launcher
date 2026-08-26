"""What the two typing clocks are about to do, as the dashboard reports it.

The reminder and the stall ping are the only things in claunch that put text
into a session's terminal on a timer with nobody asking. Until now the only
thing published about them was their *configuration* — on/off and an interval
— and that is the one fact which cannot answer the question a person watching
a terminal actually has: is this thing going to fire, and when. Configured and
armed, configured and correctly silent, and configured with a dead tick all
read identically from a config file.

So this pins the readout, not the clocks (their firing rules are held in
``test_reminder_clock.py`` and ``test_stall_ping_clock.py``). Three layers,
kept apart on purpose and checked apart here:

* :func:`cflow_clock.reminder_policy` / :func:`cflow_clock.ping_policy` — the
  effective settings, floors included. Shared by the clock and the readout so
  the two cannot disagree about the interval.
* ``Clock.timers()`` — the monotonic stamps in memory, as ages.
* ``api._cflow_timers`` — the two above plus the run's position and its
  session's status, resolved into one word a reader can act on.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}

#: "argument not given", so a test can pass config=None and mean it — None is
#: exactly the value under test (an unreadable config file).
_KEEP = object()

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
  one:
    instructions: do one
    next: two
  two:
    ask: {prompt: "ship it"}
    instructions: do two
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(LINEAR, encoding="utf-8")
    (d / ".claunch" / "workflows" / "gated.yaml").write_text(GATED, encoding="utf-8")
    return d


class _FakeSession:
    def __init__(self, name: str, cwd: str, status: str = "busy") -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        self.status_value = status

    def status(self, threshold=None):
        return self.status_value

    async def deliver(self, text: str) -> bool:
        self.delivered.append(text)
        return True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]


def _snaps(reminder=None, ping=None) -> dict:
    """The shape :func:`api._clock_snapshots` hands to the composer."""
    return {
        "reminder": reminder or {"running": True, "timers": {}},
        "ping": ping or {"running": True, "timers": {}},
    }


# --------------------------------------------------------------------------- #
# the effective settings, one rule for both readers
# --------------------------------------------------------------------------- #
def test_reminder_policy_layers_the_override_and_applies_the_floor():
    cfg = {"cflow_reminder": True, "cflow_reminder_interval": 600.0}
    assert cflow_clock.reminder_policy({}, cfg) == (True, 600.0)
    # the run's own override wins, in both directions
    assert cflow_clock.reminder_policy({"reminder": {"enabled": False}}, cfg) == (
        False, 600.0,
    )
    assert cflow_clock.reminder_policy({"reminder": {"interval": 90}}, cfg) == (
        True, 90.0,
    )
    # ...and the floor is part of the answer, not of enforcement: a run
    # overridden below it is NOT reminded that often, and a readout that
    # promised 5s would be wrong in the direction nobody can check.
    assert cflow_clock.reminder_policy({"reminder": {"interval": 5}}, cfg) == (
        True, cflow_engine.REMINDER_MIN_INTERVAL,
    )
    # zero is the signal-only configuration and must survive the floor: it
    # means "never repeat", not "repeat as fast as allowed".
    assert cflow_clock.reminder_policy({"reminder": {"interval": 0}}, cfg) == (
        True, 0.0,
    )


def test_ping_policy_is_machine_wide_and_off_without_an_interval():
    assert cflow_clock.ping_policy({"cflow_ping": False}) == (False, 0.0)
    assert cflow_clock.ping_policy(
        {"cflow_ping": True, "cflow_ping_interval": 0}
    ) == (False, 0.0)
    assert cflow_clock.ping_policy(
        {"cflow_ping": True, "cflow_ping_interval": 900}
    ) == (True, 900.0)
    assert cflow_clock.ping_policy(
        {"cflow_ping": True, "cflow_ping_interval": 10}
    ) == (True, cflow_clock.PING_MIN_INTERVAL)


# --------------------------------------------------------------------------- #
# the clocks' own tables
# --------------------------------------------------------------------------- #
def test_reminder_timers_report_arming_firing_and_the_hold(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    session = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": session}))
    key = (cwd, "w1")

    # Nothing scanned yet: no timer, and no invented one.
    assert clock.timers(1000.0) == {}
    # A clock that was never started is not running, and says so — this is
    # the whole difference between "reminders are on" and "reminders happen".
    assert clock.running is False

    clock.scan(1000.0)
    t = clock.timers(1000.0)[key]
    assert t["armed_ago"] == 0.0
    assert t["fired_ago"] is None and t["held_ago"] is None
    # the age is measured against the caller's clock, not re-read
    assert clock.timers(1240.0)[key]["armed_ago"] == 240.0

    due = clock.scan(1000.0 + 601)
    assert len(due) == 1
    asyncio.run(clock._deliver(cwd, "w1", due[0][2], "reminder"))
    assert session.delivered
    fired = clock.timers()[key]
    assert fired["fired_kind"] == "reminder"
    assert fired["fired_ago"] is not None and fired["fired_ago"] < 5
    # delivery re-arms, so the countdown restarts from the delivery
    assert fired["armed_ago"] < 5

    # A due reminder into a session that stopped is HELD, and the hold is
    # stamped: armed, configured, correctly silent — indistinguishable from
    # broken unless the hold itself is reported.
    session.status_value = "idle"
    due = clock.scan(time.monotonic() + 601)
    asyncio.run(clock._deliver(cwd, "w1", due[0][2], "reminder"))
    assert len(session.delivered) == 1        # nothing new was typed
    assert clock.timers()[key]["held_ago"] is not None


def test_ping_timers_say_when_they_are_not_counting(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping", True)
    store.set_daemon_field("cflow_ping_interval", 900.0)
    session = _FakeSession("w1", cwd, status="busy")
    clock = cflow_clock.StallPingClock(_FakeManager({"w1": session}))
    key = (cwd, "w1")

    clock.scan(1000.0)
    # Somebody is at work: this clock re-arms every pass, so a countdown drawn
    # from armed_ago alone would sit at the top and read as frozen.
    assert clock.timers(1000.0)[key]["working"] is True
    clock.scan(2000.0)
    assert clock.timers(2000.0)[key]["armed_ago"] == 0.0

    session.status_value = "idle"
    clock.scan(3000.0)
    assert clock.timers(3000.0)[key]["working"] is False
    # The stretch is measured from the LAST WORKING PASS (t=2000), not from
    # the pass that first found the session stopped: the agent stopped some
    # time inside that gap, and counting from the discovery would hand every
    # stall a free interval it did not earn.
    assert clock.timers(3600.0)[key]["armed_ago"] == 1600.0
    assert clock.scan(2000.0 + 901)       # and it fires past the interval


# --------------------------------------------------------------------------- #
# the composed readout
# --------------------------------------------------------------------------- #
def test_the_readout_names_every_way_a_clock_can_be_quiet(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    busy = _FakeManager({"w1": _FakeSession("w1", cwd, status="busy")})
    cfg = {"cflow_reminder": True, "cflow_reminder_interval": 600.0}

    def read(manager=busy, snaps=None, config=_KEEP, pay=None):
        return api_mod._cflow_timers(
            manager, snaps or _snaps(), cfg if config is _KEEP else config,
            cwd, "w1", pay or payload,
        )

    # No clock object at all (an app built without the ticks): stopped, and
    # never a number.
    out = api_mod._cflow_timers(
        busy, {"reminder": {"running": False, "timers": {}},
               "ping": {"running": False, "timers": {}}},
        cfg, cwd, "w1", payload,
    )
    assert out["reminder"]["state"] == "stopped"
    assert out["reminder"]["due_in"] is None

    # Running, enabled, but the tick has not seen this run yet.
    assert read()["reminder"]["state"] == "arming"

    # Running and enabled with a timer: counting, and the remaining seconds
    # are the interval less the age — the number the strip draws.
    snaps = _snaps(reminder={
        "running": True,
        "timers": {(cwd, "w1"): {"armed_ago": 240.0, "fired_ago": None,
                                 "fired_kind": None, "held_ago": None,
                                 "probed_ago": None, "probe_code": None}},
    })
    rem = read(snaps=snaps)["reminder"]
    assert (rem["state"], rem["due_in"], rem["interval"]) == ("counting", 360.0, 600.0)

    # Past the interval with the session working: due. With it stopped: held —
    # the same timer, and the opposite thing to tell a reader.
    snaps["reminder"]["timers"][(cwd, "w1")]["armed_ago"] = 900.0
    assert read(snaps=snaps)["reminder"]["state"] == "due"
    idle = _FakeManager({"w1": _FakeSession("w1", cwd, status="idle")})
    assert read(manager=idle, snaps=snaps)["reminder"]["state"] == "held"
    # ...and due_in stays negative rather than clamped: the overshoot is
    # exactly the stretch somebody looking at a held clock wants to see.
    assert read(manager=idle, snaps=snaps)["reminder"]["due_in"] == -300.0

    # Switched off is not the same as not running, and both are said plainly.
    off = read(config={"cflow_reminder": False, "cflow_reminder_interval": 600.0})
    assert (off["reminder"]["state"], off["reminder"]["running"]) == ("off", True)
    # An unreadable config reads as off, never as a guessed interval.
    assert read(config=None)["reminder"]["state"] == "off"

    # The ping is machine-wide and off by default, so it says so beside a
    # reminder that is counting — the strip needs both to pick between them.
    assert read(snaps=snaps)["ping"]["state"] == "off"


def test_a_gate_is_not_the_configuration_being_off(proj):
    """A run parked on a human's approval hears from neither clock, and the
    readout must blame the position rather than the settings — 'off' there
    would send somebody to switch on a thing that is already on."""
    cwd = str(proj)
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    assert payload["status"] == "waiting_approval"
    assert cflow_clock._actionable(payload) is False

    mgr = _FakeManager({"w1": _FakeSession("w1", cwd)})
    out = api_mod._cflow_timers(
        mgr, _snaps(),
        {"cflow_reminder": True, "cflow_reminder_interval": 600.0,
         "cflow_ping": True, "cflow_ping_interval": 900.0},
        cwd, "w1", payload,
    )
    assert out["reminder"]["state"] == "blocked"
    assert out["reminder"]["enabled"] is True
    assert out["ping"]["state"] == "blocked"


def test_the_ping_readout_says_it_is_waiting_on_a_working_session(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    mgr = _FakeManager({"w1": _FakeSession("w1", cwd, status="busy")})
    snaps = _snaps(ping={
        "running": True,
        "timers": {(cwd, "w1"): {"armed_ago": 0.0, "fired_ago": None,
                                 "working": True}},
    })
    out = api_mod._cflow_timers(
        mgr, snaps,
        {"cflow_ping": True, "cflow_ping_interval": 900.0},
        cwd, "w1", payload,
    )
    assert out["ping"]["state"] == "waiting"
    # No session driving it at all (a CLI run) is a different silence again.
    out = api_mod._cflow_timers(
        _FakeManager({}), snaps,
        {"cflow_ping": True, "cflow_ping_interval": 900.0},
        cwd, "w1", payload,
    )
    assert out["ping"]["state"] == "blocked"


# --------------------------------------------------------------------------- #
# the doors the dashboard reads it through
# --------------------------------------------------------------------------- #
def test_the_api_publishes_the_timers_on_both_cflow_doors(proj):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        app = build_app(mgr, "sekrit", started_at=time.monotonic(),
                        mesh=MeshManager(mgr))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get(
                "/api/cflow", params={"cwd": cwd, "scope": "w1"}, headers=BEARER,
            )
            runs = (await resp.json())["runs"]
            assert len(runs) == 1
            timers = runs[0]["timers"]
            # This app was built without the ticks — which is the honest
            # answer, published rather than papered over with an interval.
            assert timers["reminder"]["state"] == "stopped"
            assert timers["reminder"]["running"] is False
            assert timers["ping"]["state"] == "stopped"

            resp = await client.get(
                f"/api/cflow/run?cwd={cwd}&scope=w1", headers=BEARER,
            )
            detail = await resp.json()
            assert detail["timers"]["reminder"]["state"] == "stopped"
            assert detail["timers"]["reminder"]["enabled"] is True
            assert detail["timers"]["reminder"]["interval"] == 600.0

            # A tick that is actually running flips it, and only that does:
            # every other check here reads a clock that was never started, so
            # without this one "stopped" could be a constant.
            reminder = cflow_clock.ReminderClock(mgr)
            reminder.start()
            ping = cflow_clock.StallPingClock(mgr)
            ping.start()
            # The one line daemon/__main__.py adds after starting them: this
            # is what makes the ticks visible to the dashboard at all.
            app["cflow_clocks"] = {"reminder": reminder, "ping": ping}
            try:
                assert reminder.running is True
                resp = await client.get(
                    "/api/cflow", params={"cwd": cwd, "scope": "w1"},
                    headers=BEARER,
                )
                timers = (await resp.json())["runs"][0]["timers"]
                # Running and enabled, but the 15s poll has not come round:
                # armed-to-be, and named as such rather than given a number
                # nothing has measured yet.
                assert timers["reminder"] == {
                    "running": True, "enabled": True, "interval": 600.0,
                    "due_in": None, "fired_ago": None, "state": "arming",
                }
                # The ping is running too and still says off — the two facts
                # are independent, which is the distinction the strip draws.
                assert timers["ping"]["running"] is True
                assert timers["ping"]["state"] == "off"
            finally:
                await reminder.shutdown()
                await ping.shutdown()
        finally:
            await client.close()

    asyncio.run(scenario())
