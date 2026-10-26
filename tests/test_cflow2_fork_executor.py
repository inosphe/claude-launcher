"""cflow v2 ``fork`` executor: each member of a group runs in a quick-fork of
the run's driver (claunch-dm09i.1, docs/cflow-v2.md 6.4).

The member scope is the delegation point (``group.member_executor``); the
daemon's clock quick-forks the driver for each one, the copy drives the
member's nested run, and the group joins when every member is back. The
clock refuses a fork when the driver is itself a quick-fork or has fewer
than ``fork_min_context`` tokens left before its auto-compact, and the member
then runs in the driver's own run.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import caps as caps_mod
from claude_launcher.cflow2 import executors, frontend, ir, mcp, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_clock, cflow2_exec

T0 = 1_000_000.0
CAPS = {"session.spawn": True, "mcp.claunch": True}

CHART = """
procedures:
  part:
    params: {what: {type: str, required: true}}
    returns: {ok: {files: list}}
    nodes:
      piece: {prompt: "do ${params.what}", returns: {ok: {files: list}}}
    body:
      - piece
      - {return: ok, value: {files: "$result['piece'].value.files"}}
nodes:
  plan: {prompt: plan}
  after: {prompt: after}
body:
  - id: forks
    group: {name: forks, run: part, limit: 5, member_executor: [fork, inline], do: plan}
  - after
"""


class FakeSession:
    def __init__(self, name, cwd, quick_fork_of=None):
        self.sdef = SimpleNamespace(name=name, cwd=cwd, quick_fork_of=quick_fork_of)
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
def _no_dispatcher(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    executors.register(None)
    yield
    executors.register(None)


def _start(cwd, src=CHART, caps=None):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
    return runtime.start(chart, cwd=str(cwd), scope="own", caps=caps or CAPS, now=T0,
                         driver="own")


def _clock(mgr, *, left=500_000, forks=None, **kw):
    forks = forks if forks is not None else []

    def fork(parent, body):
        name = f"{parent}-qf{len(forks) + 1}"
        forks.append((parent, body, name))
        mgr.sessions[name] = FakeSession(name, mgr.get(parent).sdef.cwd, quick_fork_of=parent)
        return name

    kw.setdefault("probe_runner", None)
    kw.setdefault("threaded", False)
    kw.setdefault("fork_min_context", 100_000)
    clock = cflow2_clock.Cflow2Clock(
        mgr, poll=5, fork=fork, context_left=lambda s: (left, "stub"),
        spawn=lambda p, b: pytest.fail("a fork delegation must not use the spawn route"),
        **kw)
    return clock, forks


def _fork_members(cwd, n):
    for i in range(n):
        runtime.fork("forks", {"what": f"slice{i}"}, cwd=str(cwd), scope="own", now=T0)


def _dels(cwd):
    return RunStore(str(cwd), "own").load_state()["delegations"]


def _labels(cwd, scope="own"):
    return [t["label"] for t in runtime.status(cwd=str(cwd), scope=scope, tick=False)["tasks"]]


def _as(monkeypatch, session):
    monkeypatch.setenv("CLAUNCH_SESSION", session)


# -- compile and plan ------------------------------------------------------------


def test_member_executor_makes_each_member_a_delegation_point():
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(CHART))
    group = next(n for n in chart.nodes.values() if n.type == ir.T_GROUP)
    assert group.executor == []
    assert chart.node(group.get("member")).executor == ["fork", "inline"]


def test_an_unknown_member_executor_is_refused():
    with pytest.raises(frontend.FrontendError, match="unknown executor"):
        frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(
            CHART.replace("[fork, inline]", "[forks]")))


def test_fork_needs_the_spawn_policy_and_the_claunch_tools():
    node = ir.Node(id="m", type=ir.T_SCOPE, executor=["fork", "inline"])
    assert caps_mod.plan_node(node, CAPS, flat=False).route == "fork"
    no_mcp = caps_mod.plan_node(node, {"session.spawn": True}, flat=True)
    assert no_mcp.route == "inline" and ("fork", "mcp.claunch") in no_mcp.executor.skipped
    no_spawn = caps_mod.plan_node(node, {"mcp.claunch": True}, flat=True)
    assert ("fork", "session.spawn") in no_spawn.executor.skipped


def test_forks_run_at_once_in_the_shared_checkout():
    plan = caps_mod.plan_parallel("fork", CAPS, 5, write_overlap=True)
    assert plan.concurrent and not plan.isolate
    assert plan.route == "parallel-shared-checkout"


# -- the clock -------------------------------------------------------------------


def test_each_member_quick_forks_the_driver_and_the_group_joins_after_all(
        tmp_path, monkeypatch):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, forks = _clock(mgr)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 2)
    dels = _dels(tmp_path)
    assert [d["executor"] for d in dels.values()] == ["fork", "fork"]
    assert all(d["status"] == "queued" and d.get("nested") for d in dels.values())
    clock.scan()
    assert [(p, n) for p, _, n in forks] == [("own", "own-qf1"), ("own", "own-qf2")]
    body = forks[0][1]
    assert body["self_merge_allowed"] is True
    assert "quick-fork of 'own'" in body["task"] and "handoff" in body["task"]
    assert "[cflow2-delegation " in body["task"]
    dels = _dels(tmp_path)
    assert {d["child"] for d in dels.values()} == {"own-qf1", "own-qf2"}

    # the driver finishes the do-part: the group still waits for its members
    runtime.report("plan", cwd=str(tmp_path), scope="own", now=T0)
    assert _labels(tmp_path) == []

    for did, rec in sorted(dels.items()):
        nested = rec["nested"]["scope"]
        _as(monkeypatch, rec["child"])
        mcp.call_tool("flow_report", {"node": "piece", "scope": nested, "cwd": str(tmp_path),
                                      "value": {"files": [f"{did}.py"]}})
        monkeypatch.delenv("CLAUNCH_SESSION")
    assert _labels(tmp_path) == ["after"]
    value = RunStore(str(tmp_path), "own").load_state()
    settled = [e for e in runtime.journal(cwd=str(tmp_path), scope="own")
               if e["event"] == "group_settled"]
    assert settled and settled[-1]["members"] == 2 and settled[-1]["outcome"] == "ok"
    assert value["status"] == "running"


def test_only_the_fork_may_drive_its_members_run(tmp_path, monkeypatch):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, _ = _clock(mgr)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    clock.scan()
    rec = next(iter(_dels(tmp_path).values()))
    _as(monkeypatch, "someone-else")
    with pytest.raises(runtime.FlowError, match="may not operate"):
        mcp.call_tool("flow_report", {"node": "piece", "scope": rec["nested"]["scope"],
                                      "cwd": str(tmp_path), "value": {"files": []}})


def test_low_context_refuses_the_fork_and_the_member_runs_in_the_driver(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, forks = _clock(mgr, left=99_999)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    notices = clock.scan()
    assert forks == []
    rec = next(iter(_dels(tmp_path).values()))
    assert rec["status"] == "rerouted"
    fb = [e for e in runtime.journal(cwd=str(tmp_path), scope="own")
          if e["event"] == "fallback" and e.get("delegation") == rec["id"]]
    assert fb and fb[-1]["route"] == "fork -> inline"
    assert "99999 tokens left" in fb[-1]["cause"] and "100000" in fb[-1]["cause"]
    assert sorted(_labels(tmp_path)) == ["piece", "plan"]
    text = "\n".join(t for _, who, t in notices if who == "own")
    assert "fell back to your own run" in text and "fork refused" in text


def test_unknown_context_refuses_the_fork(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, forks = _clock(mgr, left=None)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    clock.scan()
    assert forks == []
    assert next(iter(_dels(tmp_path).values()))["status"] == "rerouted"


def test_a_quick_fork_driver_does_not_fork_again(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path), quick_fork_of="origin"))
    clock, forks = _clock(mgr)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    clock.scan()
    assert forks == []
    fb = [e for e in runtime.journal(cwd=str(tmp_path), scope="own") if e["event"] == "fallback"]
    assert any("does not fork again" in (e.get("cause") or "") for e in fb)


def test_a_refused_quick_fork_route_falls_back_inline(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))

    def refuse(parent, body):
        raise cflow2_exec.SpawnRefused("spawn policy: max_children reached")

    clock = cflow2_clock.Cflow2Clock(mgr, poll=5, probe_runner=None, threaded=False,
                                     fork=refuse, context_left=lambda s: (500_000, "stub"),
                                     fork_min_context=100_000)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    notices = clock.scan()
    assert next(iter(_dels(tmp_path).values()))["status"] == "rerouted"
    assert "piece" in _labels(tmp_path)
    assert any("fell back to your own run" in t for _, _, t in notices)


def test_a_fork_that_ends_without_a_result_fails_its_member(tmp_path):
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, forks = _clock(mgr)
    clock.scan()
    _start(tmp_path)
    _fork_members(tmp_path, 1)
    clock.scan()
    mgr.sessions[forks[0][2]].exited = True
    clock.scan()
    rec = next(iter(_dels(tmp_path).values()))
    assert rec["status"] == "failed"


def test_the_group_limit_counts_every_fork(tmp_path):
    _start(tmp_path)
    _fork_members(tmp_path, 5)
    with pytest.raises(runtime.FlowError, match="limit of 5"):
        _fork_members(tmp_path, 1)


# -- the fork's opening ------------------------------------------------------------


def test_the_fork_task_says_where_its_part_starts_and_ends():
    brief = {"delegation": "d3", "label": "part", "nested": {"scope": "own~d3"},
             "submit": "drive scope 'own~d3'", "tasks": [{"label": "piece", "prompt": "x"}]}
    text = executors.fork_task(brief, cwd="/w", scope="own", driver="own")
    assert "no other context" not in text
    assert "quick-fork of 'own'" in text and "drive scope 'own~d3'" in text
    assert "handoff(text=" in text and "cancelled" in text


def test_context_left_reads_the_compact_threshold_first(monkeypatch):
    from claude_launcher.daemon import ctxsize

    monkeypatch.setattr(ctxsize, "for_session", lambda sdef: {"tokens": 700_000})
    monkeypatch.setattr(ctxsize, "context_of",
                        lambda sdef, r: dict(r, compact_window=900_000,
                                             model_context_window=1_000_000))
    left, how = cflow2_exec.context_left(FakeSession("own", "/w"))
    assert left == 200_000 and "auto-compact threshold" in how
    monkeypatch.setattr(ctxsize, "context_of",
                        lambda sdef, r: dict(r, model_context_window=1_000_000))
    assert cflow2_exec.context_left(FakeSession("own", "/w"))[0] == 300_000
    monkeypatch.setattr(ctxsize, "context_of", lambda sdef, r: dict(r))
    assert cflow2_exec.context_left(FakeSession("own", "/w"))[0] is None


# -- group.allow -------------------------------------------------------------------


ALLOW = CHART.replace(
    "member_executor: [fork, inline], do: plan}",
    "member_executor: [fork, inline], allow: \"$params.on == true\", do: plan}",
).replace("nodes:\n  plan:", "params: {on: {default: false}}\nnodes:\n  plan:")


def test_a_group_whose_allow_is_false_takes_no_member(tmp_path):
    _start(tmp_path, ALLOW)
    st = runtime.status(cwd=str(tmp_path), scope="own", tick=False)
    assert st["groups"][0]["can_fork"] is False
    assert st["groups"][0]["member_executor"] == ["fork", "inline"]
    with pytest.raises(runtime.FlowError, match="does not take members"):
        _fork_members(tmp_path, 1)
    # the do-part still runs and the group joins with no member
    runtime.report("plan", cwd=str(tmp_path), scope="own", now=T0)
    assert _labels(tmp_path) == ["after"]


def test_a_group_whose_allow_is_true_takes_members(tmp_path):
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(ALLOW))
    runtime.start(chart, cwd=str(tmp_path), scope="own", caps=CAPS, now=T0, driver="own",
                  params={"on": True})
    assert runtime.status(cwd=str(tmp_path), scope="own", tick=False)["groups"][0]["can_fork"]
    _fork_members(tmp_path, 1)


def test_a_group_inside_a_forks_nested_run_is_not_forked_from_the_origin(tmp_path):
    """The session a nested run's fork would copy is the delegation's assignee
    (a quick-fork), so it is refused -- never a second copy of the origin."""
    nested_src = CHART.replace(
        "      piece: {prompt: \"do ${params.what}\", returns: {ok: {files: list}}}\n"
        "    body:\n      - piece\n",
        "      piece: {prompt: \"do ${params.what}\", returns: {ok: {files: list}}}\n"
        "      inner: {prompt: inner}\n"
        "    body:\n"
        "      - id: deeper\n"
        "        group: {name: deeper, run: leaf, member_executor: [fork, inline], do: piece}\n",
    ).replace("procedures:\n", "procedures:\n  leaf:\n    nodes:\n      lf: {prompt: lf}\n"
              "    body: [lf, {return: ok}]\n")
    mgr = FakeManager(FakeSession("own", str(tmp_path)))
    clock, forks = _clock(mgr)
    clock.scan()
    _start(tmp_path, nested_src)
    _fork_members(tmp_path, 1)
    clock.scan()
    assert [n for _, _, n in forks] == ["own-qf1"]
    nested = next(iter(_dels(tmp_path).values()))["nested"]["scope"]
    runtime.fork("deeper", None, cwd=str(tmp_path), scope=nested, now=T0)
    clock.scan()
    assert [n for _, _, n in forks] == ["own-qf1"]
    fb = [e for e in runtime.journal(cwd=str(tmp_path), scope=nested) if e["event"] == "fallback"]
    assert any("does not fork again" in (e.get("cause") or "") for e in fb)


# -- pins for the review's surviving mutants (PR #529) ---------------------------


def test_a_fork_delegation_answers_to_its_fork_like_a_session_one():
    """``_check_delegation_authority``: a fork is a session executor -- only
    its assignee submits; the driver may still brief it."""
    r = SimpleNamespace(state={"driver": "own", "status": "running"})
    rec = {"id": "d1", "executor": "fork", "child": "own-qf1"}
    runtime._check_delegation_authority(r, rec, "own-qf1", "submit")
    runtime._check_delegation_authority(r, rec, "own", "brief")
    with pytest.raises(runtime.FlowError, match="carried out by session own-qf1"):
        runtime._check_delegation_authority(r, rec, "own", "submit")
    with pytest.raises(runtime.FlowError, match="carried out by session own-qf1"):
        runtime._check_delegation_authority(r, rec, "someone-else", "brief")


def test_the_fork_floor_comes_from_the_daemon_config(monkeypatch):
    mgr = FakeManager(FakeSession("own", "."))
    clock, _ = _clock(mgr, fork_min_context=None)
    monkeypatch.setattr(cflow2_clock.store, "daemon_config",
                        lambda: {"cflow2_fork_min_context": 250_000})
    assert clock._fork_min() == 250_000
    monkeypatch.setattr(cflow2_clock.store, "daemon_config",
                        lambda: {"cflow2_fork_min_context": "lots"})
    assert clock._fork_min() == cflow2_exec.FORK_MIN_CONTEXT

    def broken():
        raise OSError("config unreadable")

    monkeypatch.setattr(cflow2_clock.store, "daemon_config", broken)
    assert clock._fork_min() == cflow2_exec.FORK_MIN_CONTEXT
    monkeypatch.setattr(cflow2_clock.store, "daemon_config", lambda: {})
    assert clock._fork_min() == cflow2_exec.FORK_MIN_CONTEXT


def test_the_fork_floor_defaults_to_100k():
    from claude_launcher import store

    assert store.DAEMON_DEFAULTS["cflow2_fork_min_context"] == 100_000
    assert cflow2_exec.FORK_MIN_CONTEXT == 100_000


def test_session_needs_constrain_a_fork_and_subagent_needs_do_not():
    assert caps_mod.need_applies("session.spawn", "fork")
    assert caps_mod.need_applies("session.spawn", "session")
    assert not caps_mod.need_applies("subagent.spawn", "fork")
    assert not caps_mod.need_applies("session.spawn", "subagent")
    assert not caps_mod.need_applies("session.spawn", "inline")


def test_check_reads_the_group_allow_expression():
    from claude_launcher.cflow2 import check

    src = CHART.replace("member_executor: [fork, inline], do: plan}",
                        "member_executor: [fork, inline], allow: \"$facts.on\", do: plan}")
    chart = frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(src))
    found = [f for f in check.check(chart) if f.code == "facts-outside-trigger"]
    assert found and "group allow" in found[0].message


def test_an_allow_that_fails_to_evaluate_takes_no_member(tmp_path, monkeypatch):
    _start(tmp_path, ALLOW)

    def fails(r, iid, text):
        raise runtime.expr_mod.ExprError("cannot evaluate")

    monkeypatch.setattr(runtime, "_guard", fails)
    assert runtime.status(cwd=str(tmp_path), scope="own", tick=False)["groups"][0][
        "can_fork"] is False
    with pytest.raises(runtime.FlowError, match="does not take members"):
        _fork_members(tmp_path, 1)
