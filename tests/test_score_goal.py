"""Reward and penalty counts persist independently, ride input sends, and stop nothing."""

import asyncio
import inspect
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from claude_launcher import store
from claude_launcher.daemon import onboard, score_goal, session_reminder
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}

CHILD = (
    "import sys\n"
    "print('READY')\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip())\n"
)


class Session:
    def __init__(self):
        self.sdef = SessionDef(name="worker", score_goal=True)
        self.exited = False
        self.messages = []
        self.state = "busy"
        self.argv = ()

    def status(self):
        return self.state

    async def deliver(self, text):
        if callable(text):
            text = text()
        if not text:
            return False
        self.messages.append(("prompt", text))
        return True


def test_definition_roundtrip_and_old_records():
    old = SessionDef.from_dict({"name": "old"})
    assert not old.score_goal and old.user_reward == 0 and old.user_penalty == 0
    assert "score_goal" not in old.to_dict()
    chosen = replace(old, score_goal=True, user_reward=7, user_penalty=2)
    restored = SessionDef.from_dict(chosen.to_dict())
    assert restored == chosen
    assert score_goal.active(restored)
    # No cutoff: any count still repeats while the selection is recorded.
    assert score_goal.active(replace(restored, user_reward=10))
    assert not score_goal.active(replace(restored, score_goal=False))


def test_pre_split_record_discards_the_single_score():
    legacy = SessionDef.from_dict(
        {"name": "legacy", "score_goal": True, "user_score": 7.5}
    )
    assert legacy.score_goal
    assert legacy.user_reward == 0 and legacy.user_penalty == 0
    assert "user_score" not in legacy.to_dict()


@pytest.mark.parametrize("bad", ["up", "REWARD", 1, True, None, ["reward"]])
def test_invalid_feedback(bad):
    with pytest.raises(ValueError):
        score_goal.feedback(bad)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "7", None])
def test_invalid_counts(bad):
    with pytest.raises(ValueError):
        score_goal.count(bad)


def test_apply_adds_one_point_to_one_count_only():
    sdef = SessionDef(name="worker", score_goal=True)
    assert score_goal.apply(sdef, "none") == {}
    assert score_goal.apply(sdef, "reward") == {"user_reward": 1}
    sdef = replace(sdef, user_reward=3, user_penalty=2)
    assert score_goal.apply(sdef, "penalty") == {"user_penalty": 3}


def test_open_with_delivers_the_block_as_is_for_goal_sessions():
    """No startup slash command: the goal text arrives inside the opening,
    so open_with has nothing to special-case even for an opted-in session."""
    session = Session()

    async def scenario():
        onboard.open_with(session, "opening task")
        await asyncio.gather(
            *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        )

    asyncio.run(scenario())
    assert session.messages == [("prompt", "opening task")]


def test_the_unstamped_command_path_is_gone():
    from claude_launcher.daemon import session as session_mod

    assert not hasattr(session_mod.Session, "deliver_command")
    assert "command" not in inspect.signature(
        session_mod.Session._deliver
    ).parameters


def test_goal_repeats_while_enabled_regardless_of_counts(monkeypatch):
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
        session.sdef = replace(session.sdef, user_reward=6, user_penalty=2)
        await tick(60)
        assert len(session.messages) == 1
        assert "리워드 6점, 패널티 2점" in session.messages[-1][1]
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
        # Ten of anything stops nothing: the goal keeps repeating while enabled.
        session.sdef = replace(session.sdef, user_reward=10)
        await tick(301)
        assert len(session.messages) == 3
        assert "리워드 10점, 패널티 2점" in session.messages[-1][1]
        session.sdef = replace(session.sdef, score_goal=False)
        # Other sources still deliver, with no score goal attached.
        await service._deliver_session("worker", cflow_block="continue workflow")
        assert "continue workflow" in session.messages[-1][1]
        assert "Score goal" not in session.messages[-1][1]
        await tick(400)
        assert len(session.messages) == 4

    asyncio.run(scenario())


def _register_py_harness() -> None:
    store.update(
        lambda doc: doc.update(
            {"harnesses": {"py": {"command": [sys.executable, "-u", "-c", CHILD]}}}
        )
    )


def test_keys_carry_one_feedback_point_when_the_send_lands(home, tmp_path):
    """An input send may carry one reward or penalty point: applied on a landed
    send, never on a duplicate, refused on a session without the feature."""
    _register_py_harness()

    async def run():
        from aiohttp.test_utils import TestClient, TestServer

        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        app = build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            mgr.create(SessionDef(
                name="rated", harness="py", cwd=str(tmp_path), score_goal=True,
            ))
            mgr.create(SessionDef(name="plain", harness="py", cwd=str(tmp_path)))
            rated = mgr.get("rated")
            await rated.wait_for("idle", timeout=10.0, threshold=0.5)

            async def post(name, body):
                resp = await client.post(
                    f"/api/sessions/{name}/keys", json=body, headers=BEARER,
                )
                return resp, await resp.json()

            resp, doc = await post("rated", {
                "keys": ["well done", "Enter"], "force": True,
                "input_id": "fb-1", "feedback": "reward",
            })
            assert resp.status == 200, doc
            assert doc["score_goal"] == {
                "enabled": True, "reward": 1, "penalty": 0, "active": True,
            }
            assert rated.sdef.user_reward == 1 and rated.sdef.user_penalty == 0

            # The same input_id landing twice is one send and one point.
            resp, doc = await post("rated", {
                "keys": ["well done", "Enter"], "force": True,
                "input_id": "fb-1", "feedback": "reward",
            })
            assert resp.status == 200 and doc["duplicate"] is True
            assert rated.sdef.user_reward == 1

            resp, doc = await post("rated", {
                "paste": "line one\nline two", "enter": True, "force": True,
                "feedback": "penalty",
            })
            assert resp.status == 200, doc
            assert doc["score_goal"]["penalty"] == 1
            assert rated.sdef.user_reward == 1 and rated.sdef.user_penalty == 1

            # No feedback key is a plain send: the counts do not move.
            resp, doc = await post("rated", {"keys": ["plain", "Enter"], "force": True})
            assert resp.status == 200, doc
            assert rated.sdef.user_reward == 1 and rated.sdef.user_penalty == 1

            resp, _ = await post("rated", {"keys": ["x", "Enter"], "feedback": "up"})
            assert resp.status == 400
            resp, _ = await post("plain", {"keys": ["x", "Enter"], "feedback": "reward"})
            assert resp.status == 409
            # A refused send delivered nothing, so it granted nothing either.
            assert mgr.get("plain").sdef.user_reward == 0

            # The counts persist with the session definition.
            saved = next(
                row["def"] for row in mgr._store.load_all()
                if row["def"]["name"] == "rated"
            )
            restored = SessionDef.from_dict(saved)
            assert restored.user_reward == 1 and restored.user_penalty == 1

            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())
