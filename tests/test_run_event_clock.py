"""The run event clock: an overseer hears when a run stops being its agent's.

The clock's contract is *transition, then one fyi*: entering a human gate,
finishing a recurring round, or losing the driving session gets one block
typed into the overseer (spawn parent first, mesh leader after), and a run
that merely sits — or moves between agent-actionable steps — is silence.
First sight arms rather than fires, so a daemon restart replays nothing;
``orphaned`` is state rather than a transition and is the one exception.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from claude_launcher import store
from claude_launcher.cflow import engine as cflow_engine
from claude_launcher.daemon import cflow_clock
from claude_launcher.daemon.harness import SessionDef
from claude_launcher.daemon.mesh import Member

import pytest

LINEAR = """
name: linear
steps:
  one:
    instructions: do one
    next: two
  two:
    instructions: do two
"""

# The gate on the SECOND step: entering it is a transition the clock can see.
GATED = """
name: gated
steps:
  one:
    instructions: do one
    next: ship
  ship:
    ask:
      prompt: ok to ship?
    instructions: ship it
"""

# The gate on the FIRST step: the run is born waiting, no transition ever.
GATED_FIRST = """
name: gatedfirst
steps:
  ship:
    ask:
      prompt: ok?
    instructions: ship it
"""

ROUNDS = """
name: rounds
recur: true
steps:
  one:
    instructions: do the round
"""


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    """An isolated project with the workflows above declared."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    d = tmp_path / "proj"
    wf = d / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    for text in (LINEAR, GATED, GATED_FIRST, ROUNDS):
        name = text.split("name:", 1)[1].split()[0]
        (wf / f"{name}.yaml").write_text(text, encoding="utf-8")
    return d


class _FakeSession:
    def __init__(
        self, name: str, cwd: str, parent: str = None, *, keep_alive: bool = False
    ) -> None:
        self.exited = False
        self.sdef = SessionDef(
            name=name, cwd=cwd, parent=parent, keep_alive=keep_alive
        )
        self.delivered: list = []
        self.deliver_ok = True
        # The kill-on-end half: ``append_wal`` records (and fails when
        # ``wal_ok`` is false), ``kill`` marks the end the way the clock's
        # caller would, and ``status_value`` lets a test park the driver
        # mid-turn.
        self.recorded: list = []
        self.wal_ok = True
        self.killed = False
        self.status_value = "idle"

    def status(self, threshold=None):
        return self.status_value

    async def deliver(self, text: str) -> bool:
        if not self.deliver_ok:
            return False
        self.delivered.append(text)
        return True

    def append_wal(self, text: str) -> bool:
        if not self.wal_ok:
            return False
        self.recorded.append(text)
        return True

    def kill(self, *, force: bool = False) -> None:
        self.killed = True
        self.exited = True


class _FakeManager:
    def __init__(self, sessions: dict) -> None:
        self._sessions = sessions

    def get(self, name: str):
        if name not in self._sessions:
            raise KeyError(name)
        return self._sessions[name]

    def persist(self) -> None:
        pass


class _FakeMesh:
    """Just enough of MeshManager for the leader fallback."""

    def __init__(self, memberships: dict, meshes: dict) -> None:
        self._memberships = memberships  # session -> [{mesh: ...}, ...]
        self._meshes = meshes            # name -> namespace with .members

    def meshes_for_session(self, session: str):
        return self._memberships.get(session, [])

    def get(self, name: str):
        return self._meshes[name]

    def _is_local(self, mesh, member) -> bool:
        return not member.machine


# --------------------------------------------------------------------------- #
# what fires, and when
# --------------------------------------------------------------------------- #
def test_gate_entry_fires_once_per_transition(proj):
    cwd = str(proj)
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    assert clock.scan() == []                       # first sight arms
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "waiting_approval"
    events = clock.scan()
    assert [e["kind"] for e in events] == ["human-gate"]
    assert "waiting on a human approval" in events[0]["block"]
    assert "gated/ship" in events[0]["block"]
    assert "do not clear it for them" in events[0]["block"]
    assert "status -t w1" in events[0]["block"]
    assert clock.scan() == []                       # still at the gate: not news


def test_first_sight_at_a_gate_arms_only(proj):
    """The restart trade, stated as a test: a clock that first sees a run
    already parked at its gate says nothing — the pull channel covers it."""
    cwd = str(proj)
    cflow_engine.start("gatedfirst", cwd=cwd, scope="w1")
    assert cflow_engine.status(cwd, scope="w1")["status"] == "waiting_approval"
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    assert clock.scan() == []
    assert clock.scan() == []


def test_round_done_fires_once(proj):
    cwd = str(proj)
    cflow_engine.start("rounds", cwd=cwd, scope="w1")
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    clock.scan()                                    # arm at the step
    cflow_engine.report("round done", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")     # ends the round; recur files
    events = clock.scan()
    assert [e["kind"] for e in events] == ["round-done"]
    assert "waiting for a goal" in events[0]["block"]
    assert clock.scan() == []


def test_a_finished_nonrecurring_run_is_not_an_event(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    clock.scan()
    for summary in ("did one", "did two"):
        cflow_engine.report(summary, cwd=cwd, scope="w1")
        cflow_engine.next_step(cwd=cwd, scope="w1")
    assert clock.scan() == []


def test_orphaned_fires_on_sight_once(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    gone = _FakeSession("w1", cwd)
    gone.exited = True
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": gone}))
    events = clock.scan()                           # state, not a transition
    assert [e["kind"] for e in events] == ["orphaned"]
    assert "nobody is driving" in events[0]["block"]
    assert clock.scan() == []                       # once per run

    # a scope no manager knows is a standalone run — never orphaned
    assert cflow_clock.RunEventClock(_FakeManager({})).scan() == []
    # the same name in another directory is somebody else's session
    elsewhere = _FakeSession("w1", str(proj.parent))
    elsewhere.exited = True
    assert (
        cflow_clock.RunEventClock(_FakeManager({"w1": elsewhere})).scan() == []
    )


def test_disabled_tracks_silently(proj):
    """Off means quiet, not blind: transitions in the dark are not replayed
    when the switch comes back on."""
    cwd = str(proj)
    store.set_daemon_field("cflow_events", False)
    cflow_engine.start("gated", cwd=cwd, scope="w1")
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    clock.scan()
    cflow_engine.report("did one", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    assert clock.scan() == []                       # disabled: silence
    store.set_daemon_field("cflow_events", True)
    assert clock.scan() == []                       # ...and no replay


# --------------------------------------------------------------------------- #
# who is told
# --------------------------------------------------------------------------- #
def _event(cwd: str) -> dict:
    return {"cwd": cwd, "scope": "w1", "kind": "human-gate", "block": "hello"}


def test_recipient_parent_first(proj):
    cwd = str(proj)
    boss = _FakeSession("boss", cwd)
    worker = _FakeSession("w1", cwd, parent="boss")
    clock = cflow_clock.RunEventClock(_FakeManager({"boss": boss, "w1": worker}))
    assert asyncio.run(clock._deliver(_event(cwd))) is True
    assert boss.delivered == ["hello"]


def test_recipient_falls_back_to_the_mesh_leader(proj):
    cwd = str(proj)
    worker = _FakeSession("w1", cwd, parent="boss")  # parent named, not present
    lead = _FakeSession("lead", cwd)
    mesh = _FakeMesh(
        {"w1": [{"mesh": "team", "handle": "w1", "role": "worker"}]},
        {
            "team": SimpleNamespace(
                members={
                    "lead": Member("lead", "lead", role="leader"),
                    "w1": Member("w1", "w1", role="worker"),
                }
            )
        },
    )
    clock = cflow_clock.RunEventClock(
        _FakeManager({"w1": worker, "lead": lead}), mesh
    )
    assert asyncio.run(clock._deliver(_event(cwd))) is True
    assert lead.delivered == ["hello"]


def test_no_overseer_settles_by_dropping(proj):
    clock = cflow_clock.RunEventClock(_FakeManager({}))
    assert asyncio.run(clock._deliver(_event(str(proj)))) is True


def test_failed_delivery_keeps_the_debt(proj):
    cwd = str(proj)
    boss = _FakeSession("boss", cwd)
    boss.deliver_ok = False
    worker = _FakeSession("w1", cwd, parent="boss")
    clock = cflow_clock.RunEventClock(_FakeManager({"boss": boss, "w1": worker}))
    event = _event(cwd)
    assert asyncio.run(clock._deliver(event)) is False   # held...
    boss.deliver_ok = True
    assert asyncio.run(clock._deliver(event)) is True    # ...and lands later
    assert boss.delivered == ["hello"]


# --------------------------------------------------------------------------- #
# the pull-side summary (the children API view)
# --------------------------------------------------------------------------- #
def test_run_summary_maps_only_the_sessions_own_run(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    out = cflow_clock.run_summary("w1", cwd)
    assert out["workflow"] == "linear"
    assert out["status"] == "step"
    assert out["step"] == "one"
    assert out["run"]
    # another scope, another directory, no directory: not this session's run
    assert cflow_clock.run_summary("w2", cwd) is None
    assert cflow_clock.run_summary("w1", str(proj.parent)) is None
    assert cflow_clock.run_summary("w1", "") is None


def test_run_summary_shows_the_recur_wait(proj):
    cwd = str(proj)
    cflow_engine.start("rounds", cwd=cwd, scope="w1")
    cflow_engine.report("round done", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    out = cflow_clock.run_summary("w1", cwd)
    # recur records the workflow by the name the round ran under, and the
    # summary passes the record through rather than prettifying it
    assert out["pending_start"]["by"] == "recur"
    assert out["pending_start"]["workflow"] == "rounds"


# --------------------------------------------------------------------------- #
# kill-on-end: a finished ONE-SHOT run gets its session reaped — record first
# --------------------------------------------------------------------------- #
def _finish_linear(cwd: str, scope: str = "w1") -> None:
    """Advance the two-step LINEAR workflow all the way to ``done``."""
    for summary in ("did one", "did two"):
        cflow_engine.report(summary, cwd=cwd, scope=scope)
        cflow_engine.next_step(cwd=cwd, scope=scope)


def _run_id(cwd: str, scope: str = "w1") -> str:
    return cflow_engine.status(cwd, scope=scope)["run"]


def test_kill_on_end_records_then_ends(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()                                # arm at the step
    _finish_linear(cwd)
    run_id = _run_id(cwd)
    assert clock.scan() == []                   # not an overseer event
    assert len(worker.recorded) == 1            # the WAL landed first
    assert "session ended" in worker.recorded[0]
    assert [s[1] for s in clock._end_pending] == ["w1"]
    asyncio.run(clock._finish_end(cwd, "w1", run_id))
    assert worker.killed is True
    assert worker.exited is True
    assert clock.scan() == []                   # marked — never replayed


def test_kill_on_end_record_failure_leaves_session_alive(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    worker.wal_ok = False
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    assert clock.scan() == []
    assert clock._end_pending == []             # no kill queued
    assert worker.recorded == []
    assert worker.killed is False
    assert worker.exited is False               # the session survives
    assert clock.scan() == []                   # and it is not retried


def test_kill_on_end_skips_a_recurring_round(proj):
    cwd = str(proj)
    cflow_engine.start("rounds", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    cflow_engine.report("round done", cwd=cwd, scope="w1")
    cflow_engine.next_step(cwd=cwd, scope="w1")
    events = clock.scan()
    assert [e["kind"] for e in events] == ["round-done"]  # recur is an EVENT
    assert worker.recorded == []                 # ...never a kill-on-end
    assert clock._end_pending == []
    assert worker.killed is False


def test_kill_on_end_skips_a_session_that_already_exited(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    gone = _FakeSession("w1", cwd)
    gone.exited = True
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": gone}))
    # The run finished while the clock was away (a restart): first sight at
    # done, session already gone — there is nothing to record into or to reap.
    _finish_linear(cwd)
    assert clock.scan() == []
    assert gone.recorded == []
    assert gone.killed is False


def test_kill_on_end_keep_alive_records_but_ends_nothing(proj):
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd, keep_alive=True)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    assert clock.scan() == []
    assert len(worker.recorded) == 1            # the record is still written
    assert "keep-alive" in worker.recorded[0]
    assert [s[1] for s in clock._end_pending] == ["w1"]
    asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd)))
    assert worker.killed is False               # ...but nothing is ended
    assert worker.exited is False


def test_kill_on_end_waits_out_a_busy_turn_then_kills(proj):
    cwd = str(proj)
    store.set_daemon_field("cflow_kill_on_end_grace", 0.3)
    try:
        cflow_engine.start("linear", cwd=cwd, scope="w1")
        worker = _FakeSession("w1", cwd)
        worker.status_value = "busy"            # the finishing turn is running
        clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
        clock.scan()
        _finish_linear(cwd)
        assert clock.scan() == []
        assert len(worker.recorded) == 1
        assert worker.killed is False           # not cut mid-turn
        asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd)))
        assert worker.killed is True            # the cap, then ended
    finally:
        store.set_daemon_field("cflow_kill_on_end_grace", None)


def test_kill_on_end_disabled_tracks_silently(proj):
    cwd = str(proj)
    store.set_daemon_field("cflow_kill_on_end", False)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    assert clock.scan() == []                   # off: silence
    assert worker.recorded == []
    assert clock._end_pending == []
    store.set_daemon_field("cflow_kill_on_end", True)
    assert clock.scan() == []                   # ...and no replay
    assert worker.recorded == []
    assert worker.killed is False


# --------------------------------------------------------------------------- #
# the ending notice: the overseer hears that the session is gone
# --------------------------------------------------------------------------- #


def test_kill_on_end_tells_the_overseer_the_session_ended(proj):
    """The kill is silent to everyone but the session that dies.

    ``end_block`` goes into the dying session's OWN transcript, which no peer
    reads, so before this the first anyone learned of the kill was a message
    that never landed. The overseer is told after the kill lands, through the
    same debt queue the other events use.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    boss = _FakeSession("boss", cwd)
    worker = _FakeSession("w1", cwd, parent="boss")
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker, "boss": boss}))
    clock.scan()
    _finish_linear(cwd)
    assert clock.scan() == []
    assert clock._debt == []                    # nothing yet: the kill is next
    asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd), "linear"))
    assert worker.killed is True
    assert [e["kind"] for e in clock._debt] == ["session-ended"]
    assert asyncio.run(clock._deliver(clock._debt[0])) is True
    block = boss.delivered[0]
    assert "session: w1" in block
    assert "ENDED" in block
    # ...and what the reader is to do instead of messaging it.
    assert "queued" in block and "claunch respawn w1" in block


def test_the_ending_notice_names_the_workflow_the_scan_queued(proj):
    """The workflow name travels with the queued end-sequence.

    ``_finish_end`` runs after the run is done and cannot read the position
    back, so a notice composed there would say '?' unless the scan hands the
    name over with the queue entry.
    """
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    clock.scan()
    assert clock._end_pending == [(cwd, "w1", _run_id(cwd), "linear")]


def test_no_ending_notice_when_keep_alive_kept_the_session(proj):
    """Nothing ended, so nothing is announced — the peers can still reach it."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd, keep_alive=True)
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    clock.scan()
    asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd), "linear"))
    assert worker.killed is False
    assert clock._debt == []


def test_the_ending_notice_is_not_gated_on_cflow_events(proj):
    """``cflow_events`` mutes transitions; a session disappearing is not one.

    The switch that governs the ending is ``cflow_kill_on_end`` — the one that
    caused the kill. Muting the notice with the other switch would restore
    exactly the silence being fixed: the daemon ends a session and the peers
    find out by talking to a closed terminal.
    """
    cwd = str(proj)
    store.set_daemon_field("cflow_events", False)
    try:
        cflow_engine.start("linear", cwd=cwd, scope="w1")
        worker = _FakeSession("w1", cwd)
        clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
        clock.scan()
        _finish_linear(cwd)
        clock.scan()
        asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd), "linear"))
        assert [e["kind"] for e in clock._debt] == ["session-ended"]
    finally:
        store.set_daemon_field("cflow_events", True)


def test_a_failed_kill_announces_nothing(proj):
    """A session that is still alive must not be reported as ended."""
    cwd = str(proj)
    cflow_engine.start("linear", cwd=cwd, scope="w1")
    worker = _FakeSession("w1", cwd)

    def boom(*, force: bool = False):
        raise RuntimeError("pty is wedged")

    worker.kill = boom
    clock = cflow_clock.RunEventClock(_FakeManager({"w1": worker}))
    clock.scan()
    _finish_linear(cwd)
    clock.scan()
    asyncio.run(clock._finish_end(cwd, "w1", _run_id(cwd), "linear"))
    assert worker.exited is False
    assert clock._debt == []
