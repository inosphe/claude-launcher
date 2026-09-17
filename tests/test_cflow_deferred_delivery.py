"""Cflow wake-ups survive draft holds after the workflow has already moved."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from claude_launcher.cflow import engine, responders
from claude_launcher.daemon import api, cflow_clock, session as session_mod
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.screen import ScreenState


class Terminal(session_mod.Session):
    """Real input/delivery code with a recording PTY, no child process."""

    def __init__(self, cwd, name="w1"):
        self.sdef = SessionDef(name=name, cwd=str(cwd))
        self.exited = False
        self._input_ready = True
        self._last_human_input = self._last_terminal_input = 0.0
        self._draft_open = False
        self._delivery_lock = asyncio.Lock()
        self._deferred_deliveries = set()
        self._deferred_delivery_lock = asyncio.Lock()
        self.writes = []
        self.test_screen = ScreenState(80, 24)

    @property
    def screen(self):
        return self.test_screen

    def status(self, threshold=None):
        return "idle"

    async def write_bytes(self, data):
        self.writes.append(data)


@pytest.fixture(autouse=True)
def fast_keyboard(monkeypatch):
    monkeypatch.setattr(session_mod, "TYPING_HOLD_TIMEOUT", 0.01)
    monkeypatch.setattr(session_mod, "TYPING_GUARD", 0)
    monkeypatch.setattr(session_mod, "PASTE_ENTER_DELAY", 0)
    monkeypatch.setattr(session_mod, "delivery_stamp", lambda: "[T]")


def manager(session):
    return SimpleNamespace(list=lambda: [session], get=lambda name: session)


async def drain(session):
    await asyncio.wait_for(asyncio.gather(*session._deferred_deliveries), 2)
    await asyncio.sleep(0)
    assert not session._deferred_deliveries


def test_nudge_returns_promptly_and_survives_draft_timeout(tmp_path):
    async def run():
        s = Terminal(tmp_path)
        s.note_human_input(at_terminal=True, data=b"draft")
        assert await api._nudge_sessions(manager(s), str(tmp_path), "w1", "resume") == ["w1"]
        await asyncio.sleep(0.04)  # past the normal delivery refusal
        assert not s.writes and s._deferred_deliveries
        other = Terminal(tmp_path, "w2")
        assert other.queue_delivery("other session")
        await drain(other)
        assert other.writes  # no cross-session head-of-line blocking
        s.note_human_input(at_terminal=True, data=b"\r")
        await drain(s)
        assert s.writes == [b"[T]\rresume", b"\r"]

    asyncio.run(run())


def test_checklist_wake_up_survives_after_state_transition(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    flows = tmp_path / ".claunch" / "workflows"
    flows.mkdir(parents=True)
    (flows / "probe.yaml").write_text(
        "name: probe\nsteps:\n  gate:\n    instructions: wait\n"
        "    checklist:\n      then: work\n      items:\n"
        "        - id: ready\n          describe: ready\n"
        "          check: 'python -c pass'\n"
        "  work:\n    instructions: continue\n", encoding="utf-8",
    )
    cwd = str(tmp_path)
    engine.start("probe", cwd=cwd, scope="w1")
    engine.report("ready", cwd=cwd, scope="w1")

    async def run():
        s = Terminal(tmp_path)
        s.note_human_input(at_terminal=True, data=b"draft")
        clock = cflow_clock.ChecklistClock(manager(s))
        notices = clock.scan(now=1000)
        assert len(notices) == 1
        assert engine.status(cwd, scope="w1")["step_id"] == "work"
        await clock._deliver(*notices[0])
        await asyncio.sleep(0.04)
        assert not s.writes
        assert clock.scan(now=2000) == []
        s.note_human_input(at_terminal=True, data=b"\r")
        await drain(s)
        assert len(s.writes) == 2
        assert b"checklist passed" in s.writes[0]

    asyncio.run(run())


@pytest.mark.parametrize("clock_type", [
    cflow_clock.WindowClock, cflow_clock.TimerClock,
    cflow_clock.RoundStartClock, cflow_clock.RestartClock,
])
def test_other_transition_clocks_queue_behind_drafts(tmp_path, clock_type):
    async def run():
        s = Terminal(tmp_path)
        s.note_human_input(at_terminal=True, data=b"draft")
        kwargs = {"boot_id": "test"} if clock_type is cflow_clock.RestartClock else {}
        await clock_type(manager(s), **kwargs)._deliver(str(tmp_path), "w1", "moved")
        await asyncio.sleep(0.04)
        assert not s.writes and s._deferred_deliveries
        s.note_human_input(at_terminal=True, data=b"\r")
        await drain(s)
        assert s.writes == [b"[T]\rmoved", b"\r"]

    asyncio.run(run())


def test_queued_delivery_does_not_retry_a_partial_write(tmp_path):
    async def run():
        s = Terminal(tmp_path)
        calls = []

        async def fail(data):
            calls.append(data)
            raise OSError("partial PTY write")

        s.write_bytes = fail
        assert s.queue_delivery("once")
        await drain(s)
        assert len(calls) == 1

    asyncio.run(run())


def test_shutdown_cancels_draft_wait_without_writing(tmp_path):
    async def run():
        s = Terminal(tmp_path)
        s.pty = None
        s.note_human_input(at_terminal=True, data=b"draft")
        assert s.queue_delivery("pending")
        await asyncio.sleep(0.04)
        pending = tuple(s._deferred_deliveries)
        await s.shutdown()
        assert all(task.cancelled() for task in pending)
        assert not s._deferred_deliveries and not s.writes

    asyncio.run(run())


def test_queued_messages_keep_order_and_stop_on_exit(tmp_path):
    async def run():
        s = Terminal(tmp_path)
        s.note_human_input(at_terminal=True, data=b"draft")
        assert s.queue_delivery("first") and s.queue_delivery("second")
        await asyncio.sleep(0.04)
        assert not s.writes
        s.note_human_input(at_terminal=True, data=b"\r")
        await drain(s)
        assert s.writes == [b"[T]\rfirst", b"\r", b"[T]\rsecond", b"\r"]
        s.writes.clear()
        s.note_human_input(at_terminal=True, data=b"draft")
        assert s.queue_delivery("never")
        await asyncio.sleep(0.04)
        s.exited = True
        await drain(s)
        assert not s.writes
        assert not s.queue_delivery("after exit")

    asyncio.run(run())


def test_deferred_http_delivery_reports_acceptance_before_draft_release(tmp_path):
    async def run():
        s = Terminal(tmp_path)
        s.note_human_input(at_terminal=True, data=b"draft")

        async def body():
            return {"text": "from CLI", "defer": True}

        request = SimpleNamespace(app={"manager": manager(s)}, match_info={"name": "w1"}, json=body)
        response = await api.h_session_deliver(request)
        assert json.loads(response.text) == {"ok": True, "queued": True, "delivered": False}
        assert not s.writes
        s.note_human_input(at_terminal=True, data=b"\r")
        await drain(s)
        assert s.writes == [b"[T]\rfrom CLI", b"\r"]

    asyncio.run(run())


@pytest.mark.parametrize("result, accepted", [
    ({"ok": True, "delivered": False}, []),
    ({"ok": True, "delivered": False, "queued": True}, ["w1"]),
    ({"ok": True, "delivered": True}, ["w1"]),
])
def test_cli_reports_only_accepted_or_delivered_nudges(tmp_path, monkeypatch, result, accepted):
    class Client:
        def get(self, *args, **kwargs):
            return {"sessions": [{"name": "w1", "cwd": str(tmp_path), "status": "idle"}]}

        def post(self, path, body, **kwargs):
            assert body == {"text": "resume", "defer": True}
            return result

    monkeypatch.setattr(responders.daemon_client, "connect", lambda: Client())
    assert responders.nudge("w1", "resume", cwd=str(tmp_path)) == accepted
