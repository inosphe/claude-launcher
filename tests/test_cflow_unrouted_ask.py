"""The silence a run dies in: an ask that was never put to anyone.

``goto`` moves a run without delivering the step, and opening a delegated ask
is a write that only ``next`` performs — so a run forced onto such a step
reports ``waiting_answer`` while no responder holds the question. That state
used to read as "somebody else is on it" to everything that looks at it: the
reminder clock skipped it, the overseer heard nothing, and the run sat
forever. These tests pin both halves of the repair — the driver is poked, and
a genuinely delegated ask stays as quiet as it always was.
"""

from __future__ import annotations

import pytest

from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

# The ask is on the second step, so the run has somewhere to be forced FROM.
DELEGATED = """
name: shipit
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    ask:
      prompt: the diff is green -- approve the push?
      from: [{role: leader}]
    instructions: push it
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    (d / ".claunch" / "workflows").mkdir(parents=True)
    (d / ".claunch" / "workflows" / "shipit.yaml").write_text(
        DELEGATED, encoding="utf-8"
    )
    return d


class _FakeSession:
    def __init__(self, name: str, cwd: str, parent=None) -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd, parent=parent)
        self.delivered: list = []
        self.status_value = "busy"

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

    def list(self):
        return list(self._sessions.values())


def _forced_onto_the_ask(cwd: str) -> dict:
    """A run parked exactly where the accident put it: goto'd onto an ask
    step, so the question exists in the workflow but was never opened."""
    cflow_engine.start("shipit", cwd=cwd, scope="w1")
    cflow_engine.goto("ship", cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    # the state under test, spelled out: delegated-looking, held by nobody
    assert payload["status"] == "waiting_answer"
    assert not (payload.get("ask") or {}).get("asked")
    return payload


# --------------------------------------------------------------------------- #
# the discriminator
# --------------------------------------------------------------------------- #
def test_an_unopened_ask_is_told_apart_from_a_delegated_one(proj):
    cwd = str(proj)
    payload = _forced_onto_the_ask(cwd)
    assert cflow_clock._ask_reached_nobody(payload) is True
    assert cflow_clock._actionable(payload) is True

    # ...and once the driver opens it, the question is somebody's: the same
    # status now means the opposite, and must go back to being silent.
    opened = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert opened["status"] in ("waiting_answer", "waiting_approval")
    if opened["status"] == "waiting_answer":
        assert cflow_clock._ask_reached_nobody(opened) is False
        assert cflow_clock._actionable(opened) is False


def test_plain_positions_are_unchanged(proj):
    """The predicate widens the clock's reach; it must not narrow it."""
    cwd = str(proj)
    cflow_engine.start("shipit", cwd=cwd, scope="w1")
    assert cflow_clock._actionable(cflow_engine.status(cwd, scope="w1")) is True
    for status in ("waiting_approval", "waiting_selection", "idle", "done"):
        assert cflow_clock._actionable({"status": status}) is False


# --------------------------------------------------------------------------- #
# the reminder clock
# --------------------------------------------------------------------------- #
def test_the_driver_is_reminded_and_told_to_call_next(proj):
    cwd = str(proj)
    _forced_onto_the_ask(cwd)
    clock = cflow_clock.ReminderClock(_FakeManager({}))
    t = 1000.0
    assert clock.scan(t) == []  # first sight arms, as everywhere else
    due = clock.scan(t + 601)
    assert [(c, s) for c, s, _, _ in due] == [(cwd, "w1")]
    block = due[0][2]
    # the block must not pretend to hand over a step whose content is
    # withheld -- it names the real position and the one call that moves it
    assert "entry approval not yet opened" in block
    assert "never actually put to anyone" in block
    assert "Call 'next'" in block
    assert "approve the push" in block  # the question itself, for context
    assert "file it with 'report'" not in block


def _delegated(cwd: str) -> dict:
    """What ``status`` serves once the ask has actually reached a responder.

    Built rather than provoked: routing one for real needs a live mesh with a
    matching role, and the clock's decision is made on this shape alone.
    """
    return {
        **cflow_engine.status(cwd, scope="w1"),
        "status": "waiting_answer",
        "reason": "approval",
        "ask": {
            "kind": "approval",
            "asked": [{"kind": "member", "handle": "boss"}],
            "skipped": [],
        },
    }


def test_a_genuinely_delegated_ask_stays_silent(proj, monkeypatch):
    """The noise this repair must not create: a question that reached a
    responder is theirs, and poking the driver about it is nagging them for
    somebody else's answer."""
    cwd = str(proj)
    _forced_onto_the_ask(cwd)
    delegated = _delegated(cwd)
    assert cflow_clock._ask_reached_nobody(delegated) is False
    monkeypatch.setattr(
        cflow_clock.cflow_engine, "status", lambda *a, **k: delegated
    )

    clock = cflow_clock.ReminderClock(_FakeManager({}))
    assert clock.scan(1000.0) == []
    assert clock.scan(1000.0 + 601) == []  # the interval passes, still silent

    # and the overseer is not told either: nothing is stuck, somebody is
    # simply thinking
    boss = _FakeSession("boss", cwd)
    worker = _FakeSession("w1", cwd, parent="boss")
    events = cflow_clock.RunEventClock(
        _FakeManager({"boss": boss, "w1": worker})
    )
    assert events.scan() == []
    assert events.scan() == []


# --------------------------------------------------------------------------- #
# the overseer's event
# --------------------------------------------------------------------------- #
def test_the_overseer_hears_about_a_gate_nobody_holds(proj):
    """The reminder only reaches a *busy* session, so an idle or dead driver
    leaves this run silent — the event is the other way out."""
    cwd = str(proj)
    cflow_engine.start("shipit", cwd=cwd, scope="w1")
    boss = _FakeSession("boss", cwd)
    worker = _FakeSession("w1", cwd, parent="boss")
    clock = cflow_clock.RunEventClock(_FakeManager({"boss": boss, "w1": worker}))
    assert clock.scan() == []  # first sight arms

    cflow_engine.goto("ship", cwd=cwd, scope="w1")
    events = clock.scan()
    assert [e["kind"] for e in events] == ["human-gate"]
    block = events[0]["block"]
    assert "never put to anyone" in block
    assert "approve the push" in block
    # it is an approval, not a selection -- the old wording called every
    # non-waiting_approval gate a "selection"
    assert "selection" not in block
    assert "calling 'next'" in block  # the driver's route, named for the boss

    # transition, not state: sitting there does not repeat the event
    assert clock.scan() == []
