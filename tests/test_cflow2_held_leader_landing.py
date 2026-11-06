"""A held leader still takes landing requests and wakes for them (claunch-1brxe).

Seen on another host: leader s1297 sat at its standby choose, the idle hold
freed its program, and worker s1221's landing request went nowhere. Two
things stood in the way, and each is pinned here:

* the landing Router read a held session as gone (``exited`` without looking
  at ``held_at``), so the landing-target probe and the push both answered
  "nobody can take this";
* once a request does reach the held leader's ``landing`` queue, nothing in
  its run moves -- the standby choose is the same wait, its one re-nudge
  already spent -- so no notice was typed and the leader was never woken.
  The batched option's window opening is now announced once, and typed to a
  held driver it wakes it (``Cflow2Clock._deliver`` -> ``wake_soon``).
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import frontend, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_clock, landing_route

from test_cflow2_clock import FakeSession, _clock  # noqa: E402
from test_cflow2_held_quiescent import HeldManager, _held, _neighbour  # noqa: E402

T0 = 1_000_000.0

#: The slice of improv-leader-v2 this is about: a ``landing`` queue workers
#: push into (access as in improv-common's ``landing-queue``) and a standby
#: choose whose integrate option gathers it.
LEADER = """
    collections:
      landing:
        kind: queue
        key: issue
        item: {issue: str}
        status: {initial: requested, values: [requested, landed]}
        access: {push: [child, daemon, driver], set: [driver]}
    nodes:
      merge: {prompt: M}
    body:
      loop:
        max: 10
        while: "true"
        do:
          id: standby
          choose:
            options:
              integrate: {then: merge, interval: 300,
                          batch: {collection: landing, status: requested, size: 5}}
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)


def _start(cwd, scope="s15"):
    chart = frontend.compile_text("cflow: 2\nname: lead\n" + textwrap.dedent(LEADER))
    return runtime.start(chart, cwd=str(cwd), scope=scope, caps={}, now=T0)


def _push(cwd, key, now, scope="s15"):
    runtime.collection_op("push", "landing", cwd=str(cwd), scope=scope,
                          principal="child", session="w1", item={"issue": key}, now=now)


def _deliver_all(clock, notices):
    for cwd, name, text in notices:
        clock._deliver(cwd, name, text)


def _spent_renudge(clock, now):
    """Bring the standby choose past its single re-nudge, as an idle leader
    that later got held has: the first pass starts the interval, the next
    one past it types the one nudge."""
    clock.scan(now=now)
    nudged = clock.scan(now=now + cflow2_clock.DEFAULT_RENUDGE_INTERVAL + 1)
    assert [n for _, n, _ in nudged] == ["s15"]
    return now + cflow2_clock.DEFAULT_RENUDGE_INTERVAL + 1


# -- the Router -----------------------------------------------------------------

def _router(lead):
    sessions = {
        "w1": SimpleNamespace(exited=False, sdef=SimpleNamespace(name="w1", parent=None)),
        "lead": lead,
    }
    members = {"w1": SimpleNamespace(session="w1", roles=["worker"]),
               "lead": SimpleNamespace(session="lead", roles=["leader"])}
    mesh = SimpleNamespace(
        meshes_for_session=lambda s: [{"mesh": "m"}] if s in members else [],
        get=lambda name: SimpleNamespace(members=members),
        is_local_member=lambda mesh, m: True,
    )
    holder = landing_route.Holder("/repo", "lead@main", True, "v2")
    return landing_route.Router(
        SimpleNamespace(get=lambda n: sessions[n]), mesh,
        lambda session, repo: [holder] if session == "lead" else [])


def _lead(**state):
    return SimpleNamespace(sdef=SimpleNamespace(name="lead", parent=None), **state)


@pytest.mark.parametrize("relation", ["stacked", "independent"])
def test_a_held_leader_is_the_landing_target(relation):
    router = _router(_lead(exited=True, held_at="2026-11-06T07:00:00Z"))
    found = (router.independent("/repo", "w1") if relation == "independent"
             else router.stacked("/repo", "w1"))
    assert found.owner == "lead" and found.holder is not None, found.why


def test_an_ended_leader_is_still_refused():
    router = _router(_lead(exited=True, held_at=None))
    found = router.independent("/repo", "w1")
    assert found.holder is None
    assert "leader 'lead' has exited" in found.why


# -- the clock: a request reaching a held leader wakes it -----------------------

def test_a_request_in_a_held_leaders_queue_wakes_it_when_the_window_opens(tmp_path):
    _start(tmp_path)
    mgr = HeldManager(_held("s15", tmp_path), _neighbour(tmp_path))
    clock = _clock(mgr)
    t = _spent_renudge(clock, T0 + 1)
    _push(tmp_path, "x-1", t + 10)
    # one record of five: the window opens interval (300 s) after it arrived
    assert clock.scan(now=t + 20) == []
    assert clock.scan(now=t + 309) == []
    notices = clock.scan(now=t + 311)
    assert [n for _, n, _ in notices] == ["s15"]
    assert "batched option's window opened" in notices[0][2]
    assert "waiting on you: standby" in notices[0][2]
    _deliver_all(clock, notices)
    assert [name for name, _ in mgr.woken] == ["s15"]
    # the same window is told once
    assert clock.scan(now=t + 330) == []


def test_a_full_batch_is_told_at_once(tmp_path):
    _start(tmp_path)
    clock = _clock(HeldManager(_held("s15", tmp_path), _neighbour(tmp_path)))
    t = _spent_renudge(clock, T0 + 1)
    for i in range(5):
        _push(tmp_path, f"x-{i}", t + 10 + i)
    notices = clock.scan(now=t + 20)
    assert [n for _, n, _ in notices] == ["s15"]


def test_a_live_leader_is_told_too(tmp_path):
    """The silence was not the hold's: a live idle leader past its one
    re-nudge heard nothing of new requests either."""
    _start(tmp_path)
    lead = FakeSession("s15", str(tmp_path))
    clock = _clock(HeldManager(lead))
    t = _spent_renudge(clock, T0 + 1)
    _push(tmp_path, "x-1", t + 10)
    _deliver_all(clock, clock.scan(now=t + 311))
    assert any("batched option's window opened" in text for text in lead.delivered)


def test_an_empty_queue_tells_nothing(tmp_path):
    _start(tmp_path)
    clock = _clock(HeldManager(_held("s15", tmp_path), _neighbour(tmp_path)))
    t = _spent_renudge(clock, T0 + 1)
    assert clock.scan(now=t + 5000) == []


def test_a_waiting_batch_keeps_a_held_slot_off_quiescent(tmp_path):
    _start(tmp_path)
    run = lambda now: runtime.Run.load(RunStore(str(tmp_path), "s15"), now)  # noqa: E731
    assert runtime.quiescent(run(T0 + 1))
    _push(tmp_path, "x-1", T0 + 2)
    assert not runtime.quiescent(run(T0 + 3))
