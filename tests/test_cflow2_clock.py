"""The daemon's cflow v2 clock: waits, monitors, budgets, dispatch and the
session executor (docs/cflow-v2.md 4, 6.4, 7, 8). In-process: a fake
session manager, stub spawn and a fake harness script."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import executors, frontend, mcp, runtime
from claude_launcher.cflow2.store import RunStore, StoreError
from claude_launcher.daemon import cflow2_clock, cflow2_exec

T0 = 1_000_000.0


class FakeSession:
    def __init__(self, name, cwd):
        self.sdef = SimpleNamespace(name=name, cwd=cwd)
        self.exited = False
        self.delivered = []

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class FakeManager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]


@pytest.fixture(autouse=True)
def _no_dispatcher():
    executors.register(None)
    yield
    executors.register(None)


def _start(cwd, src, caps=None, scope="own", now=T0):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
    return runtime.start(chart, cwd=str(cwd), scope=scope, caps=caps or {}, now=now)


def _events(cwd, name, scope="own"):
    return [e for e in runtime.journal(cwd=str(cwd), scope=scope) if e["event"] == name]


def _clock(manager, **kw):
    kw.setdefault("probe_runner", None)
    kw.setdefault("threaded", False)
    return cflow2_clock.Cflow2Clock(manager, poll=5, **kw)


# -- waits ---------------------------------------------------------------------


def test_wait_probe_runs_without_the_run_lock(tmp_path):
    _start(tmp_path, """
        nodes:
          a: {prompt: A}
        body:
          seq:
            - wait: {probe: "check", poll: 10}
            - a
    """)
    held = []

    def probe(cmd, cwd):
        # Would time out if the clock held the slot's lock around the probe.
        try:
            with RunStore(str(tmp_path), "own").lock(timeout=0.2):
                held.append(False)
        except StoreError:
            held.append(True)
        return (0, "ok")

    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, probe_runner=probe)
    clock.scan(now=T0 + 1)
    assert held == [False]
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["a"]


def test_heartbeat_names_scanned_dirs_and_makes_dispatch_available(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr)
    assert not executors.available(str(tmp_path))
    clock.scan()
    assert executors.available(str(tmp_path))
    assert not executors.available(str(tmp_path / "elsewhere"))
    assert not executors.available(str(tmp_path), now=time.time() + 3600)
    # a run started from "another process" now queues instead of falling back
    st = _start(tmp_path, """
        nodes:
          t: {prompt: T, executor: [session, inline]}
        body: t
    """, caps={"session.spawn": True})
    assert st["delegations"][0]["status"] == "queued"
    assert not [e for e in _events(tmp_path, "fallback") if "dispatcher" in e["cause"]]


# -- monitors --------------------------------------------------------------------

MON = """
    nodes:
      a: {prompt: A}
      b: {prompt: B}
    data: {tip: abc}
    body: [a, b]
    spec:
      monitors:
        tip-unmoved:
          always: "$probe.tip == $data.tip"
          probe: {tip: "git rev-parse HEAD"}
          every: 60
          on_violation: %s
"""


def test_monitor_always_halts_and_notifies(tmp_path):
    _start(tmp_path, MON % "halt")
    value = {"v": "abc"}
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, probe_runner=lambda cmd, cwd: (0, value["v"] + "\n"))
    assert clock.scan(now=T0 + 1) == []
    value["v"] = "zzz"
    assert clock.scan(now=T0 + 30) == []  # every: 60 not elapsed
    notices = clock.scan(now=T0 + 62)
    assert len(notices) == 1 and "tip-unmoved" in notices[0][2]
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert st["status"] == "waiting_approval" and st["reason"] == "monitor_violation"
    ev = _events(tmp_path, "monitor_violation")
    assert len(ev) == 1 and ev[0]["action"] == "halt"
    # still violated: not reported again
    assert clock.scan(now=T0 + 130) == []
    st = runtime.release_halt(cwd=str(tmp_path), scope="own")
    assert st["status"] == "running"


def test_monitor_always_record_does_not_stop(tmp_path):
    _start(tmp_path, MON % "record")
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))),
                   probe_runner=lambda cmd, cwd: (1, "fatal"))
    assert clock.scan(now=T0 + 1) == []
    assert _events(tmp_path, "monitor_violation")[0]["action"] == "record"
    assert runtime.status(cwd=str(tmp_path), scope="own", tick=False)["status"] == "running"


WITHIN = """
    nodes:
      a: {prompt: A}
      b: {prompt: B}
    body: [a, b]
    spec:
      monitors:
        b-follows:
          when: {left: a}
          within: 60
          expect: {left: b}
          on_violation: notify
"""


def test_monitor_within_violated_once(tmp_path):
    _start(tmp_path, WITHIN)
    runtime.report("a", cwd=str(tmp_path), scope="own", now=T0)
    kw = {"cwd": str(tmp_path), "scope": "own"}
    assert runtime.monitors_tick(T0, None, journal_now=time.time() + 10, **kw) == []
    found = runtime.monitors_tick(T0, None, journal_now=time.time() + 120, **kw)
    assert [v["monitor"] for v in found] == ["b-follows"] and found[0]["action"] == "notify"
    assert runtime.monitors_tick(T0, None, journal_now=time.time() + 240, **kw) == []


def test_monitor_within_satisfied(tmp_path):
    _start(tmp_path, WITHIN)
    kw = {"cwd": str(tmp_path), "scope": "own"}
    runtime.report("a", now=T0, **kw)
    runtime.report("b", now=T0, **kw)
    assert runtime.monitors_tick(T0, None, journal_now=time.time() + 999, **kw) == []


# -- budgets -----------------------------------------------------------------------


def test_subagent_budget_timeout_notifies_driver(tmp_path):
    _start(tmp_path, """
        nodes:
          t: {prompt: T, returns: {ok: {}, timeout: {}}, executor: [subagent], budget: {seconds: 5}}
          after: {prompt: after}
        body:
          try: t
          catch: {timeout: after}
    """, caps={"subagent.spawn": True, "mcp.claunch": True})
    sess = FakeSession("own", str(tmp_path))
    clock = _clock(FakeManager(sess))
    assert clock.scan(now=T0 + 3) == []
    notices = clock.scan(now=T0 + 6)
    assert len(notices) == 1 and "budget" in notices[0][2] and notices[0][1] == "own"
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert st["delegations"][0]["status"] == "timeout"
    assert [t["label"] for t in st["tasks"]] == ["after"]


# -- session executor ------------------------------------------------------------------

SESSION = """
    nodes:
      t: {prompt: "review ${params.x}", returns: {ok: {verdict: str}}, executor: [session, subagent]}
      after: {prompt: after}
    params: {x: {default: thing}}
    body: [t, after]
"""
SESSION_CAPS = {"session.spawn": True, "subagent.spawn": True, "mcp.claunch": True}


def test_session_executor_spawns_child_and_child_submits(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    parent = FakeSession("own", str(run_dir))
    mgr = FakeManager(parent)
    calls = []

    def spawn(parent_name, body):
        calls.append((parent_name, body))
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path / "kid"))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(run_dir, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    assert st["delegations"][0]["executor"] == "session"
    clock.scan()
    assert calls and calls[0][0] == "own"
    body = calls[0][1]
    assert body["workflow"] == "-" and body["beads"] is False
    assert "review thing" in body["task"] and 'scope="own"' in body["task"]
    assert str(run_dir.resolve()) in body["task"]
    rec = RunStore(str(run_dir), "own").load_state()["delegations"][did]
    assert rec["status"] == "running" and rec["child"] == "kid"

    # the child, in its own directory under its own name, submits by scope+cwd
    kid_dir = tmp_path / "kid"
    kid_dir.mkdir()
    monkeypatch.chdir(kid_dir)
    monkeypatch.setenv("CLAUNCH_SESSION", "kid")
    mcp.call_tool("flow_delegate", {"delegation": did, "scope": "own",
                                    "cwd": str(run_dir), "outcome": "ok",
                                    "value": {"verdict": "fine"}})
    st = runtime.status(cwd=str(run_dir), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["after"]


def _kid_submits(tmp_path, monkeypatch, run_dir, did):
    kid_dir = tmp_path / "kid"
    kid_dir.mkdir(exist_ok=True)
    monkeypatch.chdir(kid_dir)
    monkeypatch.setenv("CLAUNCH_SESSION", "kid")
    mcp.call_tool("flow_delegate", {"delegation": did, "scope": "own",
                                    "cwd": str(run_dir), "outcome": "ok",
                                    "value": {"verdict": "fine"}})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)


def _session_run(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mgr = FakeManager(FakeSession("own", str(run_dir)))

    def spawn(parent_name, body):
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path / "kid"))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(run_dir, SESSION, caps=SESSION_CAPS)
    assert clock.scan() == []  # spawns the child
    return clock, run_dir, st["delegations"][0]["id"]


def test_a_child_session_result_wakes_the_driver_once(tmp_path, monkeypatch):
    """claunch-feve0: a delegation that another process closes (a child
    session's flow_delegate) opens the driver's next task while the driver
    may be idle; the clock tells it once."""
    clock, run_dir, did = _session_run(tmp_path, monkeypatch)
    _kid_submits(tmp_path, monkeypatch, run_dir, did)
    notices = clock.scan()
    assert len(notices) == 1
    cwd, name, text = notices[0]
    assert name == "own" and "waiting on you: after" in text
    assert cflow2_clock.WHY_DELEGATION in text
    assert clock.scan() == []  # once per closing, not every pass


def test_a_pass_that_fails_later_does_not_repeat_the_notice(tmp_path, monkeypatch):
    """A step after the notice raising (caught per slot by scan) must not
    leave the old open set behind, or every pass announces it again."""
    clock, run_dir, did = _session_run(tmp_path, monkeypatch)
    _kid_submits(tmp_path, monkeypatch, run_dir, did)

    def boom(*a, **kw):
        raise runtime.FlowError("restart record unreadable")

    monkeypatch.setattr(clock, "_claim_restarts", boom)
    assert len(clock.scan()) == 1
    assert clock.scan() == []


def test_a_closed_subagent_delegation_is_not_announced(tmp_path, monkeypatch):
    """The driver submits a subagent's result itself, so it is awake."""
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    _start(tmp_path, """
        nodes:
          t: {prompt: T, returns: {ok: {}}, executor: [subagent]}
          after: {prompt: after}
        body: [t, after]
    """, caps={"subagent.spawn": True, "mcp.claunch": True})
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))))
    assert clock.scan(now=T0 + 1) == []
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    did = st["delegations"][0]["id"]
    runtime.delegate_submit(did, "ok", {}, cwd=str(tmp_path), scope="own", now=T0 + 2)
    assert [t["label"] for t in runtime.status(cwd=str(tmp_path), scope="own",
                                               tick=False)["tasks"]] == ["after"]
    assert clock.scan(now=T0 + 3) == []


def test_session_spawn_refused_falls_back_to_next_executor(tmp_path):
    def spawn(parent_name, body):
        raise cflow2_exec.SpawnRefused("spawn policy: max_children reached")

    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))), spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    clock.scan()
    rec = RunStore(str(tmp_path), "own").load_state()["delegations"][did]
    assert rec["executor"] == "subagent" and rec["status"] == "pending"
    fb = [e for e in _events(tmp_path, "fallback") if e.get("delegation") == did]
    assert fb and fb[-1]["route"] == "session -> subagent" and "max_children" in fb[-1]["cause"]


def test_session_refused_with_no_other_executor_goes_inline(tmp_path):
    def spawn(parent_name, body):
        raise cflow2_exec.SpawnRefused("denied")

    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))), spawn=spawn)
    clock.scan()
    _start(tmp_path, """
        nodes:
          t: {prompt: T, executor: [session]}
        body: t
    """, caps={"session.spawn": True})
    clock.scan()
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["t"]


def test_child_exit_without_result_fails_delegation(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))

    def spawn(parent_name, body):
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    clock.scan()
    mgr.sessions["kid"].exited = True
    clock.scan()
    rec = RunStore(str(tmp_path), "own").load_state()["delegations"][did]
    assert rec["status"] == "failed"
    assert any("ended without" in (e.get("reason") or "")
               for e in _events(tmp_path, "delegate_failed"))


def test_child_receipt_file_is_submitted(tmp_path):
    keys = []
    clock, did, receipt = _receipt_setup(tmp_path, keys)  # no mcp.claunch
    assert keys and keys[0]
    receipt.write_text(_keyed(keys, {"outcome": "ok", "value": {"verdict": "v"}}))
    clock.scan()
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["after"]


def test_a_receipt_without_the_childs_key_is_set_aside(tmp_path):
    """G1: the parent driver can write into receipts/, but not with the key
    that only the child's task text carries."""
    keys = []
    clock, did, receipt = _receipt_setup(tmp_path, keys)
    for doc in ({"outcome": "ok", "value": {"verdict": "forged"}},
                {"outcome": "ok", "value": {"verdict": "forged"}, "key": "0" * 32}):
        receipt.write_text(json.dumps(doc))
        notices = clock.scan()
        assert not receipt.exists()
        assert _rec(tmp_path, did)["status"] == "running"
        assert any(n[1] == "kid" and "refused" in n[2] for n in notices)
    assert len(_events(tmp_path, "receipt_refused")) == 2


def test_session_budget_timeout_tells_child(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))

    def spawn(parent_name, body):
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan(now=T0)
    _start(tmp_path, """
        nodes:
          t: {prompt: T, executor: [session], budget: {seconds: 10}}
        body: t
    """, caps={"session.spawn": True, "mcp.claunch": True})
    clock.scan(now=T0)
    notices = clock.scan(now=T0 + 20)
    assert {n[1] for n in notices} == {"own", "kid"}


def test_in_process_dispatcher_leaves_it_queued(tmp_path):
    clock = _clock(FakeManager())
    executors.register(clock._dispatch_hook)
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    assert st["delegations"][0]["status"] == "queued"
    assert str(tmp_path.resolve()) in clock.cwds()


def test_a_delegation_left_on_the_removed_headless_executor_is_rerouted(tmp_path):
    """An old run's record still says ``headless`` (claunch-xjbrh): nothing
    runs it any more, so the clock moves it to the node's next executor."""
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))),
                   spawn=lambda parent, body: pytest.fail("spawned in the rerouting pass"))
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    runtime.delegate_update(did, cwd=str(tmp_path), scope="own", executor="headless")
    clock.scan()
    rec = RunStore(str(tmp_path), "own").load_state()["delegations"][did]
    assert rec["executor"] == "session" and rec["status"] == "queued"
    assert any(e["route"] == "headless -> session" for e in _events(tmp_path, "fallback"))


def _wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _rec(cwd, did, scope="own"):
    return RunStore(str(cwd), scope).load_state()["delegations"][did]


def test_stopped_clock_claims_nothing(tmp_path):
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))),
                   spawn=lambda parent, body: pytest.fail("spawned after stop"))
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    clock.stop.set()
    assert clock.scan() == []
    assert _rec(tmp_path, st["delegations"][0]["id"])["status"] == "queued"


# -- B6: session spawn bookkeeping ----------------------------------------------------


class KidManager(FakeManager):
    def __init__(self, *sessions):
        super().__init__(*sessions)
        self.kids = {}
        self.killed = []

    def children(self, name):
        return [k for k, p in self.kids.items() if p == name]

    def add_kid(self, name, parent, task, cwd):
        s = FakeSession(name, cwd)
        s.sdef.task = task
        self.sessions[name] = s
        self.kids[name] = parent
        return s


def test_spawning_persisted_before_post_and_assignee_written(tmp_path):
    mgr = KidManager(FakeSession("own", str(tmp_path)))
    seen = {}

    def spawn(parent, body):
        st = RunStore(str(tmp_path), "own").load_state()
        rec = list(st["delegations"].values())[0]
        seen.update(status=rec["status"], runner=rec["runner"])
        mgr.add_kid("kid", parent, body["task"], str(tmp_path))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    clock.scan()
    assert seen["status"] == "running" and seen["runner"]["phase"] == "spawning"
    assert seen["runner"]["marker"] in mgr.sessions["kid"].sdef.task
    rec = _rec(tmp_path, did)
    assert rec["child"] == "kid" and rec["assignee"] == "kid"
    assert rec["runner"]["phase"] == "spawned"


def test_spawn_timeout_adopts_existing_child(tmp_path):
    mgr = KidManager(FakeSession("own", str(tmp_path)))

    def spawn(parent, body):
        mgr.add_kid("kid", parent, body["task"], str(tmp_path))  # created, answer lost
        raise cflow2_exec.SpawnRefused("timed out")

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    clock.scan()
    rec = _rec(tmp_path, did)
    assert rec["executor"] == "session" and rec["child"] == "kid" and rec["assignee"] == "kid"
    assert clock.results[0]["result"] == "adopted"


def test_orphan_child_appearing_after_reroute_is_killed(tmp_path):
    mgr = KidManager(FakeSession("own", str(tmp_path)))
    bodies = []
    killed = []

    def spawn(parent, body):
        bodies.append(body)
        raise cflow2_exec.SpawnRefused("timed out")

    clock = _clock(mgr, spawn=spawn, kill_child=lambda p, c: killed.append((p, c)))
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    clock.scan()
    assert _rec(tmp_path, did)["executor"] == "subagent"
    mgr.add_kid("late", "own", bodies[0]["task"], str(tmp_path))  # the daemon finished later
    clock.scan()
    assert killed == [("own", "late")]
    ev = _events(tmp_path, "orphan_child")
    assert ev and ev[0]["child"] == "late" and ev[0]["outcome"] == "killed"
    clock.scan()
    assert killed == [("own", "late")]  # once


def test_boot_requeues_spawning_without_child_once_then_reroutes(tmp_path):
    mgr = KidManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, spawn=lambda p, b: (_ for _ in ()).throw(AssertionError("no spawn")))
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    spawning = {"boot": "old", "phase": "spawning", "marker": "[cflow2-delegation own/x/d1]"}
    runtime.delegate_update(did, cwd=str(tmp_path), scope="own", status="running",
                            runner=spawning)
    clock._session(str(tmp_path), "own", did, _rec(tmp_path, did), time.time(), [])
    rec = _rec(tmp_path, did)
    assert rec["status"] == "queued" and rec["respawns"] == 1
    runtime.delegate_update(did, cwd=str(tmp_path), scope="own", status="running",
                            runner=spawning)
    clock._session(str(tmp_path), "own", did, _rec(tmp_path, did), time.time(), [])
    rec = _rec(tmp_path, did)
    assert rec["executor"] == "subagent" and rec["status"] == "pending"


def test_boot_adopts_child_of_interrupted_spawn(tmp_path):
    mgr = KidManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr)
    clock.scan()
    st = _start(tmp_path, SESSION, caps=SESSION_CAPS)
    did = st["delegations"][0]["id"]
    marker = "[cflow2-delegation own/x/d1]"
    runtime.delegate_update(did, cwd=str(tmp_path), scope="own", status="running",
                            runner={"boot": "old", "phase": "spawning", "marker": marker})
    mgr.add_kid("kid", "own", "task\n" + marker, str(tmp_path))
    clock.scan()
    rec = _rec(tmp_path, did)
    assert rec["status"] == "running" and rec["child"] == "kid" and rec["assignee"] == "kid"


# -- D: child receipt and heartbeat --------------------------------------------------


def _key_of(body):
    import re

    m = re.search(r'"key": "([0-9a-f]{32})"', body["task"])
    return m.group(1) if m else None


def _receipt_setup(tmp_path, keys=None):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    keys = [] if keys is None else keys

    def spawn(parent_name, body):
        keys.append(_key_of(body))
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps={"session.spawn": True})
    did = st["delegations"][0]["id"]
    clock.scan()
    return clock, did, RunStore(str(tmp_path), "own").receipt_path(did, ".child.json")


def _keyed(keys, doc):
    return json.dumps(dict(doc, key=keys[0]))


def test_partial_child_receipt_waits(tmp_path):
    keys = []
    clock, did, receipt = _receipt_setup(tmp_path, keys)
    receipt.write_text('{"outcome": "ok", "val')
    clock.scan()
    assert receipt.exists() and _rec(tmp_path, did)["status"] == "running"
    receipt.write_text(_keyed(keys, {"outcome": "ok", "value": {"verdict": "v"}}))
    clock.scan()
    assert _rec(tmp_path, did)["status"] == "returned"


def test_stale_partial_receipt_is_submitted_and_refused(tmp_path):
    clock, did, receipt = _receipt_setup(tmp_path)
    receipt.write_text('{"outcome": "ok", "val')
    old = time.time() - cflow2_clock.RECEIPT_SETTLE - 5
    os.utime(receipt, (old, old))
    notices = clock.scan()
    assert any(n[1] == "kid" and "refused" in n[2] for n in notices)


def test_receipt_rename_failure_submits_nothing(tmp_path, monkeypatch):
    keys = []
    clock, did, receipt = _receipt_setup(tmp_path, keys)
    receipt.write_text(_keyed(keys, {"outcome": "ok", "value": {"verdict": "v"}}))
    submitted = []
    monkeypatch.setattr(runtime, "delegate_submit", lambda *a, **k: submitted.append(k))
    from pathlib import Path

    def boom(self, target):
        raise OSError("busy")
    monkeypatch.setattr(Path, "rename", boom)
    clock.scan()
    assert submitted == [] and receipt.exists()


def test_heartbeat_refreshed_between_probes(tmp_path, monkeypatch):
    monkeypatch.setattr(cflow2_clock, "HEARTBEAT_EVERY", 0.0)
    _start(tmp_path, """
        nodes:
          a: {prompt: A}
        body:
          seq:
            - wait: {probe: "p1", poll: 10}
            - a
    """)
    _start(tmp_path, """
        nodes:
          b: {prompt: B}
        body:
          seq:
            - wait: {probe: "p2", poll: 10}
            - b
    """, scope="two")
    stamps = []

    def probe(cmd, cwd):
        stamps.append(executors.read_heartbeat()["ts"])
        time.sleep(0.05)
        return (1, "")

    # each slot's driver is a session of this daemon (claunch-7iub9)
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path)),
                               FakeSession("two", str(tmp_path))),
                   probe_runner=probe, heartbeat=True)
    clock.scan(now=T0 + 1)
    assert len(stamps) == 2 and stamps[1] > stamps[0]


def test_stuck_pass_lets_heartbeat_go_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(cflow2_clock, "HEARTBEAT_EVERY", 0.0)
    clock = _clock(FakeManager(), heartbeat=True)
    clock._beat(force=True)
    first = executors.read_heartbeat()["ts"]
    clock._scanning = True
    clock._progress = time.time() - cflow2_clock.PROGRESS_MAX - 1
    time.sleep(0.01)
    clock._beat()
    assert executors.read_heartbeat()["ts"] == first


# -- E1: probes get run values through the environment only ---------------------------


def test_run_probe_env_and_timeout_kills_group(tmp_path):
    win = sys.platform == "win32"  # cmd.exe: %VAR%, no printf, no sleep
    # cmd.exe expands %VAR% before it parses `||`, so `set VAR` is the way to print the raw value
    code, out = cflow2_clock.run_probe('set CFLOW_IN_TARGET' if win
                                       else 'printf %s "$CFLOW_IN_TARGET"', str(tmp_path),
                                       env={"CFLOW_IN_TARGET": "x || true; false"})
    assert code == 0 and out.strip().removeprefix("CFLOW_IN_TARGET=") == "x || true; false"
    code, _ = cflow2_clock.run_probe("exit 3", str(tmp_path))
    assert code == 3
    t0 = time.monotonic()
    code, out = cflow2_clock.run_probe("ping -n 30 127.0.0.1 >NUL" if win else "sleep 30 & sleep 30",
                                       str(tmp_path), timeout=0.5)
    assert code == 124 and time.monotonic() - t0 < 10


def test_an_argv_probe_reads_cflow_variables_without_a_shell(tmp_path):
    """An argv runs with no shell, so the engine fills each ``$CFLOW_<NAME>``
    from the probe's env: the value stays one argument (never a command), an
    unset one is empty, and other names are left alone (claunch-yq80p.14)."""
    code, out = cflow2_clock.run_probe(
        [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))",
         "$CFLOW_ITEM_TIP", "x=$CFLOW_GONE.", "$HOME"],
        str(tmp_path), env={"CFLOW_ITEM_TIP": "a b; false"})
    assert code == 0, out
    assert json.loads(out) == ["a b; false", "x=.", "$HOME"]
    assert runtime.expand_argv("echo $CFLOW_X", {"CFLOW_X": "1"}) == "echo $CFLOW_X"


def test_the_landing_queue_watch_probe_sees_a_landed_tip(tmp_path):
    """improv-common's landing-queue watch probe is an argv; with the tip
    and the target filled in it answers 0 once the tip is on the target
    (claunch-yq80p.14: it used to get the literal text and exit 128)."""
    import subprocess
    import yaml
    from pathlib import Path

    common = Path(runtime.__file__).parent.parent / "workflows" / "v2" / "improv-common.yaml"
    probe = yaml.safe_load(common.read_text(encoding="utf-8"))[
        "collection_types"]["landing-queue"]["watch"]["probe"]
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q", "-b", "master")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a")
    tip = git("rev-parse", "HEAD")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b")
    env = {"CFLOW_ITEM_TIP": tip, "CFLOW_TARGET": "master"}
    assert cflow2_clock.run_probe(probe, str(repo), env=env)[0] == 0
    git("checkout", "-q", "-b", "side", tip)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    env["CFLOW_ITEM_TIP"] = git("rev-parse", "HEAD")
    assert cflow2_clock.run_probe(probe, str(repo), env=env)[0] == 1


# -- the owning daemon (claunch-7iub9) ---------------------------------------------

OWNED = """
    nodes:
      reflect:
        prompt: R
        restart: {linux: "bash tools/restart_live.sh", windows: "pwsh -File tools/restart_live.ps1", timeout: 30}
    body:
      seq:
        - wait: {probe: "check", poll: 10}
        - reflect
"""


def _counting_probe(calls):
    def probe(cmd, cwd, *a, **k):
        calls.append(cmd)
        return (1, "")
    return probe


def test_a_daemon_does_not_tick_another_daemons_run_in_the_same_directory(tmp_path):
    """The gds6 incident: a named instance with a live session in the same
    directory as the default daemon's driver ticked its run and ran its
    restart. Only the daemon whose session drives the slot touches it."""
    _start(tmp_path, OWNED, scope="s15")
    other_calls, own_calls = [], []
    # the named instance: a live session of its own in the same directory
    other = _clock(FakeManager(FakeSession("k1", str(tmp_path))),
                   probe_runner=_counting_probe(other_calls), platform="Linux")
    other.scan(now=T0 + 1)
    assert other_calls == [] and other.restarts == []
    # the driver's daemon ticks it
    own = _clock(FakeManager(FakeSession("s15", str(tmp_path))),
                 probe_runner=_counting_probe(own_calls), platform="Linux")
    own.scan(now=T0 + 1)
    assert own_calls == ["check"]


def test_an_old_exited_record_with_the_drivers_name_does_not_own_the_run(tmp_path):
    """Session names are per daemon: the named instance had an exited ``s15``
    from 10-01 while the default daemon's ``s15`` drove the run."""
    _start(tmp_path, OWNED, scope="s15")
    old = FakeSession("s15", str(tmp_path))
    old.exited = True
    calls = []
    other = _clock(FakeManager(old, FakeSession("k1", str(tmp_path))),
                   probe_runner=_counting_probe(calls))
    other.scan(now=T0 + 1)
    assert calls == []


@pytest.mark.parametrize("mark", ["held_at", "paused_at"])
def test_a_held_or_paused_driver_still_owns_its_run(tmp_path, mark):
    _start(tmp_path, OWNED, scope="s15")
    driver = FakeSession("s15", str(tmp_path))
    driver.exited = True
    setattr(driver, mark, "2026-10-14T00:00:00Z")
    calls = []
    clock = _clock(FakeManager(driver, FakeSession("k1", str(tmp_path))),
                   probe_runner=_counting_probe(calls))
    clock.scan(now=T0 + 1)
    assert calls == ["check"]


def test_owner_reads_side_and_nested_slots_as_their_main_session(tmp_path):
    clock = _clock(FakeManager(FakeSession("s15", str(tmp_path))))
    assert clock.owns("s15") and clock.owns("s15+stack") and clock.owns("s15~d2")
    assert clock.owns("s15+stack~d2")
    assert not clock.owns("s16") and not clock.owns("s16+stack")
    assert not clock.owns("default") and not clock.owns("")


def test_a_directory_added_by_dispatch_follows_the_same_rule(tmp_path):
    """``extra_cwds`` (the in-process dispatcher) makes a directory scanned;
    it does not make the runs there this daemon's."""
    _start(tmp_path / "w", OWNED, scope="s15")
    calls = []
    clock = _clock(FakeManager(FakeSession("k1", str(tmp_path / "elsewhere"))),
                   probe_runner=_counting_probe(calls))
    clock.extra_cwds.add(str((tmp_path / "w").resolve()))
    assert str((tmp_path / "w").resolve()) in clock.cwds()
    clock.scan(now=T0 + 1)
    assert calls == []


def test_the_owner_claims_the_restart_the_other_daemon_left(tmp_path):
    _start(tmp_path, """
        nodes:
          reflect:
            prompt: R
            restart: {linux: "bash tools/restart_live.sh", windows: "pwsh -File tools/restart_live.ps1", timeout: 30}
        body: reflect
    """, scope="s15")
    other = _clock(FakeManager(FakeSession("k1", str(tmp_path))), platform="Linux")
    other.scan(now=T0 + 1)
    assert other.restarts == []
    st = runtime.status(cwd=str(tmp_path), scope="s15", tick=False)
    assert st["tasks"][0]["restart"]["status"] == "pending"
    own = _clock(FakeManager(FakeSession("s15", str(tmp_path))), platform="Linux")
    own.scan(now=T0 + 1)
    assert [a["command"] for _, _, a in own.restarts] == ["bash tools/restart_live.sh"]


# -- engine env: the daemon's instance identity (claunch-7iub9) ----------------------

def _instance_env(monkeypatch, tmp_path):
    from claude_launcher.daemon import instance_manifest

    monkeypatch.setenv("CLAUNCH_DAEMON", "kimyori")
    monkeypatch.setenv("CLAUNCH_DAEMON_PORT", "8379")
    monkeypatch.setenv("CLAUNCH_INSTANCE_APPLIED", "kimyori")
    monkeypatch.setenv("CLAUNCH_INSTANCE_BASE", str(tmp_path / "base"))
    monkeypatch.setenv("CLAUNCH_INSTANCE_PREV", json.dumps(
        {"CLAUDE_LAUNCHER_HOME": str(tmp_path / "base"), "CLAUNCH_DAEMON_PORT": None}))
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(tmp_path / "team"))
    return instance_manifest


def test_engine_env_drops_the_daemons_identity_and_restores_what_its_manifest_moved(
        monkeypatch, tmp_path):
    im = _instance_env(monkeypatch, tmp_path)
    env = im.engine_env(dict(os.environ), "")
    for var in im.IDENTITY_ENV:
        assert var not in env
    assert env["CLAUDE_LAUNCHER_HOME"] == str(tmp_path / "base")
    named = im.engine_env(dict(os.environ), "kimyori")
    assert named["CLAUNCH_DAEMON"] == "kimyori"
    assert not {"CLAUNCH_DAEMON_PORT", "CLAUNCH_INSTANCE_APPLIED", "CLAUNCH_INSTANCE_PREV",
                "CLAUNCH_INSTANCE_BASE"} & set(named)


def test_a_probe_gets_no_daemon_port_or_manifest_bookkeeping(monkeypatch, tmp_path):
    _instance_env(monkeypatch, tmp_path)
    code, out = cflow2_clock.run_probe(
        [sys.executable, "-c",
         "import os; print(sorted(k for k in os.environ if k.startswith('CLAUNCH_')))"],
        str(tmp_path))
    assert code == 0
    # the session's instance (this daemon's), and nothing else of the daemon's
    assert "CLAUNCH_DAEMON'" in out
    for var in ("CLAUNCH_DAEMON_PORT", "CLAUNCH_INSTANCE_APPLIED", "CLAUNCH_INSTANCE_PREV",
                "CLAUNCH_INSTANCE_BASE"):
        assert var not in out
