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
