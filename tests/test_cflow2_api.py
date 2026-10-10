"""cflow v2 on the daemon's surfaces (docs/cflow-v2.md sections 9-10).

The workflow list carries both formats told apart by ``version`` (v1 marked
deprecated, v2 procedures left out); the spawn path starts a v2 workflow on
the cflow2 runtime when the name is v2-only, when the request says
``workflow_version: 2``, or when the path is a ``.cflow2/generated`` file;
the run page reads ``/api/cflow2/run``; the person answers a user choice and
a loop limit through ``/api/cflow2/select`` and ``/api/cflow2/extend``; a
v2 parent pairs its child through its chart's ``default_child_cflow``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from claude_launcher import cli_cflow, wizard
from claude_launcher.cflow import state as cflow_state
from claude_launcher.cflow2 import layers, runtime
from claude_launcher.cflow2.store import RunStore
from claude_launcher.daemon import api as api_mod, cflow2_web, onboard
from claude_launcher.daemon.manager import SessionManager

V1_DEMO = (
    "name: demo\ndescription: the v1 one\nsteps:\n"
    "  one:\n    instructions: do one\n"
)

V2_DEMO = """\
cflow: 2
name: demo
description: the v2 one with the same name
nodes:
  only: {title: Only, prompt: do it}
body: only
"""

PICK = """\
cflow: 2
name: pick
description: a user choice, then a bounded loop
default_child_cflow: kid
nodes:
  a: {title: Step A, prompt: do a}
  b: {title: Step B, prompt: do b}
  c: {title: Step C, prompt: do c, executor: [subagent, inline]}
body:
  seq:
    - choose:
        prompt: which way?
        chooser: user
        options:
          left: {then: a}
          right: {then: b}
    - loop: {while: "true", max: 1, do: c}
"""

KID = """\
cflow: 2
name: kid
nodes:
  k: {title: Kid, prompt: do the kid's work}
body: k
"""

HELPER = """\
cflow: 2
name: helper
kind: procedure
nodes:
  x: {title: X, prompt: x}
body: x
"""

PAIRS_DEMO = PICK.replace("name: pick", "name: pairs-demo").replace(
    "default_child_cflow: kid", "default_child_cflow: demo")


@pytest.fixture
def proj(home, tmp_path, monkeypatch):
    d = tmp_path / "proj"
    v1 = d / ".claunch" / "workflows"
    v2 = v1 / "v2"
    v2.mkdir(parents=True)
    (v1 / "demo.yaml").write_text(V1_DEMO, encoding="utf-8")
    for name, text in (("demo", V2_DEMO), ("pick", PICK), ("kid", KID),
                       ("helper", HELPER), ("pairs-demo", PAIRS_DEMO)):
        (v2 / f"{name}.yaml").write_text(text, encoding="utf-8")
    # Only this test's layers: the package's v2 definitions are another
    # session's work in progress, and their compile state is not this test's.
    monkeypatch.setattr(layers, "package_dir", lambda: tmp_path / "no-package-v2")
    monkeypatch.setattr(cflow2_web, "_LISTS", {})
    monkeypatch.setattr(cflow2_web, "_DEFS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_LISTS", {})
    monkeypatch.setattr(api_mod, "_WORKFLOW_BASES", {})
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.chdir(d)
    return d


def _http(fn):
    """Run ``fn(client, bearer)`` against a real app with no sessions."""
    from aiohttp.test_utils import TestClient, TestServer

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


def _preflight(body, cwd, **kw):
    return onboard.preflight(body, mesh_mgr=None, session_name="w1", cwd=cwd,
                             harness="claude", **kw)


# --------------------------------------------------------------------------- #
# the list: both versions, version/deprecated on every entry, no procedures
# --------------------------------------------------------------------------- #
def test_the_workflow_list_carries_both_versions(proj):
    async def go(client, bearer):
        resp = await client.get(f"/api/cflow/workflows?cwd={proj}", headers=bearer)
        assert resp.status == 200
        return (await resp.json())["workflows"]

    flows = _http(go)
    v1 = [w for w in flows if w["version"] == 1]
    v2 = {w["name"]: w for w in flows if w["version"] == 2}
    assert all(w["deprecated"] is True for w in v1)
    assert all(w["deprecated_note"] == cflow2_web.DEPRECATED_NOTE for w in v1)
    demo1 = next(w for w in v1 if w["name"] == "demo")
    # The v1 fields are unchanged beside the new ones.
    for key in ("name", "path", "origin", "shadowed", "description", "steps", "recur",
                "default_role", "default_child_cflow", "priority", "filter_roles"):
        assert key in demo1
    # Both formats' "demo" are listed; the procedure is not startable.
    assert set(v2) == {"demo", "pick", "kid", "pairs-demo"}
    pick = v2["pick"]
    assert pick["deprecated"] is False
    assert pick["origin"] == layers.LAYER_PROJECT
    assert pick["kind"] == "workflow"
    assert pick["steps"] == 3
    assert pick["delegations"] == 1
    assert pick["default_child_cflow"] == "kid"
    for key in ("description", "default_role", "priority", "filter_roles", "path"):
        assert key in pick
    # v1 first: a picker taking the first match by name keeps v1.
    names = [(w["name"], w["version"]) for w in flows]
    assert names.index(("demo", 1)) < names.index(("demo", 2))


def test_the_v2_list_is_cached_and_follows_an_edit(proj):
    first = cflow2_web.startable(str(proj))
    assert cflow2_web.startable(str(proj)) is first
    (proj / ".claunch" / "workflows" / "v2" / "kid.yaml").write_text(
        KID.replace("name: kid", "name: kid\ndescription: edited later"), encoding="utf-8")
    after = {w["name"]: w for w in cflow2_web.startable(str(proj))}
    assert after["kid"]["description"] == "edited later"


def test_a_broken_v2_file_is_listed_with_its_error(proj):
    (proj / ".claunch" / "workflows" / "v2" / "bad.yaml").write_text(
        "cflow: 2\nname: bad\nbody: [nope\n", encoding="utf-8")
    bad = next(w for w in cflow2_web.startable(str(proj)) if w["name"] == "bad")
    assert bad["version"] == 2 and bad["error"]


# --------------------------------------------------------------------------- #
# which runtime a request means
# --------------------------------------------------------------------------- #
def test_a_shared_name_stays_v1_unless_v2_is_asked_for(proj):
    cwd = str(proj)
    assert cflow2_web.choose("demo", cwd=cwd) is None
    assert cflow2_web.choose("demo", 1, cwd=cwd) is None
    ref, chart = cflow2_web.choose("demo", 2, cwd=cwd)
    assert (ref, chart.name, chart.description) == ("demo", "demo",
                                                    "the v2 one with the same name")
    assert cflow2_web.choose("v2:demo", cwd=cwd)[0] == "demo"
    # A v2-only name needs neither.
    assert cflow2_web.choose("pick", cwd=cwd)[0] == "pick"
    with pytest.raises(cflow2_web.ChoiceError, match="no v2 workflow"):
        cflow2_web.choose("ghost", 2, cwd=cwd)
    with pytest.raises(cflow2_web.ChoiceError, match="workflow_version"):
        cflow2_web.choose("demo", 3, cwd=cwd)


# --------------------------------------------------------------------------- #
# the spawn path: preflight + arrange start a v2 run scoped to the session
# --------------------------------------------------------------------------- #
def test_preflight_and_arrange_start_a_v2_workflow_by_name(proj):
    plan = _preflight({"workflow": "pick", "task": "go"}, str(proj))
    assert (plan.workflow, plan.workflow_version) == ("pick", 2)
    assert "cflow v2 run of workflow 'pick'" in plan.identity
    report, opening = asyncio.run(onboard.arrange(plan, name="w1", cwd=str(proj),
                                                  mesh_mgr=None))
    assert report["workflow"]["ok"] is True and report["workflow"]["version"] == 2
    assert "flow_status" in opening and "cflow v2" in opening
    assert RunStore(str(proj), "w1").exists()
    # nothing on the v1 side
    assert not (cflow_state.cflow_dir(str(proj)) / "runs" / "w1" / "state.json").exists()
    # unwind undoes the v2 run like the v1 one
    asyncio.run(onboard.unwind(report, name="w1", cwd=str(proj), mesh_mgr=None))
    assert runtime.status(cwd=str(proj), scope="w1", tick=False)["status"] == "aborted"


def test_preflight_keeps_v1_on_a_shared_name_and_takes_v2_when_asked(proj):
    assert _preflight({"workflow": "demo"}, str(proj)).workflow_version == 1
    assert _preflight({"workflow": "demo", "workflow_version": 2},
                      str(proj)).workflow_version == 2
    # A procedure is not startable: it is not among the v2 workflows.
    with pytest.raises(onboard.OnboardError, match="no v2 workflow named 'helper'"):
        _preflight({"workflow": "helper", "workflow_version": 2}, str(proj))


def test_preflight_admits_a_generated_v2_file_and_refuses_one_elsewhere(proj, tmp_path):
    gen = proj / ".cflow2" / "generated"
    gen.mkdir(parents=True)
    (gen / "child-1.yaml").write_text(KID.replace("name: kid", "name: child-1"),
                                      encoding="utf-8")
    plan = _preflight({"workflow": ".cflow2/generated/child-1.yaml"}, str(proj))
    assert plan.workflow_version == 2
    assert plan.workflow == str((gen / "child-1.yaml").resolve())

    # A v2 file outside the generated directory is refused -- by name a spawn
    # may reach the layers, by path only the generated directory.
    elsewhere = proj / ".claunch" / "workflows" / "v2" / "kid.yaml"
    with pytest.raises(onboard.OnboardError, match="outside the generated"):
        _preflight({"workflow": str(elsewhere)}, str(proj))
    with pytest.raises(onboard.OnboardError, match="outside the generated"):
        _preflight({"workflow": "../escape.yaml", "workflow_version": 2}, str(proj))

    # A `cflow: 2` file in v1's generated directory is v2 too.
    v1gen = cflow_state.generated_workflows_dir(str(proj))
    v1gen.mkdir(parents=True)
    (v1gen / "child-2.yaml").write_text(KID.replace("name: kid", "name: child-2"),
                                        encoding="utf-8")
    assert _preflight({"workflow": ".cflow/generated/child-2.yaml"},
                      str(proj)).workflow_version == 2

    # The spawning session's directory is the second root, as for v1.
    child = tmp_path / "wt"
    child.mkdir()
    plan = _preflight({"workflow": str(gen / "child-1.yaml")}, str(child),
                      parent="pm1", parent_cwd=str(proj))
    assert plan.workflow_version == 2


def test_preflight_checks_v2_params_before_anything_exists(proj):
    needs = PICK.replace("name: pick", "name: needs").replace(
        "nodes:", "params:\n  goal: {type: str, required: true}\nnodes:")
    (proj / ".claunch" / "workflows" / "v2" / "needs.yaml").write_text(needs, encoding="utf-8")
    with pytest.raises(onboard.OnboardError, match="goal"):
        _preflight({"workflow": "needs"}, str(proj))
    plan = _preflight({"workflow": "needs", "workflow_params": {"goal": "x"}}, str(proj))
    assert plan.workflow_params == {"goal": "x"}


# --------------------------------------------------------------------------- #
# pairing: a v2 parent's chart names its child's workflow
# --------------------------------------------------------------------------- #
def test_a_v2_parent_pairs_its_child_from_its_chart(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="pm", caps={})
    assert onboard.paired_child_workflow(parent="pm", parent_cwd=cwd, cwd=cwd) == "kid"
    body = {}
    onboard.inherit_workflow(body, parent="pm", parent_cwd=cwd, cwd=cwd)
    assert body == {"workflow": "kid", "workflow_version": 2}

    # A pair both formats declare is the v2 one when a v2 parent named it.
    runtime.start(layers.load("pairs-demo", cwd), cwd=cwd, scope="pm2", caps={})
    body = {}
    onboard.inherit_workflow(body, parent="pm2", parent_cwd=cwd, cwd=cwd)
    assert body == {"workflow": "demo", "workflow_version": 2}

    # No run, no pair.
    assert onboard.paired_child_workflow(parent="nobody", parent_cwd=cwd, cwd=cwd) == ""


# --------------------------------------------------------------------------- #
# the run page and the person's answers
# --------------------------------------------------------------------------- #
def test_run_detail_and_the_users_answers(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s1", caps={})

    async def go(client, bearer):
        q = f"cwd={cwd}&scope=s1"
        out = {}
        out["v1"] = await (await client.get(f"/api/cflow/run?{q}", headers=bearer)).json()
        out["unauth"] = (await client.get(f"/api/cflow2/run?{q}")).status
        out["detail"] = await (await client.get(f"/api/cflow2/run?{q}", headers=bearer)).json()
        out["prompts"] = await (await client.get(
            f"/api/cflow2/run?{q}&prompts=1", headers=bearer)).json()
        out["runs"] = await (await client.get(
            f"/api/cflow2/runs?cwd={cwd}", headers=bearer)).json()
        body = {"cwd": cwd, "scope": "s1", "option": "left"}
        out["select_unauth"] = (await client.post("/api/cflow2/select", json=body)).status
        resp = await client.post("/api/cflow2/select", json=body, headers=bearer)
        out["select"] = (resp.status, await resp.json())
        task = runtime.status(cwd=cwd, scope="s1")["tasks"][0]["instance"]
        runtime.report(task, cwd=cwd, scope="s1")
        # the loop's single iteration runs c (delegated: subagent, else inline)
        st = runtime.status(cwd=cwd, scope="s1")
        if st.get("delegations"):
            open_ = [d for d in st["delegations"] if d["status"] == "pending"]
            runtime.delegate_submit(open_[0]["id"], "ok", None, cwd=cwd, scope="s1")
        else:
            runtime.report(st["tasks"][0]["instance"], cwd=cwd, scope="s1")
        out["held"] = await (await client.get(f"/api/cflow2/run?{q}", headers=bearer)).json()
        resp = await client.post("/api/cflow2/extend", headers=bearer,
                                 json={"cwd": cwd, "scope": "s1", "stop": True})
        out["extend"] = (resp.status, await resp.json())
        resp = await client.post("/api/cflow2/select", headers=bearer,
                                 json={"cwd": cwd, "scope": "s1", "option": "left"})
        out["late"] = (resp.status, await resp.json())
        return out

    out = _http(go)
    assert out["v1"]["status"] == "idle" and out["v1"]["v2"] is True
    assert out["unauth"] == 401 and out["select_unauth"] == 401

    d = out["detail"]
    assert set(d) >= {"run", "chart", "journal", "delegations", "instances", "visits"}
    assert d["run"]["workflow"] == "pick" and d["status"] == "running"
    chart = d["chart"]
    root = chart["nodes"][chart["root"]]
    assert root["type"] == "seq" and len(root["children"]) == 2
    for node in chart["nodes"].values():
        assert {"id", "type", "children"} <= set(node)
        assert "prompt" not in node
    assert any(n.get("prompt") for n in out["prompts"]["chart"]["nodes"].values())
    choose = next(n for n in chart["nodes"].values() if n["type"] == "choose")
    assert choose["chooser"] == "user" and choose["options"] == ["left", "right"]
    assert any(i["await"] == "choose" for i in d["instances"])
    assert d["run"]["choices"][0]["chooser"] == "user"
    assert d["journal"][0]["event"] == "started"

    runs = out["runs"]["runs"]
    assert [(r["scope"], r["workflow"], r["version"]) for r in runs] == [("s1", "pick", 2)]
    assert runs[0]["current"]  # the open choice, by its node id (it has no label)

    status, payload = out["select"]
    assert status == 200 and payload["status"] == "running"
    assert payload["nudge_scheduled_sessions"] == []

    held = out["held"]
    assert held["status"] == "waiting_approval"
    assert held["run"]["loop_limits"][0]["max"] == 1
    status, payload = out["extend"]
    assert status == 200 and payload["status"] in ("done", "failed")
    status, payload = out["late"]
    assert status == 400 and "error" in payload


def test_the_agent_still_cannot_take_the_users_choice(proj):
    cwd = str(proj)
    runtime.start(layers.load("pick", cwd), cwd=cwd, scope="s2", caps={})
    with pytest.raises(runtime.FlowError, match="user"):
        runtime.select(None, "left", cwd=cwd, scope="s2")


def test_definition_view_and_the_editor_list(proj):
    cwd = str(proj)

    async def go(client, bearer):
        ok = await client.get(f"/api/cflow2/definition?cwd={cwd}&name=pick", headers=bearer)
        missing = await client.get(f"/api/cflow2/definition?cwd={cwd}&name=ghost",
                                   headers=bearer)
        defs = await client.get(f"/api/cflow/definitions?cwd={cwd}", headers=bearer)
        return (ok.status, await ok.json()), missing.status, await defs.json()

    (status, doc), missing, defs = _http(go)
    assert status == 200
    assert doc["chart"]["name"] == "pick" and doc["text"].startswith("cflow: 2")
    assert isinstance(doc["findings"], list) and doc["delegations"]
    assert missing == 404
    assert {d["name"] for d in defs["definitions_v2"]} >= {"pick", "helper"}
    assert defs["deprecated_note"] == cflow2_web.DEPRECATED_NOTE


def test_the_dashboard_start_runs_a_v2_workflow(proj):
    cwd = str(proj)

    async def go(client, bearer):
        resp = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "s3", "workflow": "demo", "workflow_version": 2})
        first = (resp.status, await resp.json())
        resp = await client.post("/api/cflow/request", headers=bearer, json={
            "cwd": cwd, "scope": "s4", "workflow": "kid"})
        second = (resp.status, await resp.json())
        resp = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "s3", "workflow": "demo", "workflow_version": 2})
        third = (resp.status, await resp.json())
        return first, second, third

    first, second, third = _http(go)
    assert first[0] == 200 and first[1]["version"] == 2 and first[1]["workflow"] == "demo"
    assert second[0] == 200 and second[1]["workflow"] == "kid"
    assert third[0] == 400 and "still" in third[1]["error"]
    # The v1 "demo" was not started anywhere.
    assert not (cflow_state.cflow_dir(cwd) / "runs" / "s3" / "state.json").exists()


# --------------------------------------------------------------------------- #
# CLI and wizard
# --------------------------------------------------------------------------- #
def test_cflow_ls_and_show_say_v1_is_deprecated(proj, capsys):
    assert cli_cflow._cmd_ls(SimpleNamespace()) == 0
    err = capsys.readouterr().err
    assert "deprecated" in err and "claunch cflow2" in err
    assert cli_cflow._cmd_show(SimpleNamespace(workflow="demo")) == 0
    assert "deprecated" in capsys.readouterr().err


def test_the_wizard_marks_versions_and_keeps_v1_as_the_auto_pick():
    raw = [
        {"name": "improv-worker", "default_role": "worker", "version": 1, "deprecated": True},
        {"name": "improv-worker", "default_role": "worker", "version": 2},
        {"name": "solo-v2", "version": 2},
        {"name": "old"},  # an older daemon: no version at all
    ]
    entries = [wizard._workflow_entry(w) for w in raw]
    opts = wizard._workflow_options(entries, "worker")
    got = [(o.label, o.value) for o in opts]
    assert ("improv-worker [v1, deprecated]", "improv-worker") in got
    assert ("improv-worker [v2]", "v2:improv-worker") in got
    assert ("solo-v2 [v2]", "solo-v2") in got
    assert ("old", "old") in got
    # v1 ranks before v2 inside the role's band, and is the auto pick.
    assert got.index(("improv-worker [v1, deprecated]", "improv-worker")) < \
        got.index(("improv-worker [v2]", "v2:improv-worker"))
    assert wizard._workflow_default(entries, "worker") == "improv-worker"


# --------------------------------------------------------------------------- #
# approvals: a task's ask answered from the dashboard
# --------------------------------------------------------------------------- #
GATED = """\
cflow: 2
name: gated
nodes:
  ship:
    title: ship it
    prompt: ship
    ask: {prompt: "ship now?"}
body: [ship]
"""


def test_the_user_answers_an_ask_from_the_dashboard(proj):
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "v2" / "gated.yaml").write_text(GATED, encoding="utf-8")
    runtime.start(layers.load("gated", cwd), cwd=cwd, scope="g1", caps={})

    async def go(client, bearer):
        q = f"cwd={cwd}&scope=g1"
        out = {"asks": await (await client.get(f"/api/cflow2/asks?{q}", headers=bearer)).json()}
        body = {"cwd": cwd, "scope": "g1", "decision": "approve"}
        out["unauth"] = (await client.post("/api/cflow2/approve", json=body)).status
        resp = await client.post("/api/cflow2/approve", json=body, headers=bearer)
        out["approve"] = (resp.status, await resp.json())
        resp = await client.post("/api/cflow2/approve", json=body, headers=bearer)
        out["again"] = resp.status
        return out

    out = _http(go)
    assert out["unauth"] == 401
    assert out["approve"][0] == 200
    st = runtime.status(cwd=cwd, scope="g1")
    assert not st.get("asks") and st["tasks"][0]["label"] == "ship"
    assert out["again"] == 400   # nothing left to approve


# --------------------------------------------------------------------------- #
# typed start inputs (docs/cflow-v2.md 2.6), the definition by name only,
# a v2 problem that must not refuse a v1 spawn, the visit-limit answer
# --------------------------------------------------------------------------- #
INPUTS_DEF = """\
cflow: 2
name: asks-inputs
inputs:
  goal: {type: text, required: true, label: Goal}
  rounds: {type: int}
  invariants: {type: invariants}
nodes:
  w: {title: W, prompt: "goal ${data.inputs.goal}"}
body: w
"""


@pytest.fixture
def with_inputs(proj):
    (proj / ".claunch" / "workflows" / "v2" / "asks-inputs.yaml").write_text(
        INPUTS_DEF, encoding="utf-8")
    return proj


def test_the_list_and_definition_carry_the_inputs_schema(with_inputs):
    cwd = str(with_inputs)
    entry = next(w for w in cflow2_web.startable(cwd) if w["name"] == "asks-inputs")
    assert [f["name"] for f in entry["inputs"]] == ["goal", "rounds", "invariants"]
    assert entry["inputs"][0]["required"] is True and entry["inputs"][0]["label"] == "Goal"
    doc = cflow2_web.definition_view("asks-inputs", cwd)
    assert doc["inputs"] == entry["inputs"]
    assert cflow2_web.definition_view("pick", cwd)["inputs"] == []


def test_preflight_validates_cflow_inputs_per_field(with_inputs):
    cwd = str(with_inputs)
    with pytest.raises(onboard.OnboardError) as info:
        _preflight({"workflow": "asks-inputs", "cflow_inputs": {"rounds": "x", "nope": 1}}, cwd)
    assert set(info.value.errors) == {"goal", "rounds", "nope"}
    assert onboard.error_body(info.value)["errors"] == info.value.errors
    with pytest.raises(onboard.OnboardError, match="cflow_inputs"):
        _preflight({"workflow": "demo", "cflow_inputs": {"goal": "g"}}, cwd)  # v1
    plan = _preflight({"workflow": "asks-inputs", "cflow_inputs": {
        "goal": "ship it", "rounds": "2",
        "invariants": [{"text": "master unmoved", "probe": "git diff --quiet master"}]}}, cwd)
    assert plan.cflow_inputs["rounds"] == 2
    report, _ = asyncio.run(onboard.arrange(plan, name="w9", cwd=cwd, mesh_mgr=None))
    assert report["workflow"]["ok"] is True, report
    st = RunStore(cwd, "w9")
    assert st.load_state()["data"]["inputs"]["goal"] == "ship it"
    chart = st.load_chart()
    assert "input-invariant-1" in chart.rules
    assert chart.spec["monitors"]["input-invariant-1"]["probe"] == {
        "input_invariant_1": "git diff --quiet master"}


def test_the_dashboard_start_returns_field_errors(with_inputs):
    cwd = str(with_inputs)

    async def go(client, bearer):
        bad = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "i1", "workflow": "asks-inputs",
            "cflow_inputs": {"rounds": "many"}})
        good = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "i1", "workflow": "asks-inputs",
            "cflow_inputs": {"goal": "g"}})
        return (bad.status, await bad.json()), good.status

    (status, doc), good = _http(go)
    assert status == 400 and set(doc["errors"]) == {"goal", "rounds"}
    assert good == 200
    assert RunStore(cwd, "i1").load_state()["data"]["inputs"] == {
        "goal": "g", "rounds": None, "invariants": []}


def test_inputs_check_answers_constraints_without_starting(proj):
    (proj / ".claunch" / "workflows" / "v2" / "pair.yaml").write_text(
        "cflow: 2\nname: pair\ninputs: {a: {type: bool}, b: {type: bool}}\n"
        "input_constraints:\n  - {assert: 'not ($inputs.a and $inputs.b)', message: not both}\n"
        "nodes: {w: {prompt: w}}\nbody: w\n", encoding="utf-8")
    cwd = str(proj)
    entry = next(w for w in cflow2_web.startable(cwd) if w["name"] == "pair")
    assert entry["input_constraints"][0]["fields"] == ["a", "b"]

    async def go(client, bearer):
        out = []
        for raw in ({"a": True, "b": True}, {"a": True}):
            r = await client.post("/api/cflow2/inputs/check", headers=bearer, json={
                "cwd": cwd, "workflow": "pair", "workflow_version": 2, "cflow_inputs": raw})
            out.append((r.status, await r.json()))
        r = await client.post("/api/cflow2/inputs/check", headers=bearer,
                              json={"cwd": cwd, "workflow": "no-such", "workflow_version": 2})
        out.append((r.status, None))
        r = await client.post("/api/cflow/start", headers=bearer, json={
            "cwd": cwd, "scope": "p1", "workflow": "pair", "cflow_inputs": {"a": True, "b": True}})
        out.append((r.status, await r.json()))
        return out

    (s1, bad), (s2, good), (s3, _), (s4, start) = _http(go)
    assert s1 == 200 and bad["ok"] is False and bad["errors"] == {"a": "not both", "b": "not both"}
    assert s2 == 200 and good["ok"] is True and good["values"] == {"a": True, "b": False}
    assert s3 == 404
    assert s4 == 400 and set(start["errors"]) == {"a", "b"}


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "pick.yaml", "a/b"])
def test_the_definition_endpoint_takes_a_name_only(proj, name):
    from urllib.parse import quote

    async def go(client, bearer):
        resp = await client.get(f"/api/cflow2/definition?cwd={proj}&name={quote(name)}",
                                headers=bearer)
        return resp.status, await resp.json()

    status, doc = _http(go)
    assert status == 400 and "not a path" in doc["error"]
    with pytest.raises(cflow2_web.ChoiceError, match="not a definition name"):
        cflow2_web.definition_view(name, str(proj))


def test_a_broken_v2_layer_does_not_refuse_a_v1_spawn(proj, monkeypatch, caplog):
    def boom(cwd):
        raise RuntimeError("v2 layer exploded")

    monkeypatch.setattr(cflow2_web, "v2_names", boom)
    with caplog.at_level("WARNING"):
        assert cflow2_web.choose("demo", cwd=str(proj)) is None
    assert "treating it as v1" in caplog.text
    plan = _preflight({"workflow": "demo"}, str(proj))
    assert plan.workflow_version == 1
    # asked for v2 explicitly, the problem is still the answer
    with pytest.raises(RuntimeError):
        cflow2_web.choose("demo", 2, cwd=str(proj))


def test_definitions_are_cached_by_layer_stamp(proj, monkeypatch):
    calls = []
    real = layers.list_definitions
    monkeypatch.setattr(layers, "list_definitions",
                        lambda cwd=None: calls.append(cwd) or real(cwd))
    first = cflow2_web.definitions(str(proj))
    assert cflow2_web.definitions(str(proj)) == first and len(calls) == 1
    (proj / ".claunch" / "workflows" / "v2" / "new.yaml").write_text(KID.replace(
        "name: kid", "name: new"), encoding="utf-8")
    assert "new" in {d["name"] for d in cflow2_web.definitions(str(proj))}
    assert len(calls) == 2


def test_extend_with_visits_answers_a_visit_limit(proj, monkeypatch):
    seen = {}

    def fake(node, by, *, stop=False, cwd=None, scope=None):
        seen.update(node=node, by=by, stop=stop, scope=scope)
        return {"status": "running"}

    monkeypatch.setattr(runtime, "extend_visits", fake)
    monkeypatch.setattr(runtime, "extend_loop",
                        lambda *a, **k: pytest.fail("loop answer used for a visit limit"))

    async def go(client, bearer):
        resp = await client.post("/api/cflow2/extend", headers=bearer, json={
            "cwd": str(proj), "scope": "v1s", "node": "x@3", "by": 2, "visits": True})
        return resp.status

    assert _http(go) == 200
    assert seen == {"node": "x@3", "by": 2, "stop": False, "scope": "v1s"}


def test_extend_reset_answers_a_loop_limit(proj, monkeypatch):
    seen = {}

    def fake(node, by, *, stop=False, reset=False, cwd=None, scope=None):
        seen.update(node=node, stop=stop, reset=reset)
        return {"status": "running"}

    monkeypatch.setattr(runtime, "extend_loop", fake)

    async def go(client, bearer):
        ok = await client.post("/api/cflow2/extend", headers=bearer, json={
            "cwd": str(proj), "scope": "v1s", "node": "x@3", "reset": True})
        bad = await client.post("/api/cflow2/extend", headers=bearer, json={
            "cwd": str(proj), "scope": "v1s", "node": "x@3", "reset": True, "visits": True})
        return ok.status, bad.status

    assert _http(go) == (200, 400)
    assert seen == {"node": "x@3", "stop": False, "reset": True}


# --------------------------------------------------------------------------- #
# collections and trigger lines on the run page (docs/cflow-v2.md 2.7, 2.8)
# --------------------------------------------------------------------------- #
COLLECTED = """\
cflow: 2
name: collected
collections:
  landing:
    kind: queue
    key: issue
    item: {issue: str, tip: str}
    status: {values: [requested, landed], initial: requested, settled: [landed]}
    access: {push: [daemon], set: [driver, person], remove: [driver]}
nodes:
  a: {title: A, prompt: do a, triggers: [{do: checks, at: leave}]}
  b: {title: B, prompt: do b}
body: {seq: [a, b]}
"""


def test_the_run_page_carries_collections_and_trigger_lines(proj):
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "v2" / "collected.yaml").write_text(
        COLLECTED, encoding="utf-8")
    runtime.start(layers.load("collected", cwd), cwd=cwd, scope="c1", caps={}, driver="c1")
    runtime.collection_op("push", "landing", cwd=cwd, scope="c1", principal="daemon",
                          item={"issue": "x-1", "tip": "abc"})
    runtime.report("a", cwd=cwd, scope="c1")
    [trig] = runtime.claim_triggers(cwd, "c1")
    runtime.complete_trigger(cwd, "c1", trig["id"], False, "no enabled status checks")

    async def go(client, bearer):
        q = f"cwd={cwd}&scope=c1"
        out = {"detail": await (await client.get(f"/api/cflow2/run?{q}", headers=bearer)).json()}
        resp = await client.post("/api/cflow2/collection", headers=bearer, json={
            "cwd": cwd, "scope": "c1", "op": "set", "collection": "landing", "key": "x-1",
            "fields": {"status": "landed"}})
        out["set"] = (resp.status, await resp.json())
        resp = await client.post("/api/cflow2/collection", headers=bearer, json={
            "cwd": cwd, "scope": "c1", "op": "remove", "collection": "landing", "key": "x-1"})
        out["remove"] = resp.status
        resp = await client.post("/api/cflow2/collection", headers=bearer, json={
            "cwd": cwd, "scope": "c1", "op": "push", "collection": "landing", "key": "x-1"})
        out["push"] = resp.status
        return out

    out = _http(go)
    col = out["detail"]["collections"]["landing"]
    assert col["kind"] == "queue" and col["status_values"] == ["requested", "landed"]
    assert [i["issue"] for i in col["items"]] == ["x-1"]
    events = [(e["event"], e.get("trigger")) for e in out["detail"]["trigger_events"]]
    assert events == [("trigger_queued", trig["id"]), ("trigger_skipped", trig["id"])]
    assert out["set"][0] == 200
    assert out["set"][1]["collections"]["landing"]["items"][0]["status"] == "landed"
    assert out["remove"] == 400   # remove is the driver's (access), not the person's
    assert out["push"] == 400


MOVE = """\
cflow: 2
name: move
nodes:
  a: {title: A, prompt: do a, force_target: true}
  b: {title: B, prompt: do b, returns: {ok: {n: int}, blocked: {}}}
  c: {title: C, prompt: do c, force_target: true}
body:
  try: {seq: [a, b, c]}
  catch: {blocked: a}
"""


def test_the_person_moves_reports_aborts_and_archives_a_v2_run(proj):
    """The run page's presses on a v2 run (docs 2.11): nudge, a forced move to
    a force_target node, a forced task outcome, abort, archive."""
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "v2" / "move.yaml").write_text(MOVE, encoding="utf-8")
    runtime.start(layers.load("move", cwd), cwd=cwd, scope="s3", caps={})

    async def go(client, bearer):
        out = {}
        slot = {"cwd": cwd, "scope": "s3"}

        async def post(path, **body):
            resp = await client.post(path, headers=bearer, json={**slot, **body})
            return resp.status, await resp.json()

        out["unauth"] = (await client.post("/api/cflow2/move", json={**slot, "node": "c"})).status
        out["detail"] = await (await client.get(
            f"/api/cflow2/run?cwd={cwd}&scope=s3", headers=bearer)).json()
        out["nudge"] = await post("/api/cflow2/nudge")
        out["unmarked"] = await post("/api/cflow2/move", node="b")
        out["move"] = await post("/api/cflow2/move", node="c", reason="a and b are done")
        out["back"] = await post("/api/cflow2/move", node="a")
        task = runtime.status(cwd=cwd, scope="s3")["tasks"][0]["instance"]
        runtime.report(task, cwd=cwd, scope="s3")  # a -> b
        task = runtime.status(cwd=cwd, scope="s3")["tasks"][0]["instance"]
        out["bad_value"] = await post("/api/cflow2/report", node=task, outcome="ok", value={})
        out["report"] = await post("/api/cflow2/report", node=task, outcome="blocked",
                                   reason="waiting on a review")
        out["early_archive"] = await post("/api/cflow2/archive")
        out["abort"] = await post("/api/cflow2/abort", reason="enough")
        out["archive"] = await post("/api/cflow2/archive")
        return out

    out = _http(go)
    assert out["unauth"] == 401
    marked = sorted(n.get("label") for n in out["detail"]["chart"]["nodes"].values()
                    if n.get("force_target"))
    assert marked == ["a", "c"]
    assert out["nudge"] == (200, {"ok": True, "nudge_scheduled_sessions": []})
    status, payload = out["unmarked"]
    assert status == 400 and "not a force target" in payload["error"]
    status, payload = out["move"]
    assert status == 200 and [t["label"] for t in payload["tasks"]] == ["c"]
    assert payload["forced"]["left"] and payload["nudge_scheduled_sessions"] == []
    assert [t["label"] for t in out["back"][1]["tasks"]] == ["a"]
    status, payload = out["bad_value"]
    assert status == 400 and "n" in payload["error"]
    status, payload = out["report"]
    assert status == 200 and [t["label"] for t in payload["tasks"]] == ["a"]  # the catch
    assert out["early_archive"][0] == 400
    assert out["abort"][0] == 200 and out["abort"][1]["status"] == "aborted"
    assert out["archive"] == (200, {**out["archive"][1], "status": "idle"})
    assert not RunStore(cwd, "s3").exists()
    events = [e for e in RunStore(cwd, "s3").archived()[0].joinpath("journal.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    assert any('"forced"' in e and '"by": "user"' in e for e in events)
    assert any('"reported"' in e and '"by": "user"' in e for e in events)


def test_the_node_pane_reads_one_node_in_full(proj):
    """GET /api/cflow2/node: the run page's node pane (claunch-wm67r) -- the
    definition's fields, what the type does, children with roles, the live
    instance, the last result and the node's journal lines."""
    cwd = str(proj)
    (proj / ".claunch" / "workflows" / "v2" / "move.yaml").write_text(MOVE, encoding="utf-8")
    runtime.start(layers.load("move", cwd), cwd=cwd, scope="s4", caps={})
    chart = RunStore(cwd, "s4").load_chart()
    ids = {n.label: n.id for n in chart.nodes.values() if n.label}
    task = runtime.status(cwd=cwd, scope="s4")["tasks"][0]["instance"]
    runtime.report(task, "ok", None, "a is done", cwd=cwd, scope="s4")  # a -> b

    async def go(client, bearer):
        q = f"cwd={cwd}&scope=s4"
        return {
            "unauth": (await client.get(f"/api/cflow2/node?{q}&node={ids['b']}")).status,
            "a": await (await client.get(f"/api/cflow2/node?{q}&node={ids['a']}", headers=bearer)).json(),
            "b": await (await client.get(f"/api/cflow2/node?{q}&node={ids['b']}", headers=bearer)).json(),
            "root": await (await client.get(f"/api/cflow2/node?{q}&node={chart.root}",
                                            headers=bearer)).json(),
            "none": (await client.get(f"/api/cflow2/node?{q}&node=zzz", headers=bearer)).status,
        }

    out = _http(go)
    assert out["unauth"] == 401 and out["none"] == 404
    b = out["b"]
    assert b["fields"]["prompt"] == "do b" and b["fields"]["returns"] == {"ok": {"n": "int"}, "blocked": {}}
    assert "flow_report" in b["how"]
    assert [i["await"] for i in b["instances"]] == ["task"]
    a = out["a"]
    assert a["node"]["force_target"] is True and a["visits"] == 1
    assert a["result"]["outcome"] == "ok"
    assert [e["event"] for e in a["journal"]][:1] == ["entered"]
    assert [c["role"] for c in out["root"]["children"]][:2] == ["try", "catch blocked"]
