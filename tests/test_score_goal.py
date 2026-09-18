"""User ratings persist and only the selected goal stops at ten."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from claude_launcher.daemon import onboard, score_goal, session_reminder
from claude_launcher.daemon.harness import SessionDef


class Session:
    def __init__(self):
        self.sdef = SessionDef(name="worker", score_goal=True)
        self.exited = False
        self.messages = []
        self.state = "busy"

    def status(self):
        return self.state

    async def deliver(self, text):
        if callable(text):
            text = text()
        if not text:
            return False
        self.messages.append(("prompt", text))
        return True

    async def deliver_command(self, text):
        self.messages.append(("command", text))
        return True


def test_definition_roundtrip_and_old_records():
    old = SessionDef.from_dict({"name": "old"})
    assert not old.score_goal and old.user_score == 0
    assert "score_goal" not in old.to_dict()
    chosen = replace(old, score_goal=True, user_score=7.5)
    restored = SessionDef.from_dict(chosen.to_dict())
    assert restored == chosen
    assert score_goal.active(restored)
    assert not score_goal.active(replace(restored, user_score=10))


@pytest.mark.parametrize("bad", [-1, 10.1, float("inf"), float("nan"), True, "7", None])
def test_invalid_scores(bad):
    with pytest.raises(ValueError):
        score_goal.score(bad)


def test_first_input_is_command_then_opening():
    session = Session()
    asyncio.run(onboard._open_with_score_goal(session, "opening task"))
    assert session.messages == [
        ("command", "/goal " + score_goal.prompt(0)),
        ("prompt", "opening task"),
    ]


def test_goal_without_workflow_pause_idle_completion_and_latest_score(monkeypatch):
    session = Session()
    manager = SimpleNamespace(list=lambda: [session], get=lambda name: session)
    service = session_reminder.SessionReminderService(manager)
    monkeypatch.setattr(service.cflow, "scan", lambda now: [])
    monkeypatch.setattr(service, "_config", lambda: {"cflow_reminder_interval": 60})
    monkeypatch.setattr(session_reminder, "context_id_lines", lambda *a: [])
    monkeypatch.setattr(session_reminder.cflow_engine, "open_asks", lambda *a: [])
    clock = [0]
    monkeypatch.setattr(session_reminder.time, "monotonic", lambda: clock[0])

    async def tick(now):
        clock[0] = now
        await service.tick(now)

    async def scenario():
        await tick(0)
        session.sdef = replace(session.sdef, user_score=6.5)
        await tick(60)
        assert len(session.messages) == 1
        assert "6.5/10" in session.messages[-1][1]
        assert "/goal" not in session.messages[-1][1]
        session.sdef = replace(session.sdef, reminder_paused=True)
        await tick(120)
        assert len(session.messages) == 1
        service._rearm_session("worker", 120)
        session.sdef = replace(session.sdef, reminder_paused=False)
        session.state = "idle"
        await tick(180)
        assert len(session.messages) == 1
        session.state = "busy"
        await tick(181)  # Resuming starts a fresh interval.
        await tick(241)
        assert len(session.messages) == 2
        session.sdef = replace(session.sdef, user_score=10)
        await tick(301)
        assert len(session.messages) == 2
        # Other sources still deliver, with no completed score goal attached.
        await service._deliver_session("worker", cflow_block="continue workflow")
        assert "continue workflow" in session.messages[-1][1]
        assert "Score goal" not in session.messages[-1][1]
        session.sdef = replace(session.sdef, score_goal=False, user_score=0)
        await tick(400)
        assert len(session.messages) == 3

    asyncio.run(scenario())
