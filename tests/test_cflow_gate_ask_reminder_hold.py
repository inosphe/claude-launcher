"""A gate-shaped ask is a person's gate before 'next' opens it, too.

An ``ask:`` with no ``from`` is the form the deprecated ``gate:`` converts
into (:func:`cflow.model._parse_delegate`): nobody is asked, ``otherwise``
decides at once, and its default is ``human``. Opening even that ask is a
write only ``next`` performs, so a run forced onto such a step with ``goto``
-- or confirmed onto it from the CLI or the dashboard -- reported
``waiting_answer`` with no ask behind it until the driver called ``next``.
The reminder clock reads that shape as "a delegated decision that reached
nobody" (the repair pinned in ``test_cflow_unrouted_ask.py``) and types
"call 'next'" at a busy driver every interval; a driver that answers by
reading ``status`` and waiting for the approval is nagged indefinitely,
over a position whose only exit was a person's approval all along
(issue ``claunch-ueku``: the improv-worker ``end-gate`` is exactly this
shape).

These tests pin the position as the human gate it is -- every clock stays
out of it, the overseer hears about it, a person can approve it -- and keep
the two shapes that DO need the driver reading exactly as before: a
candidate list to route, and ``otherwise: self``.
"""

from __future__ import annotations

import pytest

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef

# The gate is on the second step, so the run has somewhere to be forced FROM.
GATE_ASK = """
name: gateask
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    ask:
      prompt: the diff is green -- approve the push?
    instructions: push it
"""

# The same gate reached the other way a run lands on it without 'next': a
# person confirming the driver's proposal from the CLI or the dashboard.
VIA_SELECT = """
name: viaselect
steps:
  pick:
    select:
      prompt: land it?
      chooser: user
      options:
        ship:
          description: push
          next: ship
        drop:
          description: abandon
          next: done
  ship:
    ask:
      prompt: approve the push?
    instructions: push it
  done:
    instructions: nothing to do
"""

# A delegated select with nobody to delegate to: the branch is the user's.
GATE_SELECT = """
name: gateselect
steps:
  impl:
    instructions: implement the thing
    next: route
  route:
    select:
      prompt: which way?
      chooser:
        otherwise: human
      options:
        left:
          description: go left
          next: after
        right:
          description: go right
          next: after
  after:
    instructions: carry on
"""

# The two shapes that still need the driver's 'next', kept apart on purpose.
SELF_ASK = """
name: selfask
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    ask:
      prompt: approve the push?
      otherwise: self
    instructions: push it
"""

ROUTED_ASK = """
name: routedask
steps:
  impl:
    instructions: implement the thing
    next: ship
  ship:
    ask:
      prompt: approve the push?
      from: [{role: leader}]
    instructions: push it
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    for text in (GATE_ASK, VIA_SELECT, GATE_SELECT, SELF_ASK, ROUTED_ASK):
        name = text.split("name:", 1)[1].split()[0]
        (wf / f"{name}.yaml").write_text(text, encoding="utf-8")
    return d


class _FakeSession:
    def __init__(self, name: str, cwd: str, parent=None, status="busy") -> None:
        self.exited = False
        self.sdef = SessionDef(name=name, cwd=cwd, parent=parent)
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

    def list(self):
        return list(self._sessions.values())


def _forced_onto(cwd: str, workflow: str, step: str) -> dict:
    """A run parked where an accident put it: goto'd onto the step, so the
    question exists in the workflow but 'next' has not opened it."""
    cflow_engine.start(workflow, cwd=cwd, scope="w1")
    cflow_engine.goto(step, cwd=cwd, scope="w1")
    return cflow_engine.status(cwd, scope="w1")


# --------------------------------------------------------------------------- #
# the position, as status reports it
# --------------------------------------------------------------------------- #
def test_a_gate_shaped_ask_is_a_human_gate_before_next_opens_it(proj):
    cwd = str(proj)
    payload = _forced_onto(cwd, "gateask", "ship")
    # The one thing the old shape got wrong: this is not a question nobody
    # was asked, it is the approval a person has always answered.
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "ask"
    assert payload["gate"] == "the diff is green -- approve the push?"
    assert "claunch cflow approve" in payload["how_to_unblock"]
    assert cflow_clock._ask_reached_nobody(payload) is False
    assert cflow_clock._actionable(payload) is False
    # Read-only, and said so: nothing was opened by looking.
    assert "not been opened for the record yet" in payload["note"]

    # 'next' opens the ask for the record, and the position does not change
    # shape by it -- the same gate, in front of the same person.
    opened = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert opened["status"] == "waiting_approval"
    assert opened["reason"] == "ask"
    assert cflow_clock._actionable(opened) is False


def test_a_cli_confirmed_select_lands_on_the_same_gate(proj):
    cwd = str(proj)
    cflow_engine.start("viaselect", cwd=cwd, scope="w1")
    cflow_engine.select("ship", by="user", cwd=cwd, scope="w1")
    payload = cflow_engine.status(cwd, scope="w1")
    assert payload["step_id"] == "ship"
    assert payload["status"] == "waiting_approval"
    assert payload["reason"] == "ask"
    assert cflow_clock._actionable(payload) is False


def test_a_gate_shaped_select_chooser_is_the_users_selection(proj):
    cwd = str(proj)
    payload = _forced_onto(cwd, "gateselect", "route")
    assert payload["status"] == "waiting_selection"
    assert payload["prompt"] == "which way?"
    assert [o["name"] for o in payload["options"]] == ["left", "right"]
    assert "claunch cflow select" in payload["how_to_unblock"]
    assert cflow_clock._actionable(payload) is False
    # ...and the person's answer lands on it as on any selection.
    moved = cflow_engine.select("left", by="user", cwd=cwd, scope="w1")
    assert moved["status"] == "selected"
    assert cflow_engine.status(cwd, scope="w1")["step_id"] == "after"


# --------------------------------------------------------------------------- #
# the clocks
# --------------------------------------------------------------------------- #
def test_the_reminder_clock_holds_off_a_busy_driver(proj):
    """The loop the issue describes: a busy driver at the unopened gate was
    told to 'call next' every interval. Now the position is a gate, and the
    clock stays out of gates."""
    cwd = str(proj)
    _forced_onto(cwd, "gateask", "ship")
    sess = _FakeSession("w1", cwd, status="busy")
    clock = cflow_clock.ReminderClock(_FakeManager({"w1": sess}))
    assert clock.scan(1000.0) == []
    assert clock.scan(1000.0 + 601) == []
    assert clock.scan(1000.0 + 6001) == []
    assert sess.delivered == []
    assert clock.timers() == {}  # not even armed: nothing is counting here


def test_the_stall_ping_holds_off_an_idle_driver(proj):
    cwd = str(proj)
    store.set_daemon_field("cflow_ping", True)
    store.set_daemon_field("cflow_ping_interval", 600)
    _forced_onto(cwd, "gateask", "ship")
    sess = _FakeSession("w1", cwd, status="idle")
    clock = cflow_clock.StallPingClock(_FakeManager({"w1": sess}))
    assert clock.scan(1000.0) == []
    assert clock.scan(1000.0 + 99999) == []
    assert sess.delivered == []


def test_the_overseer_hears_a_plain_gate_not_an_unrouted_ask(proj):
    """The way out of an idle driver's silence is the overseer's event, and
    it has to name the right thing: an approval a person owes, not a
    question the driver still has to route."""
    cwd = str(proj)
    cflow_engine.start("gateask", cwd=cwd, scope="w1")
    boss = _FakeSession("boss", cwd)
    worker = _FakeSession("w1", cwd, parent="boss")
    clock = cflow_clock.RunEventClock(_FakeManager({"boss": boss, "w1": worker}))
    assert clock.scan() == []  # first sight arms

    cflow_engine.goto("ship", cwd=cwd, scope="w1")
    events = clock.scan()
    assert [e["kind"] for e in events] == ["human-gate"]
    block = events[0]["block"]
    assert "approve the push" in block
    assert "never put to anyone" not in block
    assert "calling 'next'" not in block
    assert clock.scan() == []  # transition, not state


# --------------------------------------------------------------------------- #
# the person's door, and what follows it
# --------------------------------------------------------------------------- #
def test_a_person_approves_it_and_next_hands_out_the_step(proj):
    cwd = str(proj)
    _forced_onto(cwd, "gateask", "ship")
    out = cflow_engine.approve(cwd=cwd, scope="w1")
    assert out["status"] == "approved"
    step = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert step["status"] == "step"
    assert step["instructions"] == "push it"


# --------------------------------------------------------------------------- #
# the shapes that still need the driver
# --------------------------------------------------------------------------- #
def test_otherwise_self_without_candidates_still_needs_the_driver(proj):
    """No person is ever meant to hold this one: 'next' journals the
    unanswered decision and hands out the step. So the driver IS the one
    the run waits on, and the clock keeps saying so."""
    cwd = str(proj)
    payload = _forced_onto(cwd, "selfask", "ship")
    assert payload["status"] == "waiting_answer"
    assert cflow_clock._ask_reached_nobody(payload) is True
    assert cflow_clock._actionable(payload) is True
    opened = cflow_engine.next_step(cwd=cwd, scope="w1")
    assert opened["status"] == "step"
    assert opened["instructions"] == "push it"


def test_a_candidate_list_still_needs_the_driver_to_route_it(proj):
    """The contract of test_cflow_unrouted_ask.py, restated beside its
    neighbour: with somebody to ask, 'next' is what asks them."""
    cwd = str(proj)
    payload = _forced_onto(cwd, "routedask", "ship")
    assert payload["status"] == "waiting_answer"
    assert cflow_clock._ask_reached_nobody(payload) is True
    assert cflow_clock._actionable(payload) is True
