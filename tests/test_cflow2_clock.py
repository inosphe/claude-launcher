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

from claude_launcher.cflow2 import caps as caps_mod
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
          t: {prompt: T, executor: [headless, inline]}
        body: t
    """, caps={"headless.run": True})
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


# -- headless dispatch ----------------------------------------------------------------

FAKE = r'''
import json, os, sys
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "ran"), "a").write("x")
sys.stdout.write(json.dumps({"type": "result",
    "result": "ok\n```json\n{\"outcome\": \"ok\", \"value\": {\"n\": 1}}\n```"}))
'''


def _fake_launch(tmp_path):
    d = tmp_path / "fake"
    d.mkdir(exist_ok=True)
    script = d / "h.py"
    script.write_text(FAKE)
    return d, cflow2_exec.Launch(harness="claude", command=[sys.executable, str(script)],
                                 env=dict(os.environ, FAKE_DIR=str(d)))


HEADLESS = """
    nodes:
      t: {prompt: T, returns: {ok: {n: int}}, executor: [headless]}
      after: {prompt: after}
    body: [t, after]
"""
JSON_CAPS = {"headless.run": caps_mod.Capability(True, ["-p"]),
             "headless.output.json": caps_mod.Capability(True, ["--output-format", "json"])}


def test_queued_headless_is_dispatched_and_returns(tmp_path):
    d, launch = _fake_launch(tmp_path)
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, launch_for=lambda scope: launch, heartbeat=True)
    clock.scan()  # heartbeat: this directory is served
    st = _start(tmp_path, HEADLESS, caps=JSON_CAPS)
    assert st["delegations"][0]["status"] == "queued"
    clock.scan()
    assert (d / "ran").read_text() == "x"
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["after"]
    phases = [e.get("phase") for e in _events(tmp_path, "delegate_started")]
    assert "dispatched" in phases
    assert clock.results[0]["result"] == "returned"


def test_headless_threaded_respects_max(tmp_path):
    d, launch = _fake_launch(tmp_path)
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock = _clock(mgr, launch_for=lambda scope: launch, threaded=True, max_headless=1)
    clock.scan()
    _start(tmp_path, HEADLESS, caps=JSON_CAPS)
    clock.scan()
    clock.wait_jobs(30)
    assert clock.results and clock.results[0]["result"] == "returned"


def test_in_process_dispatcher_leaves_it_queued(tmp_path):
    clock = _clock(FakeManager())
    executors.register(clock._dispatch_hook)
    st = _start(tmp_path, HEADLESS, caps=JSON_CAPS)
    assert st["delegations"][0]["status"] == "queued"
    assert str(tmp_path.resolve()) in clock.cwds()


def test_orphaned_running_headless_is_requeued(tmp_path):
    clock = _clock(FakeManager(FakeSession("own", str(tmp_path))), launch_for=lambda s: None)
    clock.scan()
    st = _start(tmp_path, HEADLESS, caps=JSON_CAPS)
    did = st["delegations"][0]["id"]
    runtime.delegate_update(did, cwd=str(tmp_path), scope="own", status="running",
                            runner={"boot": "old-daemon"})
    clock.max_headless = 0  # do not start it again in this test
    clock.scan()
    rec = RunStore(str(tmp_path), "own").load_state()["delegations"][did]
    assert rec["status"] == "queued"
    assert any(e.get("route") == "requeue" for e in _events(tmp_path, "fallback"))


def test_headless_without_owning_session_reroutes(tmp_path):
    clock = _clock(FakeManager(FakeSession("other", str(tmp_path))), launch_for=lambda s: None)
    clock.scan()
    _start(tmp_path, """
        nodes:
          t: {prompt: T, executor: [headless, inline]}
        body: t
    """, caps={"headless.run": True})
    clock.scan()
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["t"]
    assert any(e["route"] == "headless -> inline" for e in _events(tmp_path, "fallback"))


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
    mgr = FakeManager(FakeSession("own", str(tmp_path)))

    def spawn(parent_name, body):
        mgr.sessions["kid"] = FakeSession("kid", str(tmp_path))
        return "kid"

    clock = _clock(mgr, spawn=spawn)
    clock.scan()
    st = _start(tmp_path, SESSION, caps={"session.spawn": True})  # no mcp.claunch
    did = st["delegations"][0]["id"]
    clock.scan()
    receipt = RunStore(str(tmp_path), "own").receipt_path(did, ".child.json")
    receipt.write_text(json.dumps({"outcome": "ok", "value": {"verdict": "v"}}))
    clock.scan()
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert [t["label"] for t in st["tasks"]] == ["after"]


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
