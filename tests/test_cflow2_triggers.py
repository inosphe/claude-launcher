"""The daemon side of cflow v2 triggers and collection watches
(docs/cflow-v2.md 2.7, 2.8, 4.5, 10 「v1 연결」): perform with a fake
manager, pushes into a v2 or v1 parent, the skip reasons, single-use
claims, the watch probe, the v1 clock's bridge to a v2 parent and the
``landing_queue`` MCP tool on a v2 holder."""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher.cflow import landing as cflow_landing

from claude_launcher.cflow import engine, landing, mcp as v1_mcp, state as state_mod
from claude_launcher.cflow2 import executors, frontend, runtime
from claude_launcher.daemon import briefing, cflow2_clock, cflow2_triggers, cflow_clock, status_checks
from claude_launcher.daemon.harness import SessionDef

T0 = 1_000_000.0

LANDING = """
collections:
  landing:
    kind: queue
    key: issue
    item: {issue: str, branch: str, tip: str, requested_by: str, note: "str?"}
    status:
      values: [requested, waiting, deferred, landed, rejected]
      initial: requested
      settled: [landed, rejected]
    access: {push: [child, daemon], set: [driver], set.landed: [daemon], remove: [driver]}
    env: {TARGET: master}
    watch:
      every: 60
      probe: [git, merge-base, --is-ancestor, "$CFLOW_ITEM_TIP", "$CFLOW_TARGET"]
      on: {0: {set: {status: landed}}}
"""

LEAD = "cflow: 2\nname: lead\n" + LANDING + """
nodes:
  a: {prompt: A}
body: a
"""

WORKER = """cflow: 2
name: worker
nodes:
  req:
    prompt: R
    triggers: [{do: enqueue-landing, at: leave}]
  after: {prompt: B}
body: {seq: [req, after]}
"""

FACTS = {"head": "abc123", "branch": "w1-feature", "in_review": ["c-1", "c-2"],
         "session": "w1"}


class Session:
    def __init__(self, name, cwd, parent=None, exited=False):
        self.sdef = SessionDef(name=name, cwd=str(cwd), parent=parent)
        self.exited = exited
        self.delivered = []

    def queue_delivery(self, text):
        self.delivered.append(text)
        return True


class Manager:
    def __init__(self, *sessions):
        self.sessions = {s.sdef.name: s for s in sessions}

    def list(self):
        return list(self.sessions.values())

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(state_mod.SESSION_ENV, raising=False)
    executors.register(None)
    yield
    executors.register(None)


def _start(cwd, text, scope):
    chart = frontend.compile_text(textwrap.dedent(text))
    return runtime.start(chart, cwd=str(cwd), scope=scope, caps={}, driver=scope, now=T0)


def _items(cwd, scope, name="landing"):
    view = runtime.collection_view(str(cwd), scope)
    return {i["issue"]: i for i in view["collections"][name]["items"]}


def _state(cwd, scope):
    from claude_launcher.cflow2 import store as cstore

    return cstore.RunStore(str(cwd), scope).load_state()


def _events(cwd, scope, name):
    return [e for e in runtime.journal(cwd=str(cwd), scope=scope) if e["event"] == name]


def _clock(manager, **kw):
    kw.setdefault("probe_runner", None)
    kw.setdefault("threaded", False)
    kw.setdefault("heartbeat", False)
    kw.setdefault("facts_fn", lambda cwd, session: dict(FACTS, session=session))
    return cflow2_clock.Cflow2Clock(manager, poll=5, **kw)


def _dirs(tmp_path):
    lead, work = tmp_path / "lead", tmp_path / "work"
    lead.mkdir()
    work.mkdir()
    return lead, work


# -- briefing / checks ------------------------------------------------------------


def test_checks_type_the_refresh_request_into_the_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(status_checks, "session_entries",
                        lambda name, enabled_only=False: [{"id": 1}, {"id": 2}])
    s = Session("w1", tmp_path)
    ok, detail = asyncio.run(cflow2_triggers.perform(
        Manager(s), str(tmp_path), "w1", {"do": "checks"}, driver="w1"))
    assert ok and "2 status check" in detail
    assert s.delivered == [status_checks.REFRESH_PROMPT]


def test_briefing_recomposes_for_the_driver(tmp_path, monkeypatch):
    seen = []

    async def compose(session, cfg, refresh=False):
        seen.append((session.sdef.name, refresh))
        return {"briefing": {"state": "working"}}

    monkeypatch.setattr(briefing, "llm_config", lambda: {"url": "x"})
    monkeypatch.setattr(briefing, "llm_configured", lambda cfg: True)
    monkeypatch.setattr(briefing, "compose", compose)
    ok, detail = asyncio.run(cflow2_triggers.perform(
        Manager(Session("w1", tmp_path)), str(tmp_path), "w1", {"do": "briefing"}, driver="w1"))
    assert ok and "working" in detail
    assert seen == [("w1", True)]


SESSION_NESTED = """cflow: 2
name: lead
nodes:
  a:
    prompt: A
    triggers: [{do: checks, at: leave}]
  ship: {prompt: ship}
  hold: {prompt: hold}
body:
  seq:
    - id: unit
      executor: [session]
      seq:
        - a
        - choose: {options: {go: {then: ship}, wait: {then: hold}}}
"""


def test_a_session_delegations_nested_run_acts_on_the_child(tmp_path, monkeypatch):
    """A nested run has no driver of its own: its briefing/checks go to the
    session carrying out the delegation, looked up when performed."""
    monkeypatch.setattr(status_checks, "session_entries",
                        lambda name, enabled_only=False: [{"id": 1}])
    kid, lead = Session("kid", tmp_path), Session("lead", tmp_path)
    clock = _clock(Manager(lead, kid), heartbeat=True)
    clock.scan()  # the heartbeat: session delegations are dispatched here
    chart = frontend.compile_text(SESSION_NESTED)
    st = runtime.start(chart, cwd=str(tmp_path), scope="lead", driver="lead", now=T0,
                       caps={"session.spawn": True, "subagent.spawn": True,
                             "mcp.claunch": True})
    (dl,) = st["delegations"]
    nested = dl["nested"]
    kw = {"cwd": nested["cwd"], "scope": nested["scope"]}
    runtime.report("a", now=T0 + 1, **kw)
    clock._claim(nested["cwd"], nested["scope"], {})
    # the child is recorded after the claim: the session is read at perform time
    assert runtime.delegate_update(dl["id"], cwd=str(tmp_path), scope="lead", child="kid")
    [(_, _, _, ok, detail)] = asyncio.run(clock.run_triggers())
    assert ok, detail
    assert kid.delivered == [status_checks.REFRESH_PROMPT] and lead.delivered == []
    assert [e["event"] for e in runtime.journal(**kw)
            if e["event"].startswith("trigger_")] == ["trigger_queued", "trigger_done"]


def test_checks_wake_a_held_driver_and_ride_behind_the_respawn(tmp_path, monkeypatch):
    """claunch-t1r9j: a held driver is asleep, not gone -- the checks request
    wakes it and is queued into the woken session. A briefing does not wake
    it (no terminal is typed into)."""
    monkeypatch.setattr(status_checks, "session_entries",
                        lambda name, enabled_only=False: [{"id": 1}])
    s = Session("w1", tmp_path, exited=True)
    s.held_at = "2026-10-13T08:00:00+00:00"
    mgr = Manager(s)
    woken = []
    mgr.wake_soon = lambda name, **kw: woken.append((name, kw)) or True
    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(tmp_path), "w1", {"do": "checks"}, driver="w1"))
    assert ok and "1 status check" in detail
    assert woken == [("w1", {"by": "cflow2-checks", "deliver": status_checks.REFRESH_PROMPT})]
    assert s.delivered == []

    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(tmp_path), "w1", {"do": "briefing"}, driver="w1"))
    assert not ok and "not live" in detail and len(woken) == 1


@pytest.mark.parametrize("do", ["checks", "briefing"])
def test_a_driver_that_is_not_live_is_a_skip(tmp_path, do):
    mgr = Manager(Session("w1", tmp_path, exited=True))
    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(tmp_path), "w1", {"do": do}, driver="w1"))
    assert not ok and "not live" in detail


def test_an_unknown_action_is_a_skip(tmp_path):
    ok, detail = asyncio.run(cflow2_triggers.perform(
        Manager(), str(tmp_path), "w1", {"do": "teleport"}, driver="w1"))
    assert not ok and "teleport" in detail


def test_a_slow_action_is_bounded(tmp_path, monkeypatch):
    async def never(*a, **k):
        await asyncio.sleep(10)

    monkeypatch.setattr(cflow2_triggers, "perform", never)
    ok, detail = asyncio.run(cflow2_triggers.perform_bounded(
        Manager(), str(tmp_path), "w1", {"do": "briefing"}, timeout=0.05))
    assert not ok and "did not finish" in detail


# -- push into a v2 parent -----------------------------------------------------------


def test_enqueue_landing_pushes_into_the_v2_parents_collection_once(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    _start(work, WORKER, "w1")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    runtime.report("req", cwd=str(work), scope="w1", now=T0 + 1)
    clock = _clock(mgr)
    clock.scan(now=T0 + 2)
    done = asyncio.run(clock.run_triggers())
    assert [(d[3]) for d in done] == [True], done
    items = _items(lead, "lead")
    assert set(items) == {"c-1", "c-2"}
    assert items["c-1"]["tip"] == "abc123" and items["c-1"]["branch"] == "w1-feature"
    assert items["c-1"]["requested_by"] == "w1" and items["c-1"]["status"] == "requested"
    assert _events(work, "w1", "trigger_done")
    # claimed once: a second pass performs nothing
    clock.scan(now=T0 + 3)
    assert asyncio.run(clock.run_triggers()) == []
    assert len(_events(work, "w1", "trigger_done")) == 1


def test_a_stop_after_the_claim_is_performed_by_the_next_boot(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    _start(work, WORKER, "w1")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    runtime.report("req", cwd=str(work), scope="w1", now=T0 + 1)
    clock = _clock(mgr)
    clock.scan(now=T0 + 2)
    assert clock.claimed
    clock.stop.set()
    assert asyncio.run(clock.run_triggers()) == []
    assert not _events(work, "w1", "trigger_done") and not _events(work, "w1", "trigger_skipped")
    assert _items(lead, "lead") == {}
    # the record stays, claimed by the stopped boot; the same boot does not
    # take it again before it goes stale
    [rec] = _state(work, "w1")["triggers"]
    assert rec["claim_boot"] == clock.boot and rec["claimed_at"] is not None
    assert runtime.claim_triggers(str(work), "w1", boot=clock.boot, now=T0 + 3) == []
    # the next boot releases and performs it (at least once, doc 2.8)
    clock2 = _clock(mgr)
    clock2.scan(now=T0 + 4)
    [(_, _, _, ok, detail)] = asyncio.run(clock2.run_triggers())
    assert ok, detail
    assert [e["reason"] for e in _events(work, "w1", "trigger_requeued")] == [
        "the daemon restarted before this trigger completed"]
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}
    assert _state(work, "w1")["triggers"] == []
    assert len(_events(work, "w1", "trigger_done")) == 1


def test_a_stale_claim_is_released(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    _start(work, WORKER, "w1")
    runtime.report("req", cwd=str(work), scope="w1", now=T0 + 1)
    [t] = runtime.claim_triggers(str(work), "w1", boot="b1", now=T0 + 2)
    assert runtime.claim_triggers(str(work), "w1", boot="b1", now=T0 + 100) == []
    late = T0 + 3 + runtime.TRIGGER_CLAIM_STALE
    # a trigger the caller still holds is not released
    assert runtime.claim_triggers(str(work), "w1", boot="b1", now=late, held=[t["id"]]) == []
    [again] = runtime.claim_triggers(str(work), "w1", boot="b1", now=late)
    assert again["id"] == t["id"]
    assert "older than" in _events(work, "w1", "trigger_requeued")[0]["reason"]
    assert runtime.complete_trigger(str(work), "w1", t["id"], True, "ok")
    assert _state(work, "w1")["triggers"] == []


def test_a_replayed_landing_push_leaves_the_record_as_the_leader_moved_it(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    _start(work, WORKER, "w1")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    runtime.report("req", cwd=str(work), scope="w1", now=T0 + 1)
    clock = _clock(mgr)
    clock.scan(now=T0 + 2)
    [(cwd, scope, trig, _)] = list(clock.claimed)
    ok, _ = asyncio.run(cflow2_triggers.perform(mgr, cwd, scope, trig, cwds=[str(lead)],
                                                facts_fn=clock.facts_fn))
    assert ok
    runtime.collection_op("set", "landing", cwd=str(lead), scope="lead", principal="driver",
                          key="c-1", fields={"status": "deferred"})
    # the daemon stopped before completing: the same trigger runs again
    ok, detail = asyncio.run(cflow2_triggers.perform(mgr, cwd, scope, trig, cwds=[str(lead)],
                                                     facts_fn=clock.facts_fn))
    assert ok and "replay" in detail, detail
    items = _items(lead, "lead")
    assert items["c-1"]["status"] == "deferred" and items["c-2"]["status"] == "requested"
    # a different push of the same key (a new request) resets it as before
    runtime.collection_op("push", "landing", cwd=str(lead), scope="lead", principal="child",
                          session="w1", item={"issue": "c-1", "tip": "t2", "branch": "b",
                                              "requested_by": "w1"})
    assert _items(lead, "lead")["c-1"]["status"] == "requested"


def test_a_push_trigger_evaluates_each_and_item(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    _start(work, """cflow: 2
name: worker
nodes:
  req:
    prompt: R
    triggers:
      - at: leave
        push:
          to: parent:landing
          each: "$facts.in_review"
          item: {issue: "$each", tip: "$facts.head", branch: "'fixed'",
                 requested_by: "$facts.session"}
body: req
""", "w1")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    runtime.report("req", cwd=str(work), scope="w1", now=T0 + 1)
    clock = _clock(mgr)
    clock.scan(now=T0 + 2)
    [(_, _, _, ok, detail)] = asyncio.run(clock.run_triggers())
    assert ok, detail
    assert {i["branch"] for i in _items(lead, "lead").values()} == {"fixed"}


def _push(mgr, work, facts=None, push=None, mesh=None):
    trig = {"do": "push", "push": push or dict(cflow2_triggers.ENQUEUE_LANDING_PUSH)}
    return asyncio.run(cflow2_triggers.perform(
        mgr, str(work), "w1", trig, driver="w1", cwds=[],
        facts_fn=lambda c, s: dict(facts or FACTS), mesh=mesh))


class Mesh:
    """The slice of MeshManager a landing request's leader step reads."""

    def __init__(self, roles, name="m1"):
        self.name = name
        self.members = {h: SimpleNamespace(session=h, roles=r) for h, r in roles.items()}

    def meshes_for_session(self, session):
        return [{"mesh": self.name}] if session in self.members else []

    def get(self, name):
        return self

    def is_local_member(self, mesh, member):
        return True


def test_fields_the_parent_does_not_declare_are_left_out(tmp_path):
    # improv-worker-v2's landing push carries PR fields (claunch-yq80p.5); a
    # parent whose landing queue predates them still gets the request.
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH)
    push["item"] = dict(push["item"], pr_url="'https://h/o/r/pull/7'", pr_number="null")
    ok, detail = _push(mgr, work, push=push)
    assert ok, detail
    assert "left out: pr_url" in detail and "pr_number" not in detail
    assert set(_items(lead, "lead")["c-1"]) >= {"issue", "tip", "branch"}
    assert "pr_url" not in _items(lead, "lead")["c-1"]


def test_no_parent_session_is_a_skip(tmp_path):
    lead, work = _dirs(tmp_path)
    ok, detail = _push(Manager(Session("w1", work)), work)
    assert not ok and "no parent session" in detail


def test_a_parent_without_the_collection_is_a_skip(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, "cflow: 2\nname: plain\nnodes:\n  a: {prompt: A}\nbody: a\n", "lead")
    ok, detail = _push(Manager(Session("lead", lead), Session("w1", work, parent="lead")), work)
    assert not ok and "parent 'lead' has no run in this repository that declares a landing queue" in detail


def test_two_v2_holders_take_the_first_in_order(tmp_path):
    lead, work = _dirs(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _start(lead, LEAD, "lead")
    _start(other, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("x", other), Session("w1", work, parent="lead"))
    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(work), "w1", {"do": "enqueue-landing"}, driver="w1",
        cwds=[str(lead), str(other)], facts_fn=lambda c, s: dict(FACTS)))
    # Two main runs: no question is asked, the first in order takes it.
    assert ok, detail
    first, second = sorted([lead, other])
    assert set(_items(first, "lead")) == {"c-1", "c-2"} and _items(second, "lead") == {}


def test_an_empty_each_is_a_skip(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    ok, detail = _push(mgr, work, facts=dict(FACTS, in_review=[]))
    assert not ok and "empty list" in detail
    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(work), "w1", {"do": "enqueue-landing"}, driver="w1",
        facts_fn=lambda c, s: dict(FACTS, in_review=[])))
    assert not ok and "holds no issue in_review" in detail


def test_a_refused_push_is_a_skip(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD.replace("push: [child, daemon]", "push: [daemon]"), "lead")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    ok, detail = _push(mgr, work)
    assert not ok and "refused" in detail
    assert _items(lead, "lead") == {}


def test_a_partly_refused_push_is_a_skip_that_counts(tmp_path):
    lead, work = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH)
    ok, detail = _push(mgr, work, facts=dict(FACTS, in_review=["c-1", 5, "c-3"]), push=push)
    assert not ok, detail
    assert "refused 1 of 3 record(s)" in detail and "2 pushed and kept: c-1, c-3" in detail
    assert set(_items(lead, "lead")) == {"c-1", "c-3"}


# -- push into a v1 parent -----------------------------------------------------------


V1_LEADER = """
name: lead
landing_queue: {target: master}
steps:
  standby:
    instructions: take requests
"""


def _v1_lead(lead):
    wf = lead / ".claunch" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "lead.yaml").write_text(V1_LEADER, encoding="utf-8")
    lcwd = state_mod.resolve_cwd(str(lead))
    engine.start("lead", cwd=lcwd, scope="lead")
    return lcwd


def test_enqueue_landing_reaches_a_v1_parent(tmp_path, monkeypatch):
    lead, work = _dirs(tmp_path)
    lcwd = _v1_lead(lead)
    calls = []
    real = engine.enqueue_landing

    def spy(issues, branch, tip, **kw):
        calls.append((list(issues), branch, tip, kw))
        return real(issues, branch, tip, **kw)

    monkeypatch.setattr(engine, "enqueue_landing", spy)
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    ok, detail = asyncio.run(cflow2_triggers.perform(
        mgr, str(work), "w1", {"do": "enqueue-landing"}, driver="w1",
        facts_fn=lambda c, s: dict(FACTS)))
    assert ok and "added c-1, c-2" in detail, detail
    assert calls == [(["c-1", "c-2"], "w1-feature", "abc123",
                      {"by": "w1", "cwd": lcwd, "scope": "lead", "run": None})]
    queue = {e["issue"]: e for e in engine.status(cwd=lcwd, scope="lead")["landing_queue"]}
    assert queue["c-2"]["tip"] == "abc123"


def test_a_partly_refused_v1_push_is_a_skip_that_counts(tmp_path, monkeypatch):
    lead, work = _dirs(tmp_path)
    _v1_lead(lead)

    def enqueue(issues, branch, tip, **kw):
        if tip == "bad":
            raise engine.CflowError("tip not found")
        return {"added": list(issues), "renewed": []}

    monkeypatch.setattr(engine, "enqueue_landing", enqueue)
    items = [{"issue": "c-1", "branch": "b", "tip": "good", "requested_by": "w1"},
             {"issue": "c-2", "branch": "b", "tip": "bad", "requested_by": "w1"}]
    ok, detail = cflow2_triggers.push_v1((str(lead), None), "lead", items, "w1")
    assert not ok, detail
    assert "refused 1 of 2 record(s) (1 pushed and kept: c-1): c-2: tip not found" in detail


def test_a_v1_and_a_v2_holder_together_take_the_first_in_order(tmp_path):
    # Both main runs in one directory: the v1 main run (no run name) sorts
    # first; nothing is asked.
    lead, work = _dirs(tmp_path)
    lcwd = _v1_lead(lead)
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    ok, detail = _push(mgr, work)
    assert ok and "(v1)" in detail, detail
    assert {e["issue"] for e in engine.status(cwd=lcwd, scope="lead")["landing_queue"]}         == {"c-1", "c-2"}
    assert _items(lead, "lead") == {}


# -- watch ---------------------------------------------------------------------------


def test_watch_sets_landed_on_exit_0_and_leaves_the_rest(tmp_path):
    lead, _ = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    for issue, tip in (("c-1", "good"), ("c-2", "bad")):
        runtime.collection_op("push", "landing", cwd=str(lead), scope="lead",
                              principal="daemon", session="w1",
                              item={"issue": issue, "branch": "b", "tip": tip,
                                    "requested_by": "w1"})
    calls = []

    def probe(cmd, cwd, timeout=None, env=None):
        calls.append((env["CFLOW_ITEM_TIP"], env["CFLOW_TARGET"], timeout))
        return (0 if env["CFLOW_ITEM_TIP"] == "good" else 1), ""

    clock = _clock(Manager(Session("lead", lead)), probe_runner=probe)
    clock.scan(now=T0 + 100)
    assert sorted(calls) == [("bad", "master", 30.0), ("good", "master", 30.0)]
    items = _items(lead, "lead")
    assert items["c-1"]["status"] == "landed"
    assert items["c-2"]["status"] == "requested"
    # not due again inside `every`
    clock.scan(now=T0 + 110)
    assert len(calls) == 2


def test_a_timed_out_watch_probe_changes_nothing(tmp_path):
    lead, _ = _dirs(tmp_path)
    _start(lead, LEAD.replace("on: {0:", "on: {124:"), "lead")
    runtime.collection_op("push", "landing", cwd=str(lead), scope="lead", principal="daemon",
                          session="w1", item={"issue": "c-1", "branch": "b", "tip": "t",
                                              "requested_by": "w1"})
    clock = _clock(Manager(Session("lead", lead)),
                   probe_runner=lambda cmd, cwd, timeout=None, env=None: (
                       cflow2_clock.PROBE_TIMED_OUT, cflow2_clock.PROBE_TIMED_OUT_TEXT))
    clock.scan(now=T0 + 100)
    assert _items(lead, "lead")["c-1"]["status"] == "requested"


def test_a_probe_that_exits_124_itself_is_applied(tmp_path):
    lead, _ = _dirs(tmp_path)
    _start(lead, LEAD.replace("on: {0:", "on: {124:"), "lead")
    runtime.collection_op("push", "landing", cwd=str(lead), scope="lead", principal="daemon",
                          session="w1", item={"issue": "c-1", "branch": "b", "tip": "t",
                                              "requested_by": "w1"})
    clock = _clock(Manager(Session("lead", lead)),
                   probe_runner=lambda cmd, cwd, timeout=None, env=None: (124, ""))
    clock.scan(now=T0 + 100)
    assert _items(lead, "lead")["c-1"]["status"] == "landed"


def test_a_stopped_clock_runs_no_watch_probe(tmp_path):
    lead, _ = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    runtime.collection_op("push", "landing", cwd=str(lead), scope="lead", principal="daemon",
                          session="w1", item={"issue": "c-1", "branch": "b", "tip": "t",
                                              "requested_by": "w1"})
    calls = []

    def probe(cmd, cwd, timeout=None, env=None):
        calls.append(cmd)
        return 0, ""

    clock = _clock(Manager(Session("lead", lead)), probe_runner=probe)
    clock.stop.set()
    clock._watch_collections(str(lead), "lead", T0 + 100)
    assert calls == []
    assert _items(lead, "lead")["c-1"]["status"] == "requested"


# -- v1 worker -> v2 parent ------------------------------------------------------------


V1_WORKER = """
name: work
steps:
  request:
    instructions: ask to land
    triggers:
      - do: enqueue-landing
        at: leave
    next: wait
  wait:
    instructions: wait
"""


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _v1_worker_at_request(tmp_path, monkeypatch, lead_same_repo=True):
    """A v1 worker with one commit at its request step, and its parent's
    checkout ``lead`` -- a worktree of the worker's repository, or with
    ``lead_same_repo=False`` a repository of its own."""
    work, lead = tmp_path / "work", tmp_path / "lead"
    work.mkdir()
    _git(work, "init", "-q", "-b", "w1-feature")
    (work / "f.txt").write_text("x", encoding="utf-8")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-q", "-m", "f")
    if lead_same_repo:
        _git(work, "worktree", "add", "-q", "-b", "lead-co", str(lead))
    else:
        lead.mkdir()
        _git(lead, "init", "-q", "-b", "master")
    tip = _git(work, "rev-parse", "HEAD")
    wf = work / ".claunch" / "workflows"
    wf.mkdir(parents=True)
    (wf / "work.yaml").write_text(V1_WORKER, encoding="utf-8")
    wcwd = state_mod.resolve_cwd(str(work))
    engine.start("work", cwd=wcwd, scope="w1")
    engine.report("asked", cwd=wcwd, scope="w1")
    engine.next_step(cwd=wcwd, scope="w1")
    monkeypatch.setattr(landing, "in_review_of", lambda repo, session: ["c-9"])
    return lead, work, tip


def _v1_request(clock):
    [(cwd, scope, action)] = [c for c in clock.scan() if c[2]["do"] == "enqueue-landing"]
    return asyncio.run(clock._perform(cwd, scope, action))


def test_a_v1_childs_request_lands_in_a_v2_parent(tmp_path, monkeypatch):
    lead, work, tip = _v1_worker_at_request(tmp_path, monkeypatch)
    _start(lead, LEAD, "lead")
    clock = cflow_clock.TriggerClock(Manager(Session("lead", lead),
                                             Session("w1", work, parent="lead")))
    ok, detail = _v1_request(clock)
    assert ok, detail
    item = _items(lead, "lead")["c-9"]
    assert item["tip"] == tip and item["branch"] == "w1-feature"
    assert item["requested_by"] == "w1" and item["status"] == "requested"


def test_a_v1_child_skips_a_v2_parent_run_in_another_repository(tmp_path, monkeypatch):
    # A mesh spans projects: a queue in another repository would measure
    # "landed" in a git that never saw this tip (the Router's rule).
    lead, work, _ = _v1_worker_at_request(tmp_path, monkeypatch, lead_same_repo=False)
    _start(lead, LEAD, "lead")
    clock = cflow_clock.TriggerClock(Manager(Session("lead", lead),
                                             Session("w1", work, parent="lead")))
    ok, detail = _v1_request(clock)
    assert not ok and "parent 'lead' has no run in this repository" in detail, detail
    assert _items(lead, "lead") == {}


def test_a_v1_child_skips_an_exited_v2_parent(tmp_path, monkeypatch):
    lead, work, _ = _v1_worker_at_request(tmp_path, monkeypatch)
    _start(lead, LEAD, "lead")
    clock = cflow_clock.TriggerClock(Manager(Session("lead", lead, exited=True),
                                             Session("w1", work, parent="lead")))
    ok, detail = _v1_request(clock)
    assert not ok and "parent 'lead' has exited" in detail, detail
    assert _items(lead, "lead") == {}


def test_a_v1_child_of_a_parent_holding_both_queues_files_on_the_v1_one(tmp_path, monkeypatch):
    # No question is asked (claunch-9wwbb): a v1 worker's request takes the
    # parent's v1 queue run, and the v2 collection stays untouched.
    lead, work = _dirs(tmp_path)
    lcwd = _v1_lead(lead)
    _start(lead, LEAD, "lead")
    calls = []
    monkeypatch.setattr(engine, "enqueue_landing",
                        lambda *a, **kw: calls.append((a, kw)) or {"added": list(a[0])})
    monkeypatch.setattr(cflow_landing, "head", lambda cwd: "abc123")
    monkeypatch.setattr(cflow_landing, "branch", lambda cwd: "w1-feature")
    monkeypatch.setattr(cflow_landing, "in_review_of", lambda cwd, session: ["c-1"])
    clock = cflow_clock.TriggerClock(Manager(Session("lead", lead),
                                             Session("w1", work, parent="lead")))
    ok, detail = asyncio.run(clock._enqueue_landing(str(work), "w1"))
    assert ok, detail
    assert [(kw["cwd"], kw["scope"]) for _, kw in calls] == [(lcwd, "lead")]
    assert _items(lead, "lead") == {}


# -- the landing_queue MCP tool on a v2 holder ----------------------------------------


def test_landing_queue_tool_lists_and_marks_a_v2_collection(tmp_path, monkeypatch):
    lead, _ = _dirs(tmp_path)
    _start(lead, LEAD, "lead")
    runtime.collection_op("push", "landing", cwd=str(lead), scope="lead", principal="daemon",
                          session="w1", item={"issue": "c-1", "branch": "b", "tip": "t",
                                              "requested_by": "w1"})
    monkeypatch.chdir(lead)
    monkeypatch.setenv(state_mod.SESSION_ENV, "lead")
    v1_mcp._seen_run = None
    listed = v1_mcp.call_tool("landing_queue", {})
    assert listed["version"] == 2
    assert [e["issue"] for e in listed["landing_queue"]] == ["c-1"]
    marked = v1_mcp.call_tool("landing_queue", {"issue": "c-1", "status": "waiting",
                                                "note": "taking it"})
    assert marked["status"] == "landing_marked"
    assert marked["landing_queue"][0]["status"] == "waiting"
    assert marked["landing_queue"][0]["note"] == "taking it"
    with pytest.raises(engine.CflowError):
        v1_mcp.call_tool("landing_queue", {"issue": "c-1", "status": "landed"})
    assert _items(lead, "lead")["c-1"]["status"] == "waiting"
    assert v1_mcp._seen_run is None  # the v2 payload does not arm the v1 fence


# -- leader: (claunch-yq80p.9) ---------------------------------------------------------

MID = """cflow: 2
name: mid
sides: {stack: lead}
nodes:
  a: {prompt: A}
body: a
"""


def _leader_tree(tmp_path):
    """lead (main run holds landing) -> m1 (worker; its stack side run holds
    landing, its main run does not) -> w1."""
    lead, work = _dirs(tmp_path)
    mid = tmp_path / "mid"
    v2 = mid / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "lead.yaml").write_text(LEAD, encoding="utf-8")
    _start(lead, LEAD, "lead")
    _start(mid, MID, "m1")
    runtime.start_side("stack", cwd=str(mid), main="m1", caps={}, now=T0)
    mgr = Manager(Session("lead", lead), Session("m1", mid, parent="lead"),
                  Session("w1", work, parent="m1"))
    return lead, mid, work, mgr


LEADER_MESH = {"lead": ["leader"], "m1": ["worker"], "w1": ["worker"]}


def test_leader_push_skips_the_parents_side_run(tmp_path):
    lead, mid, work, mgr = _leader_tree(tmp_path)
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="leader:landing")
    ok, detail = _push(mgr, work, push=push, mesh=Mesh(LEADER_MESH))
    assert ok, detail
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}
    assert _items(mid, "m1+stack") == {}
    # parent: still reaches the parent's stack run
    ok, detail = _push(mgr, work)
    assert ok and "m1@main+stack" in detail
    assert set(_items(mid, "m1+stack")) == {"c-1", "c-2"}


def test_leader_push_with_no_holding_leader_is_a_skip(tmp_path):
    lead, work = _dirs(tmp_path)
    mgr = Manager(Session("lead", lead), Session("w1", work, parent="lead"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="leader:landing")
    ok, detail = _push(mgr, work, push=push, mesh=Mesh({"lead": ["leader"], "w1": ["worker"]}))
    assert not ok and "leader 'lead' has no run in this repository" in detail
    ok, detail = _push(mgr, work, push=push)
    assert not ok and "no mesh to look for a leader in" in detail


def test_leader_push_goes_to_the_leader_only(tmp_path):
    # An ancestor holding a main landing run that is not the leader is not
    # the target of an independent request; the leader is.
    lead, work = _dirs(tmp_path)
    mid = tmp_path / "mid"
    mid.mkdir()
    _start(lead, LEAD, "lead")
    _start(mid, LEAD, "m1")   # the parent's MAIN run holds landing too
    mgr = Manager(Session("lead", lead), Session("m1", mid, parent="lead"),
                  Session("w1", work, parent="m1"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="leader:landing")
    ok, detail = _push(mgr, work, push=push, mesh=Mesh(LEADER_MESH))
    assert ok, detail
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}
    assert _items(mid, "m1") == {}


def test_leader_push_takes_an_uncle_leader(tmp_path):
    lead, mid, work, _ = _leader_tree(tmp_path)
    # lead and m1 are both roots, so lead is w1's uncle (claunch-g0ttr).
    mgr = Manager(Session("lead", lead), Session("m1", mid), Session("w1", work, parent="m1"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="leader:landing")
    ok, detail = _push(mgr, work, push=push, mesh=Mesh(LEADER_MESH))
    assert ok, detail
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}


def test_leader_push_refuses_a_cousin_leader(tmp_path):
    lead, mid, work, _ = _leader_tree(tmp_path)
    # lead sits under another root: it is w1's cousin, beside no ancestor.
    top = tmp_path / "top"
    top.mkdir()
    mgr = Manager(Session("top", top), Session("lead", lead, parent="top"),
                  Session("m1", mid), Session("w1", work, parent="m1"))
    push = dict(cflow2_triggers.ENQUEUE_LANDING_PUSH, to="leader:landing")
    ok, detail = _push(mgr, work, push=push, mesh=Mesh(LEADER_MESH))
    assert not ok and "neither a sibling nor a direct ancestor" in detail
    assert _items(lead, "lead") == {}


def test_parent_push_from_under_a_free_role_root_reaches_the_root_leader(tmp_path):
    # claunch-g0ttr (s332 under s325): the parent is a root with no merger
    # role and no landing run; the leader is another root.
    lead, work = _dirs(tmp_path)
    mid = tmp_path / "mid"
    mid.mkdir()
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("m1", mid), Session("w1", work, parent="m1"))
    mesh = Mesh({"lead": ["leader"], "m1": [], "w1": ["worker"]})
    ok, detail = _push(mgr, work, mesh=mesh)
    assert ok, detail
    assert "parent 'm1' holds no 'merger' role" in detail and "fell back to 'lead'" in detail
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}


def test_parent_push_climbs_to_the_grandparent_then_the_sibling_leader(tmp_path):
    # stacked: a parent that cannot merge is passed over for the grandparent.
    lead, work = _dirs(tmp_path)
    mid = tmp_path / "mid"
    mid.mkdir()
    _start(lead, LEAD, "lead")
    mgr = Manager(Session("lead", lead), Session("m1", mid, parent="lead", exited=True),
                  Session("w1", work, parent="m1"))
    ok, detail = _push(mgr, work)
    assert ok and "parent 'm1' has exited" in detail and "fell back to 'lead'" in detail
    assert set(_items(lead, "lead")) == {"c-1", "c-2"}
    # ... and a root worker with no parent reaches its sibling root leader.
    lead2, work2 = tmp_path / "lead2", tmp_path / "work2"
    lead2.mkdir()
    work2.mkdir()
    _start(lead2, LEAD, "boss")
    mgr = Manager(Session("boss", lead2), Session("w1", work2))
    ok, detail = _push(mgr, work2, mesh=Mesh({"boss": ["leader"], "w1": ["worker"]}))
    assert ok and "no parent session" in detail and "fell back to 'boss'" in detail
    assert set(_items(lead2, "boss")) == {"c-1", "c-2"}


def test_trigger_when_decides_whether_it_is_recorded(tmp_path):
    text = """cflow: 2
name: w
inputs:
  relation: {type: enum, choices: [stacked, independent], default: stacked}
nodes:
  req:
    prompt: R
    triggers:
      - at: leave
        when: "$data.inputs.relation != 'independent'"
        push: {to: "parent:landing", item: {issue: "'a'"}}
      - at: leave
        when: "$data.inputs.relation == 'independent'"
        push: {to: "leader:landing", item: {issue: "'a'"}}
      - at: leave
        when: "len(3) > 0"
        push: {to: "parent:landing", item: {issue: "'b'"}}
  after: {prompt: B}
body: {seq: [req, after]}
"""
    from claude_launcher.cflow2 import inputs as inputs_mod

    chart = frontend.compile_text(text)
    for relation, target in (("stacked", "parent:landing"), ("independent", "leader:landing")):
        cwd = tmp_path / relation
        cwd.mkdir()
        c = inputs_mod.apply(chart, {"relation": relation})
        runtime.start(c, cwd=str(cwd), scope="w1", caps={}, driver="w1", now=T0)
        runtime.report("req", cwd=str(cwd), scope="w1", now=T0 + 1)
        recs = _state(cwd, "w1").get("triggers") or []
        assert [r["push"]["to"] for r in recs] == [target], relation
        # a when that cannot be evaluated is not recorded and says why
        [skip] = _events(cwd, "w1", "trigger_skipped")
        assert "when" in skip["reason"]
        # the false one is in the journal too (review N5)
        [dropped] = _events(cwd, "w1", "trigger_not_queued")
        assert "is false" in dropped["reason"]
