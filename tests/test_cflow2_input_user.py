"""A person changes a started run's start input (claunch-umq1f, docs 2.6).

``set_by: [user]`` lets a person overwrite a declared input at any time --
a ``bool`` too -- through ``claunch cflow2 input --user``, the run page and
``POST /api/cflow2/input`` (``runtime.set_input(caller=user)``). The agent's
doors (``flow_input``, ``claunch cflow2 input`` without ``--user``) keep
``set_by: [agent]``'s rule: an empty input, once, the driver only.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from claude_launcher.cflow2 import check, frontend, inputs, layers, mcp as cflow2_mcp, runtime
from claude_launcher.cflow2.store import RunStore

T0 = 1_000_000.0

DEF = """
inputs:
  approve: {type: bool, default: false, set_by: [user]}
  rounds: {type: int, set_by: [user]}
  mode: {type: enum, choices: [full, quick], default: full, set_by: [user]}
  tags: {type: list, set_by: [user]}
  need: {type: string, required: true, set_by: [user]}
  issue: {type: string, default: "", set_by: [agent]}
  goal: {type: text, default: ""}
  pr: {type: bool, set_by: [user]}
  no_pr: {type: bool, set_by: [user]}
input_constraints:
  - assert: "not ($inputs.pr == true and $inputs.no_pr == true)"
    message: pr and no_pr exclude each other
nodes:
  work: {prompt: "work approve=${data.inputs.approve}"}
  plan: {prompt: plan}
body:
  loop:
    max: 3
    while: "true"
    do:
      seq:
        - cond:
            - if: "$data.inputs.approve == true"
              then: plan
            - else: []
        - work
"""


@pytest.fixture(autouse=True)
def _no_session(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    monkeypatch.delenv("CLAUNCH_SCRATCH", raising=False)


def _chart(text: str = DEF):
    return frontend.compile_text("cflow: 2\nname: t\n" + textwrap.dedent(text))


def _start(tmp_path, values=None):
    return runtime.start(_chart(), cwd=str(tmp_path), scope="t", caps={}, driver="drv",
                         inputs={"need": "x", **(values or {})}, now=T0)


def _person(tmp_path, name, value):
    return runtime.set_input(name, value, caller=runtime.CALLER_USER, cwd=str(tmp_path),
                             scope="t", now=T0)


def _values(tmp_path):
    return RunStore(str(tmp_path), "t").load_state()["data"]["inputs"]


def _events(tmp_path):
    return [e for e in RunStore(str(tmp_path), "t").journal() if e["event"] == "input_set"]


# -- declaration -------------------------------------------------------------

def test_user_is_a_setter_and_may_name_a_bool():
    sch = _chart().meta["inputs"]
    assert sch["approve"]["set_by"] == ["user"]
    assert sch["issue"]["set_by"] == ["agent"]
    both = inputs.schema({"x": {"type": "string", "set_by": ["agent", "user"]}})
    assert both["x"]["set_by"] == ["agent", "user"]


@pytest.mark.parametrize("decl,msg", [
    ("{type: bool, set_by: [agent]}", "set_by agent applies to types"),
    ("{type: invariants, set_by: [user]}", "set_by user applies to types"),
    ("{type: acceptance, set_by: [user]}", "set_by user applies to types"),
    ("{type: bool, set_by: [agent, user]}", "set_by agent applies to types"),
])
def test_types_a_setter_may_not_name(decl, msg):
    with pytest.raises(inputs.InputsError, match=msg):
        inputs.schema({"x": yaml.safe_load(decl)})


def test_a_user_input_with_a_default_or_required_is_quiet():
    """A person overwrites, so the agent-only warnings do not apply."""
    codes = [f.code for f in check.check(_chart()) if f.code.startswith("set-by-")]
    assert codes == []


# -- a person's write ----------------------------------------------------------

def test_a_person_turns_a_bool_on_and_the_next_visit_reads_it(tmp_path):
    first = _start(tmp_path)
    assert [t["label"] for t in first["tasks"]] == ["work"]
    out = _person(tmp_path, "approve", "true")
    assert out["inputs"]["approve"] is True
    # the active task's prompt is rendered with the new value
    assert out["tasks"][0]["prompt"] == "work approve=true"
    nxt = runtime.report("work", cwd=str(tmp_path), scope="t", now=T0)
    assert [t["label"] for t in nxt["tasks"]] == ["plan"]  # the guard took the new value
    ev = _events(tmp_path)
    assert [(e["name"], e["value"], e["by"], e["was"]) for e in ev] == [
        ("approve", True, "user", False)]


def test_a_person_overwrites_and_clears(tmp_path):
    _start(tmp_path, {"rounds": 2, "tags": ["a"]})
    _person(tmp_path, "rounds", "5")
    _person(tmp_path, "mode", "quick")
    _person(tmp_path, "tags", "x, y")
    assert {k: _values(tmp_path)[k] for k in ("rounds", "mode", "tags")} == {
        "rounds": 5, "mode": "quick", "tags": ["x", "y"]}
    _person(tmp_path, "rounds", "")
    _person(tmp_path, "tags", "")
    _person(tmp_path, "approve", "")  # a bool's empty spelling is false
    v = _values(tmp_path)
    assert (v["rounds"], v["tags"], v["approve"]) == (None, [], False)
    assert [e["was"] for e in _events(tmp_path)][:3] == [2, "full", ["a"]]


@pytest.mark.parametrize("name,value,msg", [
    ("rounds", "many", "is not an integer"),
    ("mode", "slow", "is not one of full, quick"),
    ("approve", "maybe", "is not true or false"),
    ("need", "", "is required"),
    ("goal", "new goal", "no set_by: \\[user\\]"),
    ("issue", "claunch-x", "no set_by: \\[user\\]"),
    ("nope", "x", "unknown input"),
])
def test_a_persons_write_is_checked_like_a_start(tmp_path, name, value, msg):
    _start(tmp_path)
    before = _values(tmp_path)
    with pytest.raises(runtime.FlowError, match=msg):
        _person(tmp_path, name, value)
    assert _values(tmp_path) == before and _events(tmp_path) == []


def test_a_constraint_holds_against_a_persons_write(tmp_path):
    _start(tmp_path, {"pr": True})
    with pytest.raises(runtime.FlowError, match="pr and no_pr exclude each other"):
        _person(tmp_path, "no_pr", True)
    _person(tmp_path, "pr", False)
    _person(tmp_path, "no_pr", True)
    assert (_values(tmp_path)["pr"], _values(tmp_path)["no_pr"]) == (False, True)


@pytest.mark.parametrize("raw", [" , ", ",", "\n \n", []])
def test_a_required_list_is_not_cleared_through_blank_items(raw):
    """claunch-txjqr: ' , ' is not empty text but coerces to [] -- still a
    clear, so a required input refuses it like an empty value."""
    sch = inputs.schema({"tags": {"type": "list", "required": True, "set_by": ["user"]}})
    with pytest.raises(inputs.InputsError, match="is required"):
        inputs.settle(sch, {"tags": ["a"]}, "tags", raw, inputs.SETTER_USER)
    assert inputs.settle(sch, {"tags": ["a"]}, "tags", " b , ", inputs.SETTER_USER) == ["b"]
    loose = inputs.schema({"tags": {"type": "list", "set_by": ["user"]}})
    assert inputs.settle(loose, {"tags": ["a"]}, "tags", " , ", inputs.SETTER_USER) == []


def test_the_agent_cannot_write_a_user_input(tmp_path):
    """The agent's door keeps its own setter: an approval gate the person
    turned on is not the agent's to turn off."""
    _start(tmp_path)
    _person(tmp_path, "approve", True)
    with pytest.raises(runtime.FlowError, match="no set_by: \\[agent\\]"):
        runtime.set_input("approve", False, session="drv", cwd=str(tmp_path), scope="t")
    with pytest.raises(runtime.FlowError, match="no set_by: \\[agent\\]"):
        runtime.set_input("rounds", 9, session="drv", cwd=str(tmp_path), scope="t")
    assert _values(tmp_path)["approve"] is True


def test_mcp_flow_input_has_no_person_door(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUNCH_SESSION", "t")
    runtime.start(_chart(), cwd=str(tmp_path), scope="t", caps={}, driver="t",
                  inputs={"need": "x"})
    with pytest.raises(runtime.FlowError, match="no set_by: \\[agent\\]"):
        cflow2_mcp.call_tool("flow_input", {"name": "approve", "value": True,
                                            "caller": "user"})
    assert _values(tmp_path)["approve"] is False


def test_a_finished_run_is_refused(tmp_path):
    runtime.start(_chart("nodes:\n  a: {prompt: A}\nbody: a\n"
                         "inputs:\n  n: {type: int, set_by: [user]}\n"),
                  cwd=str(tmp_path), scope="t", caps={}, driver="drv", now=T0)
    runtime.report("a", cwd=str(tmp_path), scope="t", now=T0)
    with pytest.raises(runtime.FlowError, match="start a new one"):
        _person(tmp_path, "n", 1)


# -- CLI ---------------------------------------------------------------------

def _cli(cwd, *args, session=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env.pop("CLAUNCH_SESSION", None)
    if session:
        env["CLAUNCH_SESSION"] = session
    return subprocess.run([sys.executable, "-c", "import sys; from claude_launcher.cli import "
                           "main; sys.exit(main())", "cflow2", *args], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=60)


def test_cli_input_user(tmp_path):
    _start(tmp_path)
    p = _cli(tmp_path, "input", "approve", "true", "--user", "--scope", "t")
    assert p.returncode == 0, p.stderr
    assert "input approve = true" in p.stdout
    assert _values(tmp_path)["approve"] is True
    # the driver's own shell is the agent answering its own gate
    p = _cli(tmp_path, "input", "approve", "false", "--user", "--scope", "t", session="drv")
    assert p.returncode == 1 and "person's answer" in p.stderr
    # without --user the agent's rule holds
    p = _cli(tmp_path, "input", "approve", "false", "--scope", "t", session="drv")
    assert p.returncode == 1 and "no set_by: [agent]" in p.stderr
    assert _values(tmp_path)["approve"] is True


# -- daemon API and the run page's payload ------------------------------------

def _http(fn):
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


def test_api_input_and_run_detail(home, tmp_path):
    _start(tmp_path)
    cwd = str(tmp_path)

    async def go(client, bearer):
        out = {}
        body = {"cwd": cwd, "scope": "t", "name": "approve", "value": True}
        out["unauth"] = (await client.post("/api/cflow2/input", json=body)).status
        resp = await client.post("/api/cflow2/input", json=body, headers=bearer)
        out["ok"] = (resp.status, await resp.json())
        resp = await client.post("/api/cflow2/input", headers=bearer,
                                 json={"cwd": cwd, "scope": "t", "name": "goal", "value": "g"})
        out["fixed"] = (resp.status, await resp.json())
        resp = await client.post("/api/cflow2/input", headers=bearer,
                                 json={"cwd": cwd, "scope": "t", "value": 1})
        out["noname"] = resp.status
        out["detail"] = await (await client.get(
            f"/api/cflow2/run?cwd={cwd}&scope=t", headers=bearer)).json()
        return out

    out = _http(go)
    assert out["unauth"] == 401
    status, doc = out["ok"]
    assert status == 200 and doc["inputs"]["approve"] is True
    assert doc["nudge_scheduled_sessions"] == []
    assert out["fixed"][0] == 400 and "no set_by: [user]" in out["fixed"][1]["error"]
    assert out["noname"] == 400
    decls = {d["name"]: d for d in out["detail"]["input_decls"]}
    assert decls["approve"]["set_by"] == ["user"] and decls["approve"]["type"] == "bool"
    assert decls["goal"]["set_by"] == []
    assert _events(tmp_path)[0]["by"] == "user"


# -- the packaged worker ---------------------------------------------------------

def test_improv_worker_lets_a_person_change_the_approval_gate():
    chart = layers.load_file(Path(__file__).resolve().parents[1] / "src" / "claude_launcher"
                             / "workflows" / "v2" / "improv-worker-v2.yaml", None)
    sch = chart.meta["inputs"]
    assert sch["require_coding_approval"]["set_by"] == ["user"]
    assert sch["max_rounds"]["set_by"] == ["user"]
    # claunch-gqgcz.1: reviewer-ensure reads these every round
    assert sch["reviewer_profile"]["set_by"] == ["user"]
    assert sch["reviewer_harness"]["set_by"] == ["user"]
    assert inputs.settle(sch, {"reviewer_profile": ""}, "reviewer_profile", "work",
                         inputs.SETTER_USER) == "work"
    assert sch["issue"]["set_by"] == ["agent"]  # the start issue stays the agent's
