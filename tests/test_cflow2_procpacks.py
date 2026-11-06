"""cflow v2 sub procedure packs and the extended call (docs/cflow-v2.md
2.12.1-2.12.3, claunch-8t5r0): a pack directory of procedures with a role
filter, installed into a workspace; a task calling ``<pack>:<procedure>``
without listing it in ``calls``; the role refusal; the fork-mode call when
the caller declares ``fork_calls`` and the callee ``forkable``, and its
inline fallback recorded where the session card, the header and the
notification read it; the picker's list (every entry, with why not)."""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from claude_launcher.cflow2 import frontend, procpacks, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import cflow2_clock, cflow2_exec, cflow2_web

T0 = 1_000_000.0

MAIN = """
cflow: 2
name: m
nodes:
  work:
    prompt: W
    calls: [local]
    fork_calls: true
  plain: {prompt: P}
body:
  - work
  - plain
"""

LOCAL = """
cflow: 2
name: local
kind: procedure
nodes:
  t: {prompt: T}
body:
  - t
"""

PACK_MANIFEST = """
pack: 1
title: Demo pack
description: two procedures
roles: {type: whitelist, roles: [Unreal]}
"""

SOLO = """
cflow: 2
name: solo
kind: procedure
forkable: true
params:
  what: {type: str, required: true}
returns:
  ok: {done: str}
nodes:
  step: {prompt: "do ${params.what}", returns: {ok: {done: str}}}
body:
  - step
  - {return: ok, value: {done: "$result.step.value.done"}}
"""

PLAIN = """
cflow: 2
name: plain
kind: procedure
nodes:
  step: {prompt: S}
body:
  - step
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)
    v2 = tmp_path / "proj" / ".claunch" / "workflows" / "v2"
    v2.mkdir(parents=True)
    (v2 / "m.yaml").write_text(textwrap.dedent(MAIN), encoding="utf-8")
    (v2 / "local.yaml").write_text(textwrap.dedent(LOCAL), encoding="utf-8")
    catalog = tmp_path / "catalog"
    demo = catalog / "demo"
    demo.mkdir(parents=True)
    (demo / "pack.yaml").write_text(textwrap.dedent(PACK_MANIFEST), encoding="utf-8")
    (demo / "solo.yaml").write_text(textwrap.dedent(SOLO), encoding="utf-8")
    (demo / "plain.yaml").write_text(textwrap.dedent(PLAIN), encoding="utf-8")
    monkeypatch.setattr(procpacks, "package_packs_dir", lambda: catalog)


def _cwd(tmp_path):
    return str(tmp_path / "proj")


def _start(tmp_path, text=MAIN):
    chart = frontend.compile_text(textwrap.dedent(text))
    return runtime.start(chart, cwd=_cwd(tmp_path), scope="w1", caps={}, driver="w1", now=T0)


def _status(tmp_path, scope="w1"):
    return runtime.status(cwd=_cwd(tmp_path), scope=scope, now=T0, tick=False)


def _task(st, label):
    return next(t for t in st["tasks"] if t["label"].rsplit("/", 1)[-1] == label)


def _state(tmp_path, scope="w1"):
    return RunStore(_cwd(tmp_path), scope).load_state()


# -- the pack itself -------------------------------------------------------------


def test_a_catalog_pack_lists_with_its_roles_and_procedures(tmp_path):
    (pack,) = [p for p in procpacks.all_packs(_cwd(tmp_path)) if p.id == "demo"]
    assert pack.layer == "package" and not pack.installed
    assert pack.roles == {"type": "whitelist", "roles": ["unreal"]}  # lower-cased
    assert pack.to_dict()["procedures"] == ["demo:plain", "demo:solo"]


def test_a_manifest_needs_its_version_and_known_keys():
    with pytest.raises(procpacks.PackError, match="pack: 1"):
        procpacks.parse_manifest("title: x\n", "p")
    with pytest.raises(procpacks.PackError, match="unknown key"):
        procpacks.parse_manifest("pack: 1\nwho: me\n", "p")


def test_install_set_roles_and_remove(tmp_path):
    ws = _cwd(tmp_path)
    pack = procpacks.install(ws, "demo")
    assert pack.installed and pack.layer == "project"
    assert (tmp_path / "proj" / ".claunch" / "workflows" / "v2" / "packs" / "demo"
            / "solo.yaml").is_file()
    with pytest.raises(procpacks.PackError):
        procpacks.install(ws, "demo")  # already there without force
    pack = procpacks.set_roles(ws, "demo", {"type": "blacklist", "roles": ["reviewer"]})
    assert procpacks.get("demo", ws).roles == {"type": "blacklist", "roles": ["reviewer"]}
    pack = procpacks.set_roles(ws, "demo", None)
    assert procpacks.get("demo", ws).roles is None
    procpacks.remove(ws, "demo")
    assert not procpacks.get("demo", ws).installed  # back to the catalog copy


def test_the_role_filter_judges_unknown_roles_closed_for_a_whitelist():
    white = {"type": "whitelist", "roles": ["unreal"]}
    black = {"type": "blacklist", "roles": ["reviewer"]}
    assert procpacks.admits(white, ["worker", "unreal"])
    assert not procpacks.admits(white, ["worker"]) and not procpacks.admits(white, None)
    assert procpacks.admits(black, None) and not procpacks.admits(black, ["reviewer"])
    assert procpacks.admits(None, None)


def test_the_package_ships_the_unreal_example_pack(monkeypatch, tmp_path):
    monkeypatch.undo()  # the real catalog
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    pack = procpacks.get("unreal", str(tmp_path))
    assert pack is not None and pack.roles == {"type": "whitelist", "roles": ["unreal"]}
    for name in pack.to_dict()["procedures"]:
        chart = procpacks.load(name, str(tmp_path), installed_only=False)
        assert chart.kind == "procedure"
    handoff = procpacks.load("unreal:editor-handoff", str(tmp_path), installed_only=False)
    assert handoff.meta.get("forkable") is True


# -- compile -----------------------------------------------------------------------


def test_forkable_belongs_to_a_procedure_and_fork_calls_to_a_task():
    with pytest.raises(frontend.FrontendError, match="forkable"):
        frontend.compile_text("cflow: 2\nname: w\nforkable: true\nnodes: {t: {prompt: T}}\n"
                              "body: [t]\n")
    chart = frontend.compile_text(textwrap.dedent(MAIN))
    work = next(n for n in chart.nodes.values() if n.label == "work")
    assert work.get("fork_calls") is True
    chart = frontend.compile_text(textwrap.dedent(SOLO))
    assert chart.meta.get("forkable") is True


def test_calls_may_name_a_pack_procedure():
    chart = frontend.compile_text(textwrap.dedent(MAIN.replace("[local]", "[demo:solo]")))
    work = next(n for n in chart.nodes.values() if n.label == "work")
    assert work.get("calls") == ["demo:solo"]


# -- calls ---------------------------------------------------------------------------


def test_an_installed_pack_procedure_is_callable_without_calls(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    out = runtime.call("demo:plain", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0,
                       roles=["worker", "unreal"])
    assert out["called"]["mode"] == "inline"  # plain is not forkable
    rec = _state(tmp_path)["calls"]["c1"]
    assert rec["pack"] == "demo" and rec["by"] == "agent" and rec["mode"] == "inline"
    assert "fork" not in rec


def test_a_catalog_only_pack_is_refused_until_installed(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="catalog only"):
        runtime.call("demo:plain", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0,
                     roles=["unreal"])


def test_the_pack_role_filter_refuses_a_session_without_the_role(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="pack demo admits"):
        runtime.call("demo:plain", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0,
                     roles=["worker"])
    procpacks.set_roles(_cwd(tmp_path), "demo", None)  # the filter edited away
    runtime.call("demo:plain", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0,
                 roles=["worker"])


def test_a_plain_name_still_needs_calls(tmp_path):
    _start(tmp_path)
    with pytest.raises(runtime.FlowError, match="may not call"):
        runtime.call("other", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0, roles=[])


def test_both_ends_agreeing_queue_a_fork_and_leave_the_callee_undriven(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    out = runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                       now=T0, roles=["unreal"])
    assert out["called"]["mode"] == "fork" and "queued" in out["called"]["fork"]
    state = _state(tmp_path)
    rec = state["calls"]["c1"]
    assert rec["fork"]["status"] == "queued" and rec["driver"] == "w1"
    assert runtime.fork_calls_pending(state) == [rec]
    assert _state(tmp_path, "w1~c1")["driver"] == ""
    # the status payload carries it for the run page
    call = _status(tmp_path)["calls"][0]
    assert call["mode"] == "fork" and call["fork"]["status"] == "queued"


def test_mode_inline_overrides_the_agreement(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    out = runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                       now=T0, roles=["unreal"], mode="inline")
    assert out["called"]["mode"] == "inline"
    assert _state(tmp_path, "w1~c1")["driver"] == "w1"
    with pytest.raises(runtime.FlowError, match="call mode"):
        runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                     now=T0, roles=["unreal"], mode="fork")


def test_a_caller_without_fork_calls_runs_a_forkable_callee_inline(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path, MAIN.replace("    fork_calls: true\n", ""))
    out = runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                       now=T0, roles=["unreal"])
    assert out["called"]["mode"] == "inline"


def test_call_forked_hands_the_callee_to_the_copy(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                 now=T0, roles=["unreal"])
    runtime.call_forked("c1", "w1-qf1", cwd=_cwd(tmp_path), scope="w1", now=T0)
    rec = _state(tmp_path)["calls"]["c1"]
    assert rec["fork"]["status"] == "running" and rec["fork"]["session"] == "w1-qf1"
    assert _state(tmp_path, "w1~c1")["driver"] == "w1-qf1"
    with pytest.raises(runtime.FlowError, match="not queued"):
        runtime.call_forked("c1", "w1-qf2", cwd=_cwd(tmp_path), scope="w1", now=T0)


def test_a_fallback_goes_inline_and_is_recorded(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                 now=T0, roles=["unreal"])
    runtime.call_fork_fallback("c1", "no conversation", cwd=_cwd(tmp_path), scope="w1",
                               now=T0)
    state = _state(tmp_path)
    rec = state["calls"]["c1"]
    assert rec["mode"] == "inline" and rec["fork"]["status"] == "fallback"
    assert rec["fallback"]["cause"] == "no conversation"
    assert runtime.fork_calls_pending(state) == []
    assert _state(tmp_path, "w1~c1")["driver"] == "w1"
    events = [e for e in RunStore(_cwd(tmp_path), "w1").journal() if e["event"] == "fallback"]
    assert events and events[-1]["route"] == "inline"
    # the rail -- what the session card and header read
    (sub,) = cflow2_web.rail_subprocs(state)
    assert sub["procedure"] == "demo:solo" and sub["pack"] == "demo"
    assert sub["mode"] == "inline" and sub["fallback"]["cause"] == "no conversation"


def test_the_rail_lists_open_calls_only(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    runtime.call("local", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0, roles=[])
    (sub,) = cflow2_web.rail_subprocs(_state(tmp_path))
    assert sub["procedure"] == "local" and sub["mode"] == "inline" and sub["by"] == "agent"
    inst = _task(_status(tmp_path, "w1~c1"), "t")["instance"]
    runtime.report(inst, "ok", None, cwd=_cwd(tmp_path), scope="w1~c1", now=T0)
    assert cflow2_web.rail_subprocs(_state(tmp_path)) == []


# -- the picker ------------------------------------------------------------------


def test_the_picker_lists_everything_and_says_why_not(tmp_path):
    _start(tmp_path)
    out = runtime.callable_procedures(_cwd(tmp_path), "w1", roles=["worker"])
    assert out["task_label"].endswith("work") and out["fork_calls"] is True
    by = {e["name"]: e for e in out["procedures"]}
    assert by["local"]["available"] and by["local"]["declared"]
    assert not by["demo:solo"]["available"] and "catalog only" in by["demo:solo"]["why"]
    procpacks.install(_cwd(tmp_path), "demo")
    by = {e["name"]: e for e in runtime.callable_procedures(
        _cwd(tmp_path), "w1", roles=["worker"])["procedures"]}
    assert not by["demo:solo"]["available"] and "pack demo admits" in by["demo:solo"]["why"]
    by = {e["name"]: e for e in runtime.callable_procedures(
        _cwd(tmp_path), "w1", roles=["unreal"])["procedures"]}
    assert by["demo:solo"]["available"] and by["demo:solo"]["mode"] == "fork"
    assert by["demo:plain"]["mode"] == "inline"


def test_the_picker_disables_everything_while_a_call_is_open(tmp_path):
    _start(tmp_path)
    runtime.call("local", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0, roles=[])
    out = runtime.callable_procedures(_cwd(tmp_path), "w1", roles=[])
    assert out["refusal"] == "a call is already open on the active task"
    assert not any(e["available"] for e in out["procedures"])


def test_a_user_call_is_marked_as_such(tmp_path):
    _start(tmp_path)
    runtime.call("local", "work", None, cwd=_cwd(tmp_path), scope="w1", now=T0, roles=[],
                 by="user")
    assert _state(tmp_path)["calls"]["c1"]["by"] == "user"


# -- the clock -----------------------------------------------------------------------


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


def _clock(tmp_path, monkeypatch, *, harness_ok=True):
    mgr = FakeManager(FakeSession("w1", _cwd(tmp_path)))
    forks, events = [], []

    def fork(parent, body):
        name = f"{parent}-qf{len(forks) + 1}"
        forks.append((parent, body))
        mgr.sessions[name] = FakeSession(name, _cwd(tmp_path), quick_fork_of=parent)
        return name

    monkeypatch.setattr(cflow2_exec, "harness_fork_refusal",
                        lambda s: "" if harness_ok else "the driver's harness (x) cannot")
    from claude_launcher.daemon import spawn_events

    monkeypatch.setattr(spawn_events, "report",
                        lambda target, **kw: events.append((target, kw)))
    clock = cflow2_clock.Cflow2Clock(
        mgr, poll=5, fork=fork, context_left=lambda s: (500_000, "stub"),
        probe_runner=None, threaded=False, fork_min_context=100_000)
    return clock, mgr, forks, events


def _fork_call(tmp_path):
    procpacks.install(_cwd(tmp_path), "demo")
    _start(tmp_path)
    runtime.call("demo:solo", "work", {"what": "x"}, cwd=_cwd(tmp_path), scope="w1",
                 now=T0, roles=["unreal"])


def test_the_clock_quick_forks_the_driver_for_a_fork_call(tmp_path, monkeypatch):
    clock, mgr, forks, events = _clock(tmp_path, monkeypatch)
    clock.scan()
    _fork_call(tmp_path)
    clock.scan()
    assert [p for p, _ in forks] == ["w1"]
    body = forks[0][1]
    assert body["self_merge_allowed"] is True
    rec = _state(tmp_path)["calls"]["c1"]
    assert repr(rec["nested"]["scope"]) in body["task"] and "Do not commit" in body["task"]
    assert rec["fork"] == {**rec["fork"], "status": "running", "session": "w1-qf1"}
    assert _state(tmp_path, "w1~c1")["driver"] == "w1-qf1"
    assert events == []
    # the copy drives the callee to its end; the result is back on the task
    inst = _task(_status(tmp_path, "w1~c1"), "step")["instance"]
    runtime.report(inst, "ok", {"done": "y"}, cwd=_cwd(tmp_path), scope="w1~c1", now=T0)
    back = _task(_status(tmp_path), "work")
    assert back["call_result"]["value"] == {"done": "y"}


def test_a_harness_that_cannot_fork_falls_back_and_says_so_twice(tmp_path, monkeypatch):
    clock, mgr, forks, events = _clock(tmp_path, monkeypatch, harness_ok=False)
    clock.scan()
    _fork_call(tmp_path)
    notices = clock.scan()
    assert forks == []
    rec = _state(tmp_path)["calls"]["c1"]
    assert rec["mode"] == "inline" and "harness" in rec["fallback"]["cause"]
    assert _state(tmp_path, "w1~c1")["driver"] == "w1"
    # the notification path
    assert [(t, kw["kind"], kw["site"]) for t, kw in events] == [
        ("w1", "fallback", "cflow2 sub procedure fork")]
    # the terminal path
    typed = "\n".join(text for _, who, text in notices if who == "w1")
    assert "fell back to inline" in typed and rec["nested"]["scope"] in typed


def test_a_fork_that_ends_before_the_callee_falls_back(tmp_path, monkeypatch):
    clock, mgr, forks, events = _clock(tmp_path, monkeypatch)
    clock.scan()
    _fork_call(tmp_path)
    clock.scan()
    mgr.get("w1-qf1").exited = True
    clock.scan()
    rec = _state(tmp_path)["calls"]["c1"]
    assert rec["fork"]["status"] == "fallback" and rec["fallback"]["fork_session"] == "w1-qf1"
    assert _state(tmp_path, "w1~c1")["driver"] == "w1"
    assert len(events) == 1


# -- the daemon's routes -------------------------------------------------------------


def _http(fn):
    import asyncio
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from claude_launcher.daemon import api as api_mod
    from claude_launcher.daemon.manager import SessionManager

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=50, restore_default=True)
        app = api_mod.build_app(mgr, "sekrit", started_at=time.monotonic())
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client, {"Authorization": "Bearer sekrit"})
        finally:
            await client.close()

    return asyncio.run(run())


def test_the_routes_list_call_and_edit_packs(tmp_path):
    cwd = _cwd(tmp_path)
    _start(tmp_path)

    async def go(client, bearer):
        packs = await (await client.get(f"/api/cflow2/packs?cwd={cwd}", headers=bearer)).json()
        assert [(p["id"], p["installed"]) for p in packs["packs"]] == [("demo", False)]
        resp = await client.post("/api/cflow2/packs/install", json={"cwd": cwd, "pack": "demo"},
                                 headers=bearer)
        assert resp.status == 200 and (await resp.json())["packs"][0]["installed"]
        # the session's roles are unknown here: the whitelist refuses, and says so
        listed = await (await client.get(f"/api/cflow2/procedures?cwd={cwd}&scope=w1",
                                         headers=bearer)).json()
        by = {e["name"]: e for e in listed["procedures"]}
        assert by["local"]["available"] and not by["demo:plain"]["available"]
        assert "pack demo admits" in by["demo:plain"]["why"]
        resp = await client.post("/api/cflow2/call", headers=bearer, json={
            "cwd": cwd, "scope": "w1", "procedure": "demo:plain"})
        assert resp.status == 400 and "pack demo admits" in (await resp.json())["error"]
        # the filter edited away from the dashboard, the same call goes through
        resp = await client.post("/api/cflow2/packs/roles", headers=bearer,
                                 json={"cwd": cwd, "pack": "demo", "roles": None})
        assert resp.status == 200
        resp = await client.post("/api/cflow2/packs/roles", headers=bearer,
                                 json={"cwd": cwd, "pack": "demo", "roles": {"type": "odd"}})
        assert resp.status == 400
        resp = await client.post("/api/cflow2/call", headers=bearer, json={
            "cwd": cwd, "scope": "w1", "procedure": "demo:plain", "mode": "inline"})
        assert resp.status == 200
        out = await resp.json()
        assert out["called"]["call"] == "c1" and "nudge_scheduled_sessions" in out
        assert out["calls"][0]["by"] == "user"
        assert (await client.post("/api/cflow2/call", json={
            "cwd": cwd, "scope": "w1", "procedure": "local"})).status == 401

    _http(go)


def test_a_session_call_is_recorded_as_the_agents(tmp_path):
    """claunch-8t5r0.7: the call names who asked -- the actor header a
    managed session's client sends makes it ``agent``; a bearer without it
    (a person's shell) stays ``user``."""
    from claude_launcher.daemon import actors

    cwd = _cwd(tmp_path)
    _start(tmp_path)

    async def go(client, bearer):
        agent = {**bearer, actors.HEADER: actors.header_value(actors.AGENT, "w1")}
        resp = await client.post("/api/cflow2/call", headers=agent, json={
            "cwd": cwd, "scope": "w1", "procedure": "local", "mode": "inline"})
        assert resp.status == 200, await resp.text()
        out = await resp.json()
        assert out["calls"][0]["by"] == "agent"

    _http(go)


def test_a_session_may_not_install_remove_or_refilter_a_pack(tmp_path):
    """claunch-8t5r0.7, user decision (person only): a pack's roles filter
    decides which sessions may call it, so a session -- an agent's shell or
    a cflow run -- is refused all three writes and nothing changes; the same
    request without the actor header goes through."""
    from claude_launcher.daemon import actors

    cwd = _cwd(tmp_path)

    async def go(client, bearer):
        for kind in (actors.AGENT, actors.CFLOW):
            session = {**bearer, actors.HEADER: actors.header_value(kind, "w1")}
            for path, extra in (("install", {}), ("remove", {}), ("roles", {"roles": None})):
                resp = await client.post(f"/api/cflow2/packs/{path}", headers=session,
                                         json={"cwd": cwd, "pack": "demo", **extra})
                assert resp.status == 403, (kind, path)
                assert "by a person" in (await resp.json())["error"]
        packs = await (await client.get(f"/api/cflow2/packs?cwd={cwd}", headers=bearer)).json()
        assert [(p["id"], p["installed"]) for p in packs["packs"]] == [("demo", False)]
        resp = await client.post("/api/cflow2/packs/install", headers=bearer,
                                 json={"cwd": cwd, "pack": "demo"})
        assert resp.status == 200 and (await resp.json())["packs"][0]["installed"]
        # an installed pack's filter is still a person's to edit
        session = {**bearer, actors.HEADER: actors.header_value(actors.AGENT, "w1")}
        resp = await client.post("/api/cflow2/packs/roles", headers=session,
                                 json={"cwd": cwd, "pack": "demo", "roles": None})
        assert resp.status == 403

    _http(go)
