"""The cflow stall ping: the clock, its guardrail exemption, and the API door.

The gap this closes is narrow and worth restating, because every other clock
deliberately declines it. A run sits at a step (or a select) that is its own
agent's to move — nothing delegated, no gate, no selection outstanding — and
the session driving it has stopped working. The reminder clock will not type
there (it steers a *busy* agent, by design); the run event clock has no event
to report (no gate entered, no round finished, the session has not exited).
So the contract here is: *stopped, at an unchanged actionable position, for
the configured interval* — and never when a guardrail is what stopped it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock
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

GATED = """
name: gated
steps:
  ship:
    gate: human review
    instructions: ship it
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project with the two workflows declared, ping switched on.

    The switch is flipped here because OFF is the shipped default (a run can
    be idle at an actionable step legitimately), and every test below is
    about what the clock does once an operator has turned it on. The one test
    that cares about the default reads it before this fixture's write.
    """
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    (d / ".claunch" / "workflows" / "gated.yaml").write_text(
        GATED, encoding="utf-8"
    )
    store.set_daemon_field("cflow_ping", True)
    return d


class _FakeSession:
    def __init__(self, name: str, cwd: str, status: str = "idle") -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        #: idle = the stall this clock exists for; busy = the reminder's.
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


def _clock(proj, sessions=None, status="idle"):
    if sessions is None:
        sessions = {"w1": _FakeSession("w1", str(proj), status)}
    return cflow_clock.StallPingClock(_FakeManager(sessions)), sessions


# --------------------------------------------------------------------------- #
# the switch
# --------------------------------------------------------------------------- #
def test_off_by_default_and_enabling_does_not_fire_a_backlog(home, tmp_path,
                                                             monkeypatch):
    """Shipped off, and turning it on starts every run's timer at zero.

    Both halves matter. Off by default because an idle-at-an-actionable-step
    run may be perfectly healthy — a workflow whose intake parks until a
    human hands it a goal is exactly that — and the daemon cannot tell those
    apart. And no backlog on enable, because keeping timers while switched
    off would ping the whole fleet at once the moment somebody ticks the box.
    """
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "linear.yaml").write_text(
        LINEAR, encoding="utf-8"
    )
    assert store.daemon_config()["cflow_ping"] is False
    cwd = str(d)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock, _ = _clock(d)
    assert clock.scan(1000.0) == []
    assert clock.scan(99999.0) == []               # off: never due
    store.set_daemon_field("cflow_ping", True)
    # the long stall accrued while off is not owed: this pass only arms
    assert clock.scan(100000.0) == []
    assert clock.scan(100000.0 + 901) != []


def test_zero_interval_is_off_too(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 0)
    clock, _ = _clock(proj)
    clock.scan(1000.0)
    assert clock.scan(99999.0) == []


def test_the_configured_interval_has_a_floor(proj):
    """A ping opens a fresh turn; a few seconds apart is not a nudge."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 5)
    clock, _ = _clock(proj)
    clock.scan(1000.0)
    assert clock.scan(1000.0 + 30) == []           # not 5s — the floor holds
    assert clock.scan(1000.0 + cflow_clock.PING_MIN_INTERVAL + 1) != []


# --------------------------------------------------------------------------- #
# the trigger: stopped, unchanged, actionable
# --------------------------------------------------------------------------- #
def test_a_stopped_session_is_pinged_after_the_interval(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 600)
    store.set_daemon_field("cflow_ping_message", "still there?")
    clock, _ = _clock(proj)
    t = 1000.0
    assert clock.scan(t) == []                     # first sight arms only
    assert clock.scan(t + 599) == []
    due = clock.scan(t + 601)
    assert [(c, s) for c, s, _ in due] == [(cwd, "w1")]
    block = due[0][2]
    assert "still there?" in block                 # the operator's own words
    assert "stall ping" in block
    assert "step 'one'" in block
    assert "machine-generated" in block            # not mistakable for a user


def test_progress_rearms_instead_of_firing(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 600)
    clock, _ = _clock(proj)
    t = 1000.0
    clock.scan(t)
    assert clock.scan(t + 601)                     # stalled at 'one'
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert clock.scan(t + 700) == []               # moved: re-armed
    due = clock.scan(t + 700 + 601)
    assert "step 'two'" in due[0][2]


def test_a_working_session_is_never_pinged(proj):
    """Busy is the reminder clock's audience, not this one's — and a stretch
    of work resets the stall, so the interval measures unbroken silence."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 600)
    clock, sessions = _clock(proj, status="busy")
    t = 1000.0
    clock.scan(t)
    assert clock.scan(t + 99999) == []             # working: never due
    sessions["w1"].status_value = "idle"           # the turn ends here
    assert clock.scan(t + 99999) == []             # ...which arms, not fires
    assert clock.scan(t + 99999 + 601) != []
    # and a burst of work in the middle of a stall resets it again
    sessions["w1"].status_value = "busy"
    clock.scan(t + 99999 + 700)
    sessions["w1"].status_value = "idle"
    clock.scan(t + 99999 + 800)
    assert clock.scan(t + 99999 + 900) == []


def test_starting_is_not_stopped(proj):
    """A session whose harness has not printed yet has not 'stopped'."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock, _ = _clock(proj, status="starting")
    clock.scan(1000.0)
    assert clock.scan(99999.0) == []


# --------------------------------------------------------------------------- #
# the guardrail exemption
# --------------------------------------------------------------------------- #
def test_a_run_a_guardrail_is_holding_is_not_a_stall(proj):
    """Parked on a human's approval is the protocol working, not a stall —
    and it is the run event clock that tells an overseer about it."""
    cwd = str(proj)
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "waiting_approval"
    clock, sessions = _clock(proj)
    clock.scan(1000.0)
    assert clock.scan(99999.0) == []
    assert sessions["w1"].delivered == []


def test_a_delegated_answer_nobody_holds_is_still_the_agents(proj):
    """The one 'waiting_*' that IS the driver's to move: an ask forced into
    place by goto, which reached nobody. cflow already treats it as the
    agent's (engine.approve, the reminder clock), so the ping does too."""
    payload = {
        "status": "waiting_answer", "workflow": "linear",
        "step_id": "ship", "visit": 1, "ask": {"asked": []},
    }
    assert cflow_clock._actionable(payload)
    block = cflow_clock.ping_block(payload, "hello", 600)
    assert "nobody was ever asked" in block


# --------------------------------------------------------------------------- #
# who gets typed into
# --------------------------------------------------------------------------- #
def test_no_driving_session_means_no_ping(proj):
    """A CLI run (no session of this name) and an exited driver both drop
    out — the latter is 'orphaned', which the run event clock reports."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 600)
    clock, _ = _clock(proj, sessions={})           # manager knows nobody
    clock.scan(1000.0)
    assert clock.scan(99999.0) == []

    gone = _FakeSession("w1", cwd)
    gone.exited = True
    clock2, _ = _clock(proj, sessions={"w1": gone})
    clock2.scan(1000.0)
    assert clock2.scan(99999.0) == []

    # and the same name in another directory is somebody else's session
    elsewhere = _FakeSession("w1", str(proj.parent))
    clock3, _ = _clock(proj, sessions={"w1": elsewhere})
    clock3.scan(1000.0)
    assert clock3.scan(99999.0) == []


def test_delivery_types_the_block_and_rearms(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    store.set_daemon_field("cflow_ping_interval", 600)
    clock, sessions = _clock(proj)
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 601)
    assert due
    asyncio.run(clock._deliver(*due[0]))
    assert len(sessions["w1"].delivered) == 1
    # a successful delivery re-arms against the real clock _deliver reads
    assert clock.scan(time.monotonic() + 100) == []


# --------------------------------------------------------------------------- #
# the block
# --------------------------------------------------------------------------- #
def test_the_block_frames_the_message_rather_than_being_it():
    """A ping lands in a session whose turn was over, so it reads as a user
    message unless the frame says otherwise — and the reader needs to be
    told the two facts that make it actionable."""
    block = cflow_clock.ping_block(
        {"status": "step", "workflow": "linear", "step_id": "impl", "visit": 2},
        "wake up",
        1800,
    )
    assert "machine-generated, not typed by the user" in block
    assert "~30 min" in block
    assert "message: wake up" in block
    assert "step 'impl' (visit 2)" in block
    assert "no approval, no selection, no delegated answer" in block
    assert "say what you are waiting for and stay put" in block


def test_the_block_survives_an_empty_message():
    block = cflow_clock.ping_block(
        {"status": "select", "workflow": "linear", "step_id": "triage"}, "", 90
    )
    assert "message:" not in block
    assert "branch choice at step 'triage'" in block


# --------------------------------------------------------------------------- #
# the API door
# --------------------------------------------------------------------------- #
def test_the_api_edits_the_ping_settings(proj):
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    packaged = store.DAEMON_DEFAULTS["cflow_ping_message"]

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        mm = MeshManager(mgr)
        app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=mm)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/api/cflow/ping", headers=BEARER)
            defs = (await resp.json())["defaults"]
            assert defs["enabled"] is True          # the fixture turned it on
            assert defs["interval"] == 900.0
            assert defs["message"] == packaged
            assert defs["min_interval"] == cflow_clock.PING_MIN_INTERVAL

            resp = await client.put(
                "/api/cflow/ping", headers=BEARER,
                json={"enabled": False, "interval": 300, "message": "oi"},
            )
            defs = (await resp.json())["defaults"]
            assert defs == {
                "enabled": False, "interval": 300.0, "message": "oi",
                "min_interval": cflow_clock.PING_MIN_INTERVAL,
            }
            # persisted where the clock (and the CLI) read it, live
            assert store.daemon_config()["cflow_ping"] is False
            assert store.daemon_config()["cflow_ping_message"] == "oi"

            # partial: an interval-only PUT leaves the message alone
            resp = await client.put(
                "/api/cflow/ping", headers=BEARER, json={"interval": 600},
            )
            assert (await resp.json())["defaults"]["message"] == "oi"

            # blank restores the packaged text rather than pinging wordlessly
            resp = await client.put(
                "/api/cflow/ping", headers=BEARER, json={"message": "   "},
            )
            assert (await resp.json())["defaults"]["message"] == packaged

            resp = await client.put(
                "/api/cflow/ping", headers=BEARER, json={"interval": 5},
            )
            assert resp.status == 400
            resp = await client.put(
                "/api/cflow/ping", headers=BEARER, json={"message": 7},
            )
            assert resp.status == 400
        finally:
            await client.close()

    asyncio.run(scenario())
