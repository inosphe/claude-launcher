"""The cflow reminder: engine override, the daemon clock, and the API doors.

The clock's contract is *no progress, then repeat*: a run sitting on the same
agent-actionable position for its interval gets that position's instructions
re-typed into its session, and a run that moves hears nothing. Configuration
is layered — machine defaults in the config file (read live), one run's
override in its own state — and both layers are exercised here.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.cflow.engine import CflowError
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


class _FakeTracker:
    """last_meaningful_change() the tests can move by hand."""

    def __init__(self) -> None:
        self.last = time.monotonic()

    def last_meaningful_change(self):
        return self.last


class _FakeSession:
    def __init__(self, name: str, cwd: str) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd)
        self.delivered: list = []
        self.tracker = _FakeTracker()

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


# --------------------------------------------------------------------------- #
# the per-run override (engine)
# --------------------------------------------------------------------------- #
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
def test_no_progress_then_repeat(proj):
    """First sight arms; the interval elapsing fires; progress re-arms."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    t = 1000.0
    assert clock.scan(t) == []                      # armed, not fired
    assert clock.scan(t + 179) == []                # default 180 not yet up
    due = clock.scan(t + 181)
    assert [(c, s) for c, s, _ in due] == [(cwd, "w1")]
    assert "do one" in due[0][2]                    # the step's instructions
    assert "step 'one'" in due[0][2]
    # the run moves: the new position re-arms instead of firing
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert clock.scan(t + 400) == []
    due = clock.scan(t + 400 + 181)
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
    due = clock.scan(t + 181)
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


def test_a_suspended_session_holds_one_reminder(proj):
    """No activity since the last reminder means nobody is reading: the next
    one is held — not dropped — and goes out the moment the screen moves."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    sess = _FakeSession("w1", cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    t = time.monotonic()
    clock.scan(t)
    due = clock.scan(t + 181)
    asyncio.run(clock._deliver(*due[0]))
    assert len(sess.delivered) == 1
    delivered_at = clock._seen[(cwd, "w1")]["delivered_at"]

    # the only screen change since is the paste's own echo (inside the grace)
    sess.tracker.last = delivered_at + 1.0
    due = clock.scan(time.monotonic() + 400)
    assert due                                   # the debt stays due...
    asyncio.run(clock._deliver(*due[0]))
    assert len(sess.delivered) == 1              # ...but nothing is typed

    # the session shows life -> the held reminder goes out at once
    sess.tracker.last = delivered_at + 60.0
    asyncio.run(clock._deliver(*due[0]))
    assert len(sess.delivered) == 2


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
            assert defs == {"enabled": True, "interval": 180.0}

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
