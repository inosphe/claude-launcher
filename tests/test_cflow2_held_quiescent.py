"""The cflow v2 clock skips a held driver's slot that has nothing to do
(runtime.quiescent) until its files change (2026-10-26: 9 of the 46 live runs
in the directories the clock scans)."""

from __future__ import annotations

import textwrap

import pytest

from claude_launcher.cflow2 import frontend, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_clock

from test_cflow2_clock import FakeManager, FakeSession, _clock  # noqa: E402

T0 = 1_000_000.0

TASK = """
    nodes:
      a: {prompt: A}
    body: a
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)


def _start(cwd, src, scope="s15"):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
    return runtime.start(chart, cwd=str(cwd), scope=scope, caps={}, now=T0)


class HeldManager(FakeManager):
    def __init__(self, *sessions):
        super().__init__(*sessions)
        self.woken = []

    def wake_soon(self, name, *, by="", deliver=""):
        self.woken.append((name, deliver))
        return True


def _neighbour(cwd):
    """A live session in the same directory: the clock scans only the
    directories of live sessions."""
    return FakeSession("k1", str(cwd))


def _held(name, cwd):
    s = FakeSession(name, str(cwd))
    s.exited = True
    s.held_at = "2026-10-26T00:00:00Z"
    return s


def _counting(clock):
    """The slots each pass gave a full tick (a slot is ``s15@<daemon>``)."""
    ticks = []
    original = clock.tick_slot

    def tick_slot(cwd, scope, now, notices):
        ticks.append(scope.split("@")[0])
        return original(cwd, scope, now, notices)

    clock.tick_slot = tick_slot
    return ticks


def _quiescent(cwd, src):
    _start(cwd, src)
    return runtime.quiescent(runtime.Run.load(RunStore(str(cwd), "s15"), T0 + 1))


# -- runtime.quiescent ---------------------------------------------------------

def test_a_run_at_an_agents_task_is_quiescent(tmp_path):
    assert _quiescent(tmp_path, TASK)


@pytest.mark.parametrize("body", [
    "body:\n  seq:\n    - wait: {seconds: 30}\n    - a\n",
    "body:\n  seq:\n    - wait: {probe: check, poll: 10}\n    - a\n",
])
def test_a_run_at_a_timed_or_probed_wait_is_not(tmp_path, body):
    assert not _quiescent(tmp_path, "nodes:\n  a: {prompt: A}\n" + body)


def test_a_task_that_declares_a_restart_is_not(tmp_path):
    assert not _quiescent(tmp_path, """
        nodes:
          a: {prompt: A, restart: {linux: "bash tools/restart_live.sh", timeout: 30}}
        body: a
    """)


def test_a_run_with_monitors_is_not(tmp_path):
    assert not _quiescent(tmp_path, """
        nodes:
          a: {prompt: A}
        body: a
        spec:
          monitors:
            m:
              always: "$probe.x == 1"
              probe: {x: "echo 1"}
              every: 60
              on_violation: notify
    """)


WATCHED = """
    collections:
      landing:
        kind: queue
        key: issue
        item: {issue: str}
        status: {values: [requested, landed], initial: requested, settled: [landed]}
        access: {push: [driver, person, daemon], set: [driver, person, daemon]}
        watch: {every: 60, probe: [git, log, "-1"], on: {0: {set: {status: landed}}}}
    nodes:
      a: {prompt: A}
    body: a
"""


def test_a_watched_queue_keeps_the_run_off_only_while_a_record_is_unsettled(tmp_path):
    _start(tmp_path, WATCHED)
    st = RunStore(str(tmp_path), "s15")
    assert runtime.quiescent(runtime.Run.load(st, T0 + 1))
    kw = {"cwd": str(tmp_path), "scope": "s15", "now": T0 + 2}
    runtime.collection_op("push", "landing", principal="person", item={"issue": "x"}, **kw)
    assert not runtime.quiescent(runtime.Run.load(st, T0 + 3))
    runtime.collection_op("set", "landing", principal="person", key="x",
                          fields={"status": "landed"}, **kw)
    assert runtime.quiescent(runtime.Run.load(st, T0 + 4))


def test_an_open_delegation_is_not(tmp_path):
    _start(tmp_path, TASK)
    r = runtime.Run.load(RunStore(str(tmp_path), "s15"), T0 + 1)
    r.state["delegations"] = {"d1": {"id": "d1", "status": "pending"}}
    assert not runtime.quiescent(r)
    r.state["delegations"]["d1"]["status"] = "returned"
    assert runtime.quiescent(r)


# -- the clock -----------------------------------------------------------------

def test_a_held_drivers_quiescent_slot_is_skipped_until_it_changes(tmp_path):
    _start(tmp_path, TASK)
    clock = _clock(HeldManager(_held("s15", tmp_path), _neighbour(tmp_path)))
    ticks = _counting(clock)
    clock.scan(now=T0 + 1)
    clock.scan(now=T0 + 11)
    clock.scan(now=T0 + 21)
    assert ticks == ["s15"]
    st = RunStore(str(tmp_path), "s15")
    with st.lock():
        st.commit(dict(st.load_state(), marker=1), [])
    clock.scan(now=T0 + 31)
    assert ticks == ["s15", "s15"]


def test_a_skipped_slot_gets_a_full_pass_after_the_recheck(tmp_path):
    _start(tmp_path, TASK)
    clock = _clock(HeldManager(_held("s15", tmp_path), _neighbour(tmp_path)))
    ticks = _counting(clock)
    clock.scan(now=T0 + 1)
    clock.scan(now=T0 + cflow2_clock.HELD_RECHECK)
    assert ticks == ["s15"]
    clock.scan(now=T0 + 1 + cflow2_clock.HELD_RECHECK)
    assert ticks == ["s15", "s15"]


def test_a_live_driver_is_ticked_on_every_pass(tmp_path):
    _start(tmp_path, TASK)
    clock = _clock(FakeManager(FakeSession("s15", str(tmp_path))))
    ticks = _counting(clock)
    clock.scan(now=T0 + 1)
    clock.scan(now=T0 + 11)
    assert ticks == ["s15", "s15"]


def test_a_driver_that_wakes_is_ticked_again(tmp_path):
    _start(tmp_path, TASK)
    driver = _held("s15", tmp_path)
    clock = _clock(HeldManager(driver, _neighbour(tmp_path)))
    ticks = _counting(clock)
    clock.scan(now=T0 + 1)
    driver.exited, driver.held_at = False, None
    clock.scan(now=T0 + 11)
    assert ticks == ["s15", "s15"]


def test_a_held_wait_still_fires_and_wakes_its_driver(tmp_path):
    _start(tmp_path, """
        nodes:
          a: {prompt: A}
        body:
          seq:
            - wait: {seconds: 30}
            - a
    """)
    mgr = HeldManager(_held("s15", tmp_path), _neighbour(tmp_path))
    clock = _clock(mgr)
    ticks = _counting(clock)
    assert clock.scan(now=T0 + 1) == [] and clock.scan(now=T0 + 11) == []
    assert ticks == ["s15", "s15"]
    notices = clock.scan(now=T0 + 31)
    assert [name for _, name, _ in notices] == ["s15"]
    for cwd, name, text in notices:
        clock._deliver(cwd, name, text)
    assert [name for name, _ in mgr.woken] == ["s15"]
