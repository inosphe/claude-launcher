"""One measurement window per machine, and a worker budget that adapts
(claunch-afmzw).

Two daemon instances on one machine (the default and ``kimyori``, 2026-10-13)
each kept their own ``window.json``, so each spent a worker budget of 12
without seeing the other's runs. Here two :class:`WindowManager` objects on
one state file stand in for the two daemon processes: each operation reloads
the file under its lock, so what one grants the other sees. The AIMD half
pins the budget's halving on timeouts and its recovery on passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

from claude_launcher import test_window
from claude_launcher.daemon import window as window_mod
from claude_launcher.daemon.window import WindowManager


class _Manager:
    """get(name) -> session with ``exited``; KeyError for an unknown name."""

    def __init__(self, **states):
        self.states = dict(states)

    def get(self, name: str):
        if name not in self.states:
            raise KeyError(name)
        return SimpleNamespace(exited=self.states[name])


def _make(path, instance="", manager=None, caps=(1, 5), **limits) -> WindowManager:
    shape = dict(window_mod.DEFAULT_LIMITS, **limits)
    return WindowManager(
        manager,
        caps=lambda: caps,
        limits=lambda: shape,
        state_path=path,
        cores=32,
        instance=instance,
    )


def _pair(tmp_path, **limits):
    path = tmp_path / "window.json"
    return (
        _make(path, "", _Manager(a=False, b=False, c=False, s92=False), **limits),
        _make(path, "kimyori", _Manager(s92=False, k=False), **limits),
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _session(name: str):
    return SimpleNamespace(sdef=SimpleNamespace(name=name))


# --------------------------------------------------------------------------- #
# one window across daemon instances
# --------------------------------------------------------------------------- #
def test_the_worker_budget_is_spent_across_instances(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        for name in ("a", "b", "c"):
            got = await main.acquire("targeted", session=name, pid=os.getpid(), workers=4)
            assert got["granted"] and got["advisory_n"] == 4
        refused = await kimyori.acquire("targeted", session="k", pid=os.getpid(), workers=4)
        assert not refused["granted"]
        assert "worker budget" in refused["reason"]
        assert kimyori.status()["workers_in_use"] == 12

    asyncio.run(run())


def test_a_sweep_on_one_instance_excludes_the_other(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        sweep = await kimyori.acquire("sweep", session="k", pid=os.getpid())
        assert sweep["granted"]
        targeted = await main.acquire("targeted", session="a", pid=os.getpid())
        assert not targeted["granted"]
        assert targeted["reason"] == "a sweep holds the window"

    asyncio.run(run())


def test_a_background_sweep_yields_to_normal_tests_across_instances(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        first = await main.acquire("targeted", session="a", pid=os.getpid())
        waiting = asyncio.create_task(kimyori.acquire(
            "sweep", session="k", pid=os.getpid(), priority=-1, wait=10))
        await asyncio.sleep(0.2)
        assert main.status()["queue"][0]["priority"] == -1
        normal = await main.acquire("targeted", session="b", pid=os.getpid())
        assert normal["granted"]
        assert not waiting.done()
        main.release(first["grant_id"])
        assert not waiting.done()
        main.release(normal["grant_id"])
        background = await asyncio.wait_for(waiting, timeout=5)
        assert background["granted"]
        assert main.status()["holders"][0]["instance"] == "kimyori"
        assert not (await main.acquire("targeted", session="c", pid=os.getpid()))["granted"]
        kimyori.release(background["grant_id"])
    asyncio.run(run())


def test_a_waiter_is_granted_by_a_release_on_the_other_instance(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        sweep = await main.acquire("sweep", session="a", pid=os.getpid())
        waiting = asyncio.ensure_future(
            kimyori.acquire("targeted", session="k", pid=os.getpid(), wait=10)
        )
        await asyncio.sleep(0.2)
        assert not waiting.done()
        assert [q["session"] for q in main.status()["queue"]] == ["k"]
        started = time.monotonic()
        assert main.release(sweep["grant_id"])
        got = await asyncio.wait_for(waiting, timeout=5)
        assert got["granted"]
        # Granted by the release's queue pass or the waiter's next poll.
        assert time.monotonic() - started < window_mod.SHARED_POLL + 1.0
        holders = main.status()["holders"]
        assert [(h["session"], h["instance"]) for h in holders] == [("k", "kimyori")]

    asyncio.run(run())


def test_another_instances_session_is_not_reaped_as_unknown(tmp_path):
    """The main daemon has never heard of kimyori's ``k``; before claunch-
    afmzw an unknown session counted as gone and its grant was reaped."""

    async def run():
        main, kimyori = _pair(tmp_path)
        held = await kimyori.acquire("targeted", session="k", pid=os.getpid())
        assert held["granted"]
        await main.acquire("targeted", session="a", pid=os.getpid())
        assert held["grant_id"] in {h["grant_id"] for h in main.status()["holders"]}

    asyncio.run(run())


def test_session_names_are_per_instance(tmp_path):
    """``s92`` on each instance is a different session: neither its limit nor
    its exit reaches the other."""

    async def run():
        main, kimyori = _pair(tmp_path)
        mine = await main.acquire("targeted", session="s92", pid=os.getpid())
        theirs = await kimyori.acquire("targeted", session="s92", pid=os.getpid())
        assert mine["granted"] and theirs["granted"]
        main.session_exited(_session("s92"))
        left = {h["grant_id"] for h in kimyori.status()["holders"]}
        assert left == {theirs["grant_id"]}
        assert main.release_session("s92") == 0
        assert kimyori.release_session("s92") == 1

    asyncio.run(run())


def test_a_bound_grant_of_another_instance_is_reaped_by_its_pid(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        held = await kimyori.acquire(
            "sweep", session="k", pid=_dead_pid(), bound=True
        )
        assert held["granted"]
        got = await main.acquire("targeted", session="a", pid=os.getpid())
        assert got["granted"]

    asyncio.run(run())


async def _foreign_session_sweep(kimyori, daemon_pid):
    kimyori._daemon_pid = daemon_pid
    held = await kimyori.acquire("sweep", session="k", pid=_dead_pid())
    assert held["granted"]
    return held


def test_a_session_grant_of_a_daemon_that_is_gone_is_reaped(tmp_path, monkeypatch):
    """kimyori died holding a grant for its session ``k`` (not bound: taken
    with ``claunch window acquire``). Its exit hook never ran and no daemon
    serves kimyori, so nobody else would ever release it (claunch-afmzw.3)."""
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path))

    async def run():
        main, kimyori = _pair(tmp_path)
        await _foreign_session_sweep(kimyori, _dead_pid())
        got = await main.acquire("targeted", session="a", pid=os.getpid())
        assert got["granted"]

    asyncio.run(run())


def test_a_restarted_daemon_keeps_its_sessions_grants(tmp_path, monkeypatch):
    """The grant's daemon pid is gone but kimyori is served again (it
    restarted): its sessions live on and that daemon judges them."""
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path))
    state = tmp_path / "daemons" / "kimyori"
    state.mkdir(parents=True)
    (state / "daemon.json").write_text(json.dumps({"pid": os.getpid()}))

    async def run():
        main, kimyori = _pair(tmp_path)
        await _foreign_session_sweep(kimyori, _dead_pid())
        got = await main.acquire("targeted", session="a", pid=os.getpid())
        assert not got["granted"]

    asyncio.run(run())


def test_a_live_daemons_session_grant_is_not_ours_to_reap(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path))

    async def run():
        main, kimyori = _pair(tmp_path)
        await _foreign_session_sweep(kimyori, os.getpid())
        got = await main.acquire("targeted", session="a", pid=os.getpid())
        assert not got["granted"]

    asyncio.run(run())


def test_a_waiter_whose_daemon_died_leaves_the_queue(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        sweep = await main.acquire("sweep", session="a", pid=os.getpid())
        kimyori._daemon_pid = _dead_pid()  # the long-poll's daemon is gone
        waiting = asyncio.ensure_future(
            kimyori.acquire("targeted", session="k", pid=os.getpid(), wait=10)
        )
        await asyncio.sleep(0.2)
        await main.acquire("targeted", session="b", pid=os.getpid())  # reaps
        assert main.status()["queue"] == []
        got = await asyncio.wait_for(waiting, timeout=5)
        assert not got["granted"]
        main.release(sweep["grant_id"])

    asyncio.run(run())


def test_a_restarted_instance_keeps_the_other_instances_waiters(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        sweep = await main.acquire("sweep", session="a", pid=os.getpid())
        waiting = asyncio.ensure_future(
            kimyori.acquire("targeted", session="k", pid=os.getpid(), wait=10)
        )
        await asyncio.sleep(0.2)
        # The default instance restarts: its own waiters die, kimyori's live.
        _make(tmp_path / "window.json", "", _Manager(a=False))
        assert [q["session"] for q in main.status()["queue"]] == ["k"]
        main.release(sweep["grant_id"])
        assert (await asyncio.wait_for(waiting, timeout=5))["granted"]

    asyncio.run(run())


def test_the_default_state_path_is_the_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path))
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    monkeypatch.setattr(window_mod.store, "daemon_config", lambda: {})
    assert window_mod.default_state_path() == tmp_path / "daemon" / "window.json"
    monkeypatch.setattr(
        window_mod.store, "daemon_config", lambda: {"window_scope": "instance"}
    )
    assert window_mod.default_state_path() == (
        tmp_path / "daemons" / "kimyori" / "window.json"
    )


def test_the_fallback_locks_are_the_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path))
    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    assert test_window._slot_path(0) == (
        tmp_path / "daemon" / "test-window-targeted-0.lock"
    )


# --------------------------------------------------------------------------- #
# AIMD on the worker budget
# --------------------------------------------------------------------------- #
async def _run_one(w, session, result, workers=4):
    got = await w.acquire("targeted", session=session, pid=os.getpid(), workers=workers)
    assert got["granted"], got
    assert w.report_result(got["grant_id"], result)
    assert w.release(got["grant_id"])
    return got


TIMEOUT = {"outcome": "failed", "exit_code": 1, "passed": 9, "failed": 1, "timeouts": 1}
PASSED = {"outcome": "passed", "exit_code": 0, "passed": 10, "failed": 0}


def test_timeouts_halve_the_budget_and_passes_add_it_back(tmp_path):
    async def run():
        main, kimyori = _pair(tmp_path)
        await _run_one(main, "a", TIMEOUT)
        status = main.status()
        assert status["worker_budget"] == 6
        assert status["adaptive"]["lowered"] and "timeout" in status["adaptive"]["reason"]
        # The other instance decides against the same lowered budget.
        assert kimyori.status()["worker_budget"] == 6
        await _run_one(main, "a", TIMEOUT)
        assert main.status()["worker_budget"] == window_mod.DEFAULT_LIMITS["adaptive_floor"]
        await _run_one(main, "a", TIMEOUT)
        assert main.status()["worker_budget"] == 4  # the floor holds
        for expected in range(5, 12):
            await _run_one(kimyori, "k", PASSED)
            assert main.status()["worker_budget"] == expected
        await _run_one(kimyori, "k", PASSED)
        status = main.status()
        assert status["worker_budget"] == 12
        assert not status["adaptive"]["lowered"]

    asyncio.run(run())


def test_a_run_granted_before_the_decrease_does_not_decrease_again(tmp_path):
    """One overloaded moment fails every run in it; TCP halves once per
    window, and so does this."""

    async def run():
        main, _ = _pair(tmp_path)
        grants = [
            await main.acquire("targeted", session=name, pid=os.getpid(), workers=2)
            for name in ("a", "b", "c")
        ]
        for got in grants[:2]:
            assert main.report_result(got["grant_id"], TIMEOUT)
        assert main.status()["worker_budget"] == 6
        # Nor does an old run's pass count toward the recovery.
        assert main.report_result(grants[2]["grant_id"], PASSED)
        assert main.status()["worker_budget"] == 6
        for got in grants:
            main.release(got["grant_id"])
        late = await main.acquire("targeted", session="s92", pid=os.getpid(), workers=1)
        main.report_result(late["grant_id"], PASSED)
        assert main.status()["worker_budget"] == 7

    asyncio.run(run())


def test_a_lowered_budget_narrows_a_sweep(tmp_path):
    async def run():
        main, _ = _pair(tmp_path)
        await _run_one(main, "a", TIMEOUT)
        sweep = await main.acquire("sweep", session="b", pid=os.getpid())
        assert sweep["granted"] and sweep["advisory_n"] == 6

    asyncio.run(run())


def test_one_grant_adds_back_once(tmp_path):
    async def run():
        main, _ = _pair(tmp_path)
        await _run_one(main, "a", TIMEOUT)
        got = await main.acquire("targeted", session="a", pid=os.getpid(), workers=1)
        main.report_result(got["grant_id"], PASSED)
        main.report_result(got["grant_id"], PASSED)  # the parent pytest's report
        assert main.status()["worker_budget"] == 7

    asyncio.run(run())


def test_the_adaptive_budget_can_be_turned_off(tmp_path):
    async def run():
        main, _ = _pair(tmp_path, adaptive=0)
        await _run_one(main, "a", TIMEOUT)
        assert main.status()["worker_budget"] == 12
        assert not main.status()["adaptive"]["enabled"]

    asyncio.run(run())


def test_a_failure_that_is_not_a_timeout_leaves_the_budget(tmp_path):
    async def run():
        main, _ = _pair(tmp_path)
        await _run_one(main, "a", dict(TIMEOUT, timeouts=0))
        assert main.status()["worker_budget"] == 12

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# what counts as a timeout
# --------------------------------------------------------------------------- #
def _report(text: str):
    return SimpleNamespace(longreprtext=text)


def test_pytest_result_counts_the_failures_that_read_as_timeouts():
    failed = [
        _report("    def test_x():\n>       raise TimeoutError\nE       TimeoutError"),
        _report("E       subprocess.TimeoutExpired: Command '['x']' timed out after 5 seconds"),
        _report("E       AssertionError: timed out waiting for the daemon"),
        _report("E       assert 1 == 2"),
        # Source that handles a timeout is quoted in the traceback; the
        # failure itself is an assert.
        _report("        except asyncio.TimeoutError:\n            pass\nE       assert 'a' == 'b'"),
    ]
    errored = [_report("E       asyncio.exceptions.TimeoutError")]
    result = test_window.pytest_result(1, {"failed": failed, "error": errored}, 6, 1.0)
    assert result["timeouts"] == 4
    assert window_mod.clean_result(result)["timeouts"] == 4


# --------------------------------------------------------------------------- #
# a peer that holds the state lock (claunch-afmzw.2)
# --------------------------------------------------------------------------- #
def test_a_stuck_lock_holder_stalls_the_event_loop_only_briefly(tmp_path):
    """Another daemon holding ``window.lock`` must not freeze this daemon's
    loop for LOCK_WAIT: on the loop thread the wait is LOOP_LOCK_WAIT."""
    main, _ = _pair(tmp_path)
    holder = open(main._lock_path, "a+b")
    assert window_mod._try_flock(holder)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        main.status()
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.05)
        task.cancel()
        return elapsed, ticks

    try:
        elapsed, ticks = asyncio.run(run())
    finally:
        window_mod._unflock(holder)
        holder.close()
    assert elapsed < window_mod.LOOP_LOCK_WAIT + 0.5 < window_mod.LOCK_WAIT
    assert ticks > 0


def test_off_the_loop_the_lock_wait_is_the_long_one(tmp_path, monkeypatch):
    main, _ = _pair(tmp_path)
    holder = open(main._lock_path, "a+b")
    assert window_mod._try_flock(holder)
    monkeypatch.setattr(window_mod, "LOCK_WAIT", 1.2)
    try:
        started = time.monotonic()
        main.status()
        elapsed = time.monotonic() - started
    finally:
        window_mod._unflock(holder)
        holder.close()
    assert elapsed >= 1.1
